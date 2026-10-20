"""Remove generated files while preserving source, environments and runtime data."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTECTED_ROOTS = frozenset(
    {
        ".git",
        ".venv",
        "Reference",
        ".kirakira",
        "workspace",
        "sessions",
        "memory",
        "uploads",
        "mcp",
        "proactive",
        "drift",
        "observe",
        ".transcripts",
        "node_modules",
        "runtime",
        "plugin-data",
        "subagent-runs",
        "web-auth",
    }
)
CACHE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
TOOL_CACHES = frozenset({"pytest", "ruff", "mypy"})


def candidates(root: Path):
    tracked = set(
        subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z"], text=True
        ).split("\0")
    )
    for current, directories, files in os.walk(root):
        directory = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (directory / name).is_symlink()
            and not (directory == root and name in PROTECTED_ROOTS)
        ]
        if directory == root / ".cache":
            directories[:] = [name for name in directories if name in TOOL_CACHES]
        for name in tuple(directories):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            generated = (
                name in CACHE_NAMES
                or name.endswith(".egg-info")
                or (directory == root and name in {"build", "dist"})
                or (directory == root / ".cache" and name in TOOL_CACHES)
            )
            if generated:
                directories.remove(name)
                if not any(
                    p == relative or p.startswith(relative + "/") for p in tracked
                ):
                    yield path
        for name in files:
            path = directory / name
            if (
                path.suffix in {".pyc", ".pyo"}
                and not path.is_symlink()
                and path.relative_to(root).as_posix() not in tracked
            ):
                yield path


def remove_empty_directories(root: Path):
    visited = []
    for current, directories, _ in os.walk(root):
        directory = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (directory / name).is_symlink()
            and not (directory == root and name in PROTECTED_ROOTS)
        ]
        if directory == root / ".cache":
            directories[:] = [name for name in directories if name in TOOL_CACHES]
        visited.append(directory)
    removed = 0
    for directory in reversed(visited):
        if directory != root and not any(directory.iterdir()):
            directory.rmdir()
            removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="list generated paths without deleting"
    )
    args = parser.parse_args()
    paths = list(candidates(ROOT))
    size = 0
    failures = []
    for path in paths:
        if path.is_dir():
            for current, _, files in os.walk(path):
                size += sum(
                    p.stat().st_size
                    for name in files
                    if not (p := Path(current) / name).is_symlink()
                )
        else:
            size += path.stat().st_size
        if not args.dry_run:
            for attempt in range(4):
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                    break
                except FileNotFoundError:
                    break
                except OSError as error:
                    if error.errno == errno.ENOTEMPTY and attempt < 3:
                        time.sleep(0.05)
                        continue
                    failures.append(
                        {"path": str(path.relative_to(ROOT)), "error": str(error)}
                    )
                    break
    empty = 0 if args.dry_run else remove_empty_directories(ROOT)
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "paths": [str(p.relative_to(ROOT)) for p in paths],
                "bytes": size,
                "empty_directories": empty,
                "failures": failures,
            },
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
