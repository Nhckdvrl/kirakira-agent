"""Durable execution facts shared by every reasoning entry point.

An unresolved tool intent is never permission to replay its side effect. Startup
recovery marks it for reconciliation; it does not execute tools or call a model.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent.turns.policy import ReasoningPolicy


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ExecutionStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = (
            sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            if read_only
            else sqlite3.connect(path, check_same_thread=False)
        )
        self.db.row_factory = sqlite3.Row
        if read_only:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_runs (
                run_id TEXT PRIMARY KEY, parent_id TEXT, session_key TEXT NOT NULL,
                scope TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, input_json TEXT NOT NULL, result_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_events (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(run_id,seq)
            );
        """)

    def start(self, *, session_key, scope, parent_id, inputs):
        run_id = uuid4().hex
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO execution_runs VALUES (?, ?, ?, ?, 'running', ?, NULL, ?, NULL, NULL)",
                (run_id, parent_id, session_key, scope, self.now(), _json(inputs)),
            )
        return run_id

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def append(self, run_id, kind, payload):
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT status FROM execution_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row[0] != "running":
                raise RuntimeError("execution writer is no longer active")
            seq = self.db.execute(
                "SELECT coalesce(max(seq),0)+1 FROM execution_events WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO execution_events VALUES (?, ?, ?, ?)",
                (run_id, seq, kind, _json(payload)),
            )

    def events(self, run_id):
        with self._lock:
            return [
                {"seq": r[0], "kind": r[1], "payload": json.loads(r[2])}
                for r in self.db.execute(
                    "SELECT seq,kind,payload FROM execution_events WHERE run_id=? ORDER BY seq",
                    (run_id,),
                )
            ]

    def unresolved(self, run_id):
        pending = {}
        for event in self.events(run_id):
            payload = event["payload"]
            if event["kind"] == "tool_intent":
                pending[payload["operation_id"]] = payload
            elif event["kind"] == "tool_result":
                pending.pop(payload["operation_id"], None)
        return list(pending.values())

    def finish(self, run_id, status, *, result=None, error=None):
        with self._lock, self.db:
            if self.unresolved(run_id):
                status = "needs_reconciliation"
            self.db.execute(
                "UPDATE execution_runs SET status=?,finished_at=?,result_json=?,error=? "
                "WHERE run_id=? AND status='running'",
                (
                    status,
                    self.now(),
                    _json(result) if result is not None else None,
                    error,
                    run_id,
                ),
            )

    def recover(self):
        """Only the workspace runtime owner calls this before starting new work."""
        with self._lock:
            pending = [
                r[0]
                for r in self.db.execute(
                    "SELECT run_id FROM execution_runs WHERE status='running'"
                )
            ]
            for run_id in pending:
                self.finish(
                    run_id,
                    "interrupted",
                    error="runtime exited before recording a terminal result",
                )
        return pending

    def recent(self, limit=30):
        with self._lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT run_id,parent_id,session_key,scope,status,started_at,finished_at,error "
                    "FROM execution_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def needs_reconciliation_since(self, session_key, started_at):
        with self._lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM execution_runs WHERE session_key=? AND started_at>=? "
                    "AND status='needs_reconciliation' LIMIT 1",
                    (session_key, started_at),
                ).fetchone()
                is not None
            )

    def close(self):
        self.db.close()


@dataclass
class ExecutionContext:
    store: ExecutionStore | None
    run_id: str
    policy: ReasoningPolicy
    task: object
    tool_started: bool = False

    def check(self):
        if self.policy.check_current is not None:
            self.policy.check_current()

    def record(self, kind, payload):
        if self.store is not None:
            self.store.append(self.run_id, kind, payload)


_current: ContextVar[ExecutionContext | None] = ContextVar(
    "agent_execution", default=None
)


def current_execution():
    ctx = _current.get()
    return ctx if ctx is not None and ctx.task is asyncio.current_task() else None


@contextmanager
def execution_scope(store, *, session_key, scope, inputs, policy):
    parent = _current.get()
    run_id = (
        store.start(
            session_key=session_key,
            scope=scope,
            parent_id=parent.run_id if parent else None,
            inputs=inputs,
        )
        if store
        else uuid4().hex
    )
    ctx = ExecutionContext(store, run_id, policy, asyncio.current_task())
    token = _current.set(ctx)
    try:
        yield ctx
    except BaseException as exc:
        if store:
            store.finish(
                run_id,
                "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                error=type(exc).__name__ + ": " + str(exc),
            )
        raise
    finally:
        _current.reset(token)


def policy_inputs(policy):
    return {
        "allowed_tools": sorted(policy.allowed_tools)
        if policy.allowed_tools is not None
        else None,
        "terminal_tools": sorted(policy.terminal_tools),
        "exclusive_terminal": policy.exclusive_terminal,
        "tool_choice": policy.tool_choice,
        "final_tool_choice": policy.final_tool_choice,
        "summarize_on_exhaustion": policy.summarize_on_exhaustion,
    }
