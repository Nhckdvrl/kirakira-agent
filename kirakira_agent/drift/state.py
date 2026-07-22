"""Drift 链路的持久状态（``drift.db``）。

保存每轮 run、每个 skill 的跨轮连续性（scratchpad / next_tendency）、以及
全局 min_interval 门控所需的 last_drift_at。参考 akashic 的
`plugins/drift_flow/state.py`，MVP 只保留 run 记录 + skill 连续性 + 节流。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill TEXT NOT NULL,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    briefing TEXT NOT NULL DEFAULT '',
    message_result TEXT NOT NULL DEFAULT 'silent'
);
CREATE INDEX IF NOT EXISTS idx_runs_skill ON runs(skill, run_at);
CREATE TABLE IF NOT EXISTS continuum (
    skill TEXT PRIMARY KEY,
    scratchpad TEXT NOT NULL DEFAULT '',
    next_tendency TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class DriftStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def last_drift_at(self) -> Optional[datetime]:
        row = self._db.execute(
            "SELECT run_at FROM runs ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row["run_at"])
        except ValueError:
            return None

    def can_run(self, now: datetime, min_interval_hours: float) -> bool:
        """距上次 Drift 不足 min_interval 时返回 False。"""
        if min_interval_hours <= 0:
            return True
        last = self.last_drift_at()
        if last is None:
            return True
        return (now - last).total_seconds() / 3600.0 >= min_interval_hours

    def last_run_at_by_skill(self) -> Dict[str, datetime]:
        rows = self._db.execute(
            "SELECT skill, MAX(run_at) AS last FROM runs GROUP BY skill"
        ).fetchall()
        out: Dict[str, datetime] = {}
        for row in rows:
            try:
                out[row["skill"]] = datetime.fromisoformat(row["last"])
            except (ValueError, TypeError):
                continue
        return out

    def record_run(
        self,
        *,
        skill: str,
        now: datetime,
        status: str,
        briefing: str,
        message_result: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO runs (skill, run_at, status, briefing, message_result)
            VALUES (?, ?, ?, ?, ?)
            """,
            (skill, now.isoformat(), status, briefing, message_result),
        )
        self._db.commit()

    def recent_runs(self, limit: int = 10) -> List[dict]:
        rows = self._db.execute(
            """
            SELECT skill, run_at, status, briefing, message_result
            FROM runs ORDER BY run_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_continuum(self, skill: str) -> dict:
        row = self._db.execute(
            "SELECT scratchpad, next_tendency FROM continuum WHERE skill = ?",
            (skill,),
        ).fetchone()
        if row is None:
            return {"scratchpad": "", "next_tendency": ""}
        return {"scratchpad": row["scratchpad"], "next_tendency": row["next_tendency"]}

    def save_continuum(
        self,
        *,
        skill: str,
        now: datetime,
        scratchpad: str = "",
        next_tendency: str = "",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO continuum (skill, scratchpad, next_tendency, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(skill) DO UPDATE SET
                scratchpad = excluded.scratchpad,
                next_tendency = excluded.next_tendency,
                updated_at = excluded.updated_at
            """,
            (skill, scratchpad, next_tendency, now.isoformat()),
        )
        self._db.commit()
