"""主动链路的持久状态（``proactive.db``）。

职责：事件去重/入库、未读队列、消费标记、推送冷却时间。
参考 akashic 的 `plugins/wake_proactive/state.py`，MVP 只保留去重 + ACK 队列 + 推送节流。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    item_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread'
);
CREATE INDEX IF NOT EXISTS idx_events_channel_status
    ON events(channel, status);
CREATE TABLE IF NOT EXISTS push_state (
    session_key TEXT PRIMARY KEY,
    last_push_at TEXT NOT NULL
);
"""


class ProactiveStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def ingest(
        self,
        channel: str,
        events: Sequence[Dict[str, Any]],
        now: datetime,
    ) -> List[str]:
        """入库一批事件，返回本轮**新出现**的 item_id（已存在的忽略）。"""
        new_ids: List[str] = []
        for event in events:
            item_id = str(event.get("item_id") or "").strip()
            if not item_id:
                continue
            source_id = str(event.get("_source") or "").strip()
            source_event_id = str(
                event.get("event_id") or event.get("id") or ""
            ).strip()
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO events
                    (item_id, channel, source_id, source_event_id,
                     payload, first_seen_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'unread')
                """,
                (
                    item_id,
                    channel,
                    source_id,
                    source_event_id,
                    json.dumps(event, ensure_ascii=False),
                    now.isoformat(),
                ),
            )
            if cursor.rowcount:
                new_ids.append(item_id)
        self._db.commit()
        return new_ids

    def unread(self, channel: str) -> List[Dict[str, Any]]:
        """返回某通道所有未读事件（含 first_seen_at 注解）。"""
        rows = self._db.execute(
            """
            SELECT item_id, source_id, source_event_id, payload, first_seen_at
            FROM events
            WHERE channel = ? AND status = 'unread'
            ORDER BY first_seen_at ASC
            """,
            (channel,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            event = json.loads(row["payload"])
            event["item_id"] = row["item_id"]
            event["_source"] = row["source_id"]
            event["first_seen_at"] = row["first_seen_at"]
            out.append(event)
        return out

    def consume(self, item_ids: Sequence[str], now: datetime) -> None:
        """把事件标记为已消费，之后不再进入未读队列。"""
        ids = [str(i) for i in item_ids if str(i).strip()]
        if not ids:
            return
        self._db.executemany(
            "UPDATE events SET status = 'consumed' WHERE item_id = ?",
            [(item_id,) for item_id in ids],
        )
        self._db.commit()

    def last_push_at(self, session_key: str) -> datetime | None:
        row = self._db.execute(
            "SELECT last_push_at FROM push_state WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["last_push_at"])

    def mark_push(self, session_key: str, now: datetime) -> None:
        self._db.execute(
            """
            INSERT INTO push_state (session_key, last_push_at)
            VALUES (?, ?)
            ON CONFLICT(session_key) DO UPDATE SET last_push_at = excluded.last_push_at
            """,
            (session_key, now.isoformat()),
        )
        self._db.commit()

    def in_cooldown(
        self,
        session_key: str,
        now: datetime,
        cooldown_hours: float,
    ) -> bool:
        """距上次推送不足冷却窗口时返回 True（用于抑制 content 刷屏）。"""
        if cooldown_hours <= 0:
            return False
        last = self.last_push_at(session_key)
        if last is None:
            return False
        elapsed_hours = (now - last).total_seconds() / 3600.0
        return elapsed_hours < cooldown_hours
