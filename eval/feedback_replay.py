"""Replay recorded follow-up feedback against the pool, without contacting anyone.

The roadmap asks for an offline replay and a holdout before any feedback-driven
update strategy is trusted. This module is that replay: it reads a workspace's
own stored ratings and Content items, recomputes the attention weight each watch
would have had, and reports which contact decisions would change.

It decides nothing and sends nothing. Three separate facts are reported
separately, because one does not imply another:

  * how many watches have enough ratings for any adjustment at all,
  * how the pool mass moves once those weights apply, and
  * how many pool decisions actually flip, in which direction.

A flipped decision is not by itself an improvement. Whether a suppressed
notification should have been suppressed is the owner's judgement, which this
cannot supply; `--holdout` keeps a fraction of the ratings out of the weight so
the replay can be scored later against ratings it never saw.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from plugins.followups.feedback import MIN_RATINGS, measure
from plugins.wake.pool import measure_pool


def _rows(connection: sqlite3.Connection, prefix: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for key, value in connection.execute(
        "select key, value from owner_records where key like ?", (prefix + "%",)
    ):
        try:
            out.append((key, json.loads(value if isinstance(value, str) else value.decode())))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def _in_holdout(key: str, fraction: float) -> bool:
    """Stable per-rating split, so repeated runs hold out the same records."""
    if fraction <= 0.0:
        return False
    digest = hashlib.sha256(key.encode()).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) < fraction


def replay(store: Path, *, holdout: float) -> dict[str, object]:
    with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as connection:
        tables = {name for (name,) in connection.execute(
            "select name from sqlite_master where type='table'")}
        if "owner_records" not in tables:
            raise SystemExit(f"{store} 不包含 owner_records 表")
        feedback = _rows(connection, "feedback:followups:")
        items = _rows(connection, "item:")

    # Group ratings by watch, splitting out the holdout.
    applied: dict[str, list[dict]] = defaultdict(list)
    held: dict[str, list[dict]] = defaultdict(list)
    for key, value in feedback:
        parts = key.split(":")
        if len(parts) < 4:
            continue
        watch = parts[2]
        (held if _in_holdout(key, holdout) else applied)[watch].append(value)

    weights: dict[str, object] = {}
    adjusted = 0
    for watch, records in sorted(applied.items()):
        attention = measure(records)
        adjusted += attention.adjusted
        weights[watch] = {**attention.as_dict(), "held_out": len(held.get(watch, ()))}

    # Replay the pool over whatever Content items the workspace retained.
    now = datetime.now(UTC)
    events: list[dict] = []
    for _key, value in items:
        payload = value.get("payload")
        if isinstance(payload, dict) and "_wake_initial_score" in payload:
            events.append(dict(payload) | {"id": str(value.get("item_id", ""))})
    decision: dict[str, object] = {"events_replayed": len(events)}
    if events:
        identities = {event["id"] for event in events}
        plain = measure_pool([{**e, "attention_weight": 1.0} for e in events],
                             now=now, new_item_ids=identities)
        weighted = measure_pool(events, now=now, new_item_ids=identities)
        decision |= {
            "pool_mass_unweighted": round(plain.pool_mass, 4),
            "pool_mass_weighted": round(weighted.pool_mass, 4),
            "threshold": plain.threshold,
            "would_wake_unweighted": plain.should_wake,
            "would_wake_weighted": weighted.should_wake,
            "decision_flipped": plain.should_wake != weighted.should_wake,
            "direction": ("quieter" if plain.should_wake and not weighted.should_wake
                          else "louder" if weighted.should_wake and not plain.should_wake
                          else "unchanged"),
        }
    return {
        "conditions": {"store": str(store), "holdout_fraction": holdout,
                       "min_ratings_for_adjustment": MIN_RATINGS},
        "ratings": {"total": len(feedback),
                    "applied": sum(len(v) for v in applied.values()),
                    "held_out": sum(len(v) for v in held.values()),
                    "watches_rated": len(applied),
                    "watches_adjusted": adjusted},
        "weights": weights,
        "decision": decision,
        "caveat": "翻转不等于改善；被抑制的通知是否本就该抑制，需要用留出评价单独评分。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True,
                        help="followups 的 owner_records 数据库（只读打开）")
    parser.add_argument("--holdout", type=float, default=0.0,
                        help="按稳定哈希留出的评价比例，用于之后评分")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not 0.0 <= args.holdout < 1.0:
        raise SystemExit("--holdout 必须在 [0, 1) 之间")
    report = replay(args.store, holdout=args.holdout)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
