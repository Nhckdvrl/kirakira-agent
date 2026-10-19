"""Read execution facts without recovering, replaying or changing runtime state."""

import argparse
import json
from pathlib import Path

from plugins.execution.service import ExecutionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run")
    args = parser.parse_args()
    store = ExecutionStore(
        args.workspace / ".kirakira" / "executions.db", read_only=True
    )
    try:
        result = (
            {"events": store.events(args.run), "unresolved": store.unresolved(args.run)}
            if args.run
            else store.recent()
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
