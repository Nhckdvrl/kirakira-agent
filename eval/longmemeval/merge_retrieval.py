"""Merge completed independent shards while a reader consumes the growing file."""

import argparse
import asyncio
import json
from pathlib import Path


async def merge(a):
    known = (
        {r["question_id"] for r in map(json.loads, a.output.read_text().splitlines())}
        if a.output.exists()
        else set()
    )
    while len(known) < a.expected_cases:
        for path in a.inputs:
            if not path.exists():
                continue
            for line in path.read_text().splitlines(keepends=True):
                if not line.endswith("\n"):
                    continue
                row = json.loads(line)
                if row["question_id"] in known:
                    continue
                with a.output.open("a") as out:
                    out.write(line)
                    out.flush()
                known.add(row["question_id"])
        await asyncio.sleep(1)
    while not all(p.with_suffix(".manifest.json").exists() for p in a.inputs):
        await asyncio.sleep(1)
    parts = [json.loads(p.with_suffix(".manifest.json").read_text()) for p in a.inputs]
    base = {k: v for k, v in parts[0].items() if k not in {"shards", "shard_index"}}
    for part in parts[1:]:
        if {
            k: v for k, v in part.items() if k not in {"shards", "shard_index"}
        } != base:
            raise ValueError("retrieval shards used different protocols or source code")
    a.output.with_suffix(".manifest.json").write_text(
        json.dumps({**base, "shards": len(parts)}, indent=2)
    )
    print("merged", len(known), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--inputs", nargs="+", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--expected-cases", type=int, default=36)
    asyncio.run(merge(p.parse_args()))


if __name__ == "__main__":
    main()
