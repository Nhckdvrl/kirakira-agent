"""Keep third_party/UPSTREAM.json honest after editing a pinned upstream file.

Every file this fork changes relative to the pinned upstream commit is recorded
twice: as a hunk inside a named patch artifact, and as the expected sha256 of
our modified file. `scripts/check_upstream_kernel.py` verifies both, so editing
a pinned file without updating them turns that gate red -- which is what it is
for.

Until now this was done by hand, so an ordinary fix to a pinned file left the
gate red until someone remembered -- `plugins/akasha/plugin.py` sat red from the
recall-decorator fix.

Two modes, deliberately separated, because they carry different risk:

* `--update-digests` records the current sha256 of each file whose recorded
  digest no longer matches. This is what an intentional edit to a pinned file
  needs, and it is exactly what the repository's gate checks.
* `--rebuild` additionally rewrites the patch artifacts. **Only 5 of the 13
  artifacts reproduce byte for byte** from the pinned sources with `diff -u`;
  the rest were produced by a procedure this repository never recorded, and
  rebuilding them would rewrite hundreds of KB to say the same thing (or
  silently lose hunks for files with no upstream original). So `--rebuild`
  refuses any artifact it cannot first reproduce, unless `--force` is given.

`--check` writes nothing and exits non-zero if any recorded digest is stale,
which is the form for CI.

It needs a checkout of the upstream repository to diff against, at any commit --
the pinned original of each file is read with `git show <pin>:<path>`, so the
checkout only has to contain that commit.

    python3 scripts/regenerate_upstream_patch.py <upstream-checkout> \
        --patch personal-assistant-experience.patch

With --all it rebuilds every artifact. --check writes nothing and exits
non-zero if any artifact or digest is stale, which is the form for CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "third_party/UPSTREAM.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pinned_original(upstream: Path, commit: str, path: str) -> bytes:
    """The file as upstream had it at the pinned commit.

    A file this fork added outright has no upstream original, so it diffs
    against an empty file, which is a valid patch.
    """
    done = subprocess.run(["git", "-C", str(upstream), "show", f"{commit}:{path}"],
                          capture_output=True, check=False)
    return done.stdout if done.returncode == 0 else b""


def build_patch(upstream: Path, commit: str, paths: list[str]) -> str:
    """Rebuild one artifact with GNU diff.

    It has to be `diff -u` and not difflib: the existing artifacts were produced
    with it, and the two pick different (equivalent) hunk boundaries, so difflib
    would rewrite all 700KB of patches to say the same thing. Checked against the
    five artifacts this fork has not touched -- they reproduce byte for byte.
    """
    chunks: list[str] = []
    for path in paths:
        current = ROOT / path
        if not current.is_file():
            raise SystemExit(f"patched file is missing from the tree: {path}")
        base = pinned_original(upstream, commit, path)
        with tempfile.TemporaryDirectory(prefix="upstream-patch-") as directory:
            original = os.path.join(directory, "original")
            with open(original, "wb") as stream:
                _ = stream.write(base)
            done = subprocess.run(
                ["diff", "-u", "--label", f"a/{path}", "--label", f"b/{path}",
                 original, str(current)],
                capture_output=True, check=False)
        if done.returncode not in (0, 1):
            raise SystemExit(f"diff failed for {path}: {done.stderr.decode(errors='replace')}")
        chunks.append(done.stdout.decode("utf-8", errors="surrogateescape"))
    return "".join(chunks)


def artifact_members(pin: dict[str, object]) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    patches = cast(dict[str, dict[str, str]], pin["patches"])
    for path, entry in patches.items():
        members.setdefault(entry["patch"], []).append(path)
    return {name: sorted(paths) for name, paths in members.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path, help="checkout containing the pinned commit")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if any recorded digest is stale")
    parser.add_argument("--update-digests", action="store_true",
                        help="record the current sha256 of every drifted file")
    parser.add_argument("--rebuild", action="append", default=[],
                        help="also rewrite this artifact, if it reproduces; repeatable")
    parser.add_argument("--force", action="store_true",
                        help="rewrite an artifact even if it does not reproduce")
    args = parser.parse_args()
    if not (args.check or args.update_digests or args.rebuild):
        parser.error("pass --check, --update-digests or --rebuild")

    pin = json.loads(PIN.read_text(encoding="utf-8"))
    commit = cast(str, pin["commit"])
    patches = cast(dict[str, dict[str, str]], pin["patches"])
    members = artifact_members(pin)

    drifted = [path for path in sorted(patches)
               if patches[path]["sha256"] != _digest((ROOT / path).read_bytes())]
    reproduces: dict[str, bool] = {}
    for artifact, paths in members.items():
        text = build_patch(args.upstream, commit, paths)
        reproduces[artifact] = _digest(text.encode("utf-8")) == _digest((ROOT / artifact).read_bytes())

    if args.check:
        for path in drifted:
            print(f"stale digest: {path}", file=sys.stderr)
        print(f"{len(drifted)} drifted file(s); "
              f"{sum(reproduces.values())}/{len(reproduces)} artifacts reproduce")
        return 1 if drifted else 0

    wanted = [name for name in sorted(members)
              if Path(name).name in {Path(value).name for value in args.rebuild}]
    if args.rebuild and not wanted:
        parser.error(f"no such artifact; known: {sorted(Path(n).name for n in members)}")
    for artifact in wanted:
        if not reproduces[artifact] and not args.force:
            print(f"refusing to rewrite {artifact}: it does not reproduce from the pinned "
                  f"sources, so rewriting it would not be a faithful record (use --force)",
                  file=sys.stderr)
            continue
        text = build_patch(args.upstream, commit, members[artifact])
        (ROOT / artifact).write_text(text, encoding="utf-8")
        digest = _digest(text.encode("utf-8"))
        for path in members[artifact]:
            patches[path]["patch_sha256"] = digest
        print(f"rewrote {artifact} ({len(members[artifact])} files, {len(text)} bytes)")

    if args.update_digests:
        for path in drifted:
            patches[path]["sha256"] = _digest((ROOT / path).read_bytes())
            print(f"recorded new digest for {path}")
        if not drifted:
            print("no drifted files")
    PIN.write_text(json.dumps(pin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {PIN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
