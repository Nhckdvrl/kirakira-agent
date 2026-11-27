"""Diff the model-callable tool surface against upstream akashic.

Functional alignment needs a unit. For an agent the closest one to "what can
it do" is the set of tools the model may call, so this reads every
`catalog.register(name=...)` out of the source of three trees -- this fork,
upstream core, and upstream's extra plugin repository -- and reports the
difference both ways.

Upstream split its plugins into core and extra (their ADR 0167), so both have
to be passed: comparing against core alone reports capabilities as missing
upstream when they only moved.

A tool is identified by a `register(...)` call that passes `parameters` -- the
schema the model is shown. The same `register(name=...)` shape is used for
context materials and UI slots, and counting those reports gaps that are not
tool gaps at all.

This reads source, not a running instance. It cannot see a tool registered
under a computed name, and it does not check that two tools sharing a name
behave alike -- `fetch_messages` against our `read_memory_source` is exactly
that case, and it is settled by reading the code, not by this script.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

SKIP = {".git", "node_modules", ".venv", "__pycache__", "tests", "build", "dist"}


def registered_tools(root: Path) -> dict[str, str]:
    """Tool name -> the file that registers it."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name not in {"register", "register_tool"}:
                continue
            # `register(name=...)` is also how context materials, UI slots and web
            # modules are registered. Only a model-callable tool carries the JSON
            # schema the model is shown, so require it -- without this, upstream's
            # `session-handoff` context material counts as a missing tool.
            keywords = {keyword.arg for keyword in node.keywords}
            if "parameters" not in keywords:
                continue
            for keyword in node.keywords:
                if (keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)):
                    found[keyword.value.value] = str(path.relative_to(root))
    return found


def report(ours: dict[str, str], core: dict[str, str], extra: dict[str, str]) -> dict[str, object]:
    upstream = {**core, **extra}
    missing = sorted(set(upstream) - set(ours))
    extraneous = sorted(set(ours) - set(upstream))
    return {
        "counts": {"ours": len(ours), "upstream_core": len(core),
                   "upstream_extra": len(extra), "upstream_total": len(upstream)},
        "upstream_only": [{"tool": name, "repo": "core" if name in core else "extra",
                           "file": upstream[name]} for name in missing],
        "ours_only": [{"tool": name, "file": ours[name]} for name in extraneous],
        "shared": sorted(set(ours) & set(upstream)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream_core", type=Path, help="checkout of kachofugetsu09/akashic-agent")
    parser.add_argument("upstream_extra", type=Path, help="checkout of kachofugetsu09/akashic-plugins")
    parser.add_argument("--ours", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit the report instead of a table")
    args = parser.parse_args()

    for path in (args.upstream_core, args.upstream_extra, args.ours):
        if not path.is_dir():
            parser.error(f"not a directory: {path}")

    value = report(registered_tools(args.ours), registered_tools(args.upstream_core),
                   registered_tools(args.upstream_extra))
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0

    counts = value["counts"]
    assert isinstance(counts, dict)
    print("ours {ours}  upstream core {upstream_core}  extra {upstream_extra}  "
          "upstream total {upstream_total}".format(**counts))
    for title, rows, shape in (
        ("upstream has, we do not", value["upstream_only"], "{tool:34s} {repo:5s} {file}"),
        ("we have, upstream does not", value["ours_only"], "{tool:34s} {file}"),
    ):
        assert isinstance(rows, list)
        print(f"\n=== {title} ({len(rows)}) ===")
        for row in rows:
            assert isinstance(row, dict)
            print("  " + shape.format(**row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
