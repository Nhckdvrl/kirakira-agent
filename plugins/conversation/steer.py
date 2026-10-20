"""Volatile staged inputs become Message facts only at a reply model boundary.

If the reply ends before injection, the conversation owner starts an ordinary
request. On owner shutdown, unaccepted staging is withdrawn, never persisted as
an alternative queue of user facts.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from uuid import uuid4


class Steers:
    def __init__(self, programs):
        self.programs = programs
        self.bound = {}
        self.pending = {}

    @contextmanager
    def bind(self, binding):
        if binding.value["source"] != "conversation":
            yield
            return
        key = binding.session_key
        self.bound[key] = binding
        binding.prepare_inputs = lambda: self.inject(binding)
        try:
            yield
        finally:
            if self.bound.get(key) is binding:
                self.bound.pop(key)

    def stage(self, session_key, message):
        binding = self.bound.get(session_key)
        if binding is None or binding.value["source"] != "conversation":
            return None
        if not self.programs.store.is_current(binding.identity):
            return None
        identity = str(
            message.metadata.get("client_request_id") or "steer_" + uuid4().hex
        )
        message.metadata["client_request_id"] = identity
        items = self.pending.setdefault(session_key, {})
        if identity in items:
            previous = items[identity]
            if previous.content != message.content or previous.media != message.media:
                raise ValueError("steer identity reused with different content")
        else:
            items[identity] = replace(
                message, metadata=dict(message.metadata), media=list(message.media)
            )
        return identity

    def withdraw(self, session_key, identity):
        return self.pending.get(session_key, {}).pop(identity, None) is not None

    def inject(self, binding):
        # No await between snapshot, guard and commit: staging and withdrawal
        # cannot interleave with acceptance on the owner's event loop.
        pending = self.pending.get(binding.session_key, {})
        for identity, request in tuple(pending.items()):
            binding.value = self.programs.store.steer(binding, request)
            pending.pop(identity)
        from plugins.context.context_builder import ContextBuilder

        materials = []
        for identity in binding.value.get("steer_ids", []):
            request = self.programs.store.inputs.read(
                identity, session_key=binding.session_key
            ).request
            materials.append(
                {
                    "role": "user",
                    "_ledger_message_id": identity,
                    "content": ContextBuilder._build_user_content(
                        request["content"],
                        request["media"],
                        datetime.fromisoformat(request["timestamp"]),
                    ),
                }
            )
        return materials

    def take_pending(self, session_key):
        return tuple(self.pending.pop(session_key, {}).values())
