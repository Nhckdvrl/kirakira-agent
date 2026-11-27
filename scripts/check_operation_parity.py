"""One-to-one gate between user interface entries and agent-callable operations.

The Operation Bus is only worth its weight if "the user and the agent reach the
same action" is checked rather than asserted. Each `OperationSpec` names the UI
entries it mirrors; this reads the plugin sources and holds two rules:

1. **Every declared mirror must exist, and belong to the declaring plugin.** An
   operation claiming to mirror `ui:working_directory/directory.current` fails
   if no plugin UI serves that method, and fails again if the operation is
   registered by a different plugin -- otherwise one plugin could satisfy the
   gate by pointing at a neighbour's entry, which proves nothing about whether
   its own action is reachable both ways. Without either rule `mirrors` decays
   into a comment, which is what this gate exists to prevent.
2. **Every UI entry is either mirrored or listed as not yet mirrored.** The
   backlog lives in `scripts/operation_parity_manifest.json`, so the set can
   only shrink: mirroring an entry means deleting its line, and adding a new
   unmirrored UI entry fails until it is either mirrored or recorded with a
   reason.

Rule 2 starts with a long backlog on purpose. A gate that demanded full parity
on day one would have to be switched off, and a gate that is off checks nothing.

Source-reading, like the tool diff: it sees `register_plugin_ui(..., query=...)`
handlers that dispatch on a `method` string, and `OperationSpec(...)` literals.
It cannot see an entry assembled at runtime, and it does not check that two
sides behave alike -- only that the correspondence is declared and real.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
MANIFEST = ROOT / "scripts/operation_parity_manifest.json"
SKIP = {"__pycache__", "node_modules"}


def _plugin_id(path: Path) -> str:
    relative = path.relative_to(PLUGINS)
    return relative.parts[0]


def _string_constants(node: ast.AST) -> set[str]:
    return {inner.value for inner in ast.walk(node)
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str)}


def ui_entries() -> dict[str, str]:
    """`ui:<plugin>/<method>` -> the file that serves it.

    A plugin UI registers one `query` callable that dispatches on a method name,
    so the entries are the string literals that callable compares against.
    """
    found: dict[str, str] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        registers = any(
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "register_plugin_ui"
            for node in ast.walk(tree))
        if not registers:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            names = {argument.arg for argument in node.args.args}
            names |= {argument.arg for argument in node.args.kwonlyargs}
            if "method" not in names:
                continue
            for literal in _string_constants(node):
                if "." in literal and " " not in literal and literal.islower():
                    found[f"ui:{_plugin_id(path)}/{literal}"] = str(path.relative_to(ROOT))
    return found


def declared_mirrors() -> dict[str, tuple[list[str], str]]:
    """operation_id -> (the UI entries it says it mirrors, the declaring plugin)."""
    found: dict[str, tuple[list[str], str]] = {}
    for path in sorted(PLUGINS.rglob("*.py")):
        if any(part in SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) == "OperationSpec"):
                continue
            fields = {keyword.arg: keyword.value for keyword in node.keywords}
            identity = fields.get("operation_id")
            if not (isinstance(identity, ast.Constant) and isinstance(identity.value, str)):
                continue
            mirrors = fields.get("mirrors")
            names = sorted(_string_constants(mirrors)) if mirrors is not None else []
            found[identity.value] = (names, _plugin_id(path))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", action="store_true",
                        help="record every currently unmirrored UI entry as the backlog")
    args = parser.parse_args()

    entries = ui_entries()
    mirrors = declared_mirrors()
    claimed = {name for names, _owner in mirrors.values() for name in names}

    if args.write_manifest:
        backlog = sorted(name for name in entries if name not in claimed)
        MANIFEST.write_text(json.dumps({
            "comment": ("UI entries with no agent-callable operation yet. This list may only "
                        "shrink: mirror an entry and delete its line. Adding a new unmirrored "
                        "entry fails scripts/check_operation_parity.py until it is mirrored or "
                        "recorded here with a reason."),
            "unmirrored": backlog,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"recorded {len(backlog)} unmirrored entries")
        return 0

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
    allowed = set(manifest.get("unmirrored", []))
    problems: list[str] = []

    for operation, (names, owner) in sorted(mirrors.items()):
        for name in names:
            if name not in entries:
                problems.append(
                    f"{operation} claims to mirror {name}, but no plugin UI serves it")
                continue
            declared_plugin = name.removeprefix("ui:").split("/", 1)[0]
            if declared_plugin != owner:
                problems.append(
                    f"{operation} is registered by {owner} but claims to mirror "
                    f"{name}, which belongs to {declared_plugin}")

    for name in sorted(entries):
        if name not in claimed and name not in allowed:
            problems.append(
                f"{name} ({entries[name]}) has no agent-callable operation and is not "
                f"recorded in {MANIFEST.relative_to(ROOT)}")

    stale = sorted(allowed & claimed)
    for name in stale:
        problems.append(f"{name} is mirrored now; delete it from "
                        f"{MANIFEST.relative_to(ROOT)}")

    print(f"{len(entries)} UI entries, {len(mirrors)} operations, "
          f"{len(claimed)} mirrored, {len(allowed)} in the backlog")
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
