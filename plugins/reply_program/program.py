"""Reply-program lifecycle, durable tool facts and source supersession."""

import asyncio
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from uuid import uuid4

from plugins.ledger.owner_store import StateConflict
from plugins.reply_program.store import ProgramStore, inbound, tool_chain
from plugins.sources.service import SourceSessions

_current = ContextVar("reply_program", default=None)


def current_program(session_key=None):
    value = _current.get()
    return (
        value
        if value is not None
        and (session_key is None or value.session_key == session_key)
        else None
    )


def replay_messages(groups):
    messages = []
    for group in groups:
        calls = group["calls"]
        messages.append(
            {
                "role": "assistant",
                "content": group["text"],
                "reasoning_content": group.get("reasoning_content") or "",
                "tool_calls": [
                    {
                        "id": c["call_id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in calls
                ],
            }
        )
        messages.extend(
            {
                "role": "tool",
                "tool_call_id": c["call_id"],
                "content": c["result"],
                "is_error": c["status"] != "success",
            }
            for c in calls
        )
    return messages


def unpublished_chain(groups, published):
    result = []
    for group in groups:
        calls = [c for c in group["calls"] if c.get("effect_key") not in published]
        if calls:
            result.append({**group, "calls": calls})
    return result


class ProgramBinding:
    def __init__(self, owner, identity, value):
        self.owner, self.identity, self.value = owner, identity, value
        self.session_key, self.lease = value["session_key"], value["lease"]
        self.task, self.phase = asyncio.current_task(), "preparing"
        self.batch = None
        self.prepare_inputs = lambda: []

    def ensure_current(self):
        if not self.owner.store.is_current(self.identity):
            raise asyncio.CancelledError("superseded by a newer accepted input")

    @contextmanager
    def model_scope(self):
        self.ensure_current()
        old, self.phase = self.phase, "model"
        try:
            yield
            self.ensure_current()
        finally:
            self.phase = old

    def freeze_grant(self, tools, policy, disabled, config):
        grant = {}
        for name in tools.names():
            if name in disabled or not policy.allows(name):
                continue
            tool = tools.get_tool(name)
            encoded = json.dumps(
                {"spec": asdict(tool.spec), "meta": asdict(tool.meta)}, sort_keys=True
            )
            grant[name] = hashlib.sha256(encoded.encode()).hexdigest()
        signature = {
            "tools": grant,
            "model": config.model,
            "max_steps": config.max_iterations,
            "max_tokens": config.max_tokens,
        }
        prior = self.value["grant"]
        if prior is not None:
            if signature != prior:
                raise StateConflict(
                    "model or tool grant changed; inspect the program and start a new request"
                )
            return
        self.value = self.owner.store.change(
            self.identity, self.lease, lambda v: v.update(grant=signature)
        )

    def begin_batch(self, response, iteration):
        if len({c.id for c in response.tool_calls}) != len(response.tool_calls):
            raise ValueError("duplicate tool call identities in a model batch")

        def update(value):
            index = len(value["batches"])
            value["batches"].append(
                {
                    "text": response.text,
                    "reasoning_content": response.reasoning_content,
                    "calls": [
                        {**asdict(c), "key": f"{self.identity}:{index}:{n}"}
                        for n, c in enumerate(response.tool_calls)
                    ],
                    "iteration": iteration,
                }
            )

        self.value = self.owner.store.change(self.identity, self.lease, update)
        self.batch = self.value["batches"][-1]
        self.phase = "tools"

    def effect_key(self, call_id):
        if self.batch is None:
            raise RuntimeError("tool effect outside its durable model batch")
        return next(c["key"] for c in self.batch["calls"] if c["id"] == call_id)

    def start_effect(self, call_id, name, arguments):
        key = self.effect_key(call_id)

        def update(value):
            if key in value["effects"]:
                raise StateConflict("tool effect already has an intent or result")
            value["effects"][key] = {
                "started": True,
                "name": name,
                "arguments": arguments,
                "result": None,
            }

        try:
            self.value = self.owner.store.change(self.identity, self.lease, update)
        except StateConflict:
            if not self.owner.store.is_current(self.identity):
                return False
            raise
        return True

    def record_result(self, call_id, result):
        key = self.effect_key(call_id)

        def update(value):
            row = value["effects"].setdefault(key, {"started": False})
            if row.get("result") is not None and row["result"] != result:
                raise StateConflict("tool result is immutable")
            row["result"] = result

        self.value = self.owner.store.change(
            self.identity, self.lease, update, require_current=False
        )

    def progress(self, iteration, usage):
        self.value = self.owner.store.change(
            self.identity,
            self.lease,
            lambda v: v.update(iteration=iteration, usage=usage),
            require_current=False,
        )
        self.phase = "ready"

    def snapshot(self):
        self.value = self.owner.store.read(self.identity, self.session_key)
        return self.value

    def query_anchor(self, current):
        inputs, seen = [current], {self.identity}
        previous = self.value["predecessor_id"]
        while previous is not None:
            if previous in seen or len(seen) > 1024:
                raise ValueError("invalid reply input ancestry")
            seen.add(previous)
            row = self.owner.store.read(previous, self.session_key)
            inputs.append(row["request"]["content"])
            previous = row["predecessor_id"]
        return "\n\n".join(reversed(inputs))


class ReplyPrograms:
    def __init__(self, sessions, sources=None, inputs=None, messages=None):
        self.sessions = sessions
        self.sources = sources or SourceSessions(sessions)
        self.store = ProgramStore(sessions, self.sources, inputs, messages)
        self.recovery_requests = []
        self.suspending = False
        sessions.register_initializer(
            "reply_programs",
            lambda session, tx: None,
            on_delete=self.store.delete_session,
            on_redact=self.store.redact,
        )

    def accept_channel(self, msg):
        if msg.metadata.get("resume_program_id"):
            self.store.read(str(msg.metadata["resume_program_id"]), msg.session_key)
            return
        if not (
            msg.metadata.get("input_message_id")
            or msg.metadata.get("client_request_id")
        ):
            msg.metadata["input_message_id"] = "input_" + uuid4().hex
        self.accept(
            msg,
            msg.session_key,
            supersede=self.sources.channel_policy(msg),
            auto_resume=True,
        )

    def accept(self, msg, key, *, supersede=False, auto_resume=False):
        # Validate immutable Project scope before publishing any request receipt.
        metadata = (
            {"project_id": msg.metadata["project_id"]}
            if "project_id" in msg.metadata
            else None
        )
        self.sessions.get_or_create(key, metadata=metadata)
        identity, created = self.store.accept(
            msg, key, supersede=supersede, auto_resume=auto_resume
        )
        value = self.store.read(identity, key)
        if created and supersede:
            self.sources.accepted(key, value["source"])
        return identity, value

    @contextmanager
    def activate(self, identity, *, resume=False):
        binding = ProgramBinding(
            self, identity, self.store.claim(identity, resume=resume)
        )
        key = (binding.session_key, binding.value["source"])
        token = _current.set(binding)

        def cancel():
            if binding.phase == "model":
                binding.task.cancel("superseded by a newer accepted input")

        try:
            with self.sources.task(*key, cancel):
                yield binding
        finally:
            _current.reset(token)

    def resume_request(self, identity, session_key):
        value = self.store.read(identity, session_key)
        if value["status"] not in {"paused", "failed"}:
            raise StateConflict("only a paused or failed program can resume")
        msg = inbound(value["request"])
        msg.metadata = {**msg.metadata, "resume_program_id": identity}
        msg.metadata.pop("input_message_id", None)
        return msg

    def recover(self, persist):
        # Bootstrap owns recovery before new admission. No tools or models run.
        for identity, value in self.store.recoverable():
            if value["status"] == "queued":
                value = self.store.claim(identity)
            binding = ProgramBinding(self, identity, value)
            persist(
                value["session_key"],
                inbound(value["request"]),
                binding=binding,
                status="paused" if self.store.is_current(identity) else "superseded",
                error="runtime exited before program settlement",
                recovery_pending=value.get("auto_resume", False),
            )
        for identity, value in self.store.pending_recovery():
            head = self.sessions.message_head(value["session_key"])
            if self.store.is_current(identity) and value["history_head"] == (
                list(head) if head else None
            ):
                self.recovery_requests.append(
                    self.resume_request(identity, value["session_key"])
                )

    @staticmethod
    def chain(binding):
        return tool_chain(binding.snapshot())
