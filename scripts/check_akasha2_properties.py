"""Run pinned upstream mathematical properties against Kirakira's cycle.

The upstream CLI/schema determinism test is excluded: Kirakira's host schema
and deterministic checkpoint experiment have their own verification. Three
container-only edits preserve numeric assertions for the compact array owner.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REVISION = "e85dc2c66ff4eb16af118c638d72e7c32f3dc69a"
SOURCE_SHA256 = "ff2efc943f6c82bbfc11dca32fdca6dda497170fec265beabbfecf62e1512463"
URL = f"https://raw.githubusercontent.com/kachofugetsu09/akasha-v2-engine/{REVISION}/tests/unit/test_memory_engine.py"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, help="optional pinned upstream test file"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("use a new output directory")
    args.output.mkdir(parents=True)
    source = (
        args.source.read_bytes()
        if args.source
        else urllib.request.urlopen(URL, timeout=30).read()
    )
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("upstream property file does not match the pinned source")
    transformed = source.decode()
    replacements = {
        "from akasha.domain.": "from plugins.akasha2.domain.",
        "assert graph.weight == []": "assert list(graph.weight) == []",
        "graph.last_support_seconds.copy()": "np.asarray(graph.last_support_seconds).copy()",
        "graph.support_credit.copy()": "np.asarray(graph.support_credit).copy()",
    }
    for old, new in replacements.items():
        if old not in transformed:
            raise ValueError("upstream property source changed: " + old)
        transformed = transformed.replace(old, new)
    (args.output / "manifest.json").write_text(
        json.dumps(
            {
                "upstream": URL,
                "upstream_revision": REVISION,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "transformed_sha256": hashlib.sha256(transformed.encode()).hexdigest(),
                "replacements": replacements,
                "python": sys.version,
                "excluded": "test_rebuild_database_is_independent_of_python_hash_seed",
                "reason": "host CLI/schema differs; independent cycle checkpoint/hash-seed experiment replaces it",
                "runtime_source_sha256": {
                    str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(Path("plugins/akasha2").rglob("*.py"))
                },
            },
            indent=2,
        )
    )
    with tempfile.TemporaryDirectory(prefix="kirakira-math-") as temporary:
        root = Path(temporary)
        path = root / "test_engine.py"
        path.write_text(transformed)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                f"--rootdir={root}",
                f"--confcutdir={root}",
                str(path),
                "-k",
                "not rebuild_database_is_independent_of_python_hash_seed",
            ],
            capture_output=True,
            text=True,
        )
    (args.output / "pytest.txt").write_text(result.stdout + result.stderr)
    print(result.stdout, end="")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
