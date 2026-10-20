"""Compare pinned source origins against a real, fixed Akashic Git checkout.

This checks source coverage. Runtime activation and acceptance remain separate
columns in docs/status/akashic-gap.md; a matching hash never proves deployment.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from scripts.check_upstream_kernel import ROOT, verify

SCOPES = (
    "agent/",
    "bootstrap/",
    "core/",
    "plugins/",
    "infra/",
    "utils/",
    "frontend/",
    "packages/",
    "sdk/",
    "schema/",
    "assets/",
    "bundles/",
    "migrations/",
    "types/",
)
ROOT_FILES = {
    "main.py",
    "package.json",
    "package-lock.json",
    "components.json",
    "tsconfig.json",
    "eslint.config.js",
}


def compare(reference):
    reference = Path(reference).resolve()
    pin = json.loads((ROOT / "UPSTREAM.json").read_text())
    revision = subprocess.check_output(
        ["git", "-C", str(reference), "rev-parse", "HEAD"], text=True
    ).strip()
    names = subprocess.check_output(
        ["git", "-C", str(reference), "ls-tree", "-r", "--name-only", "HEAD"], text=True
    ).splitlines()
    imported = {pin["source_paths"].get(name, name): name for name in pin["files"]}
    excluded = pin.get("excluded", {})
    expected = {name for name in names if (name.startswith(SCOPES) or name in ROOT_FILES)
                and name not in excluded}
    different = [
        name
        for name, target in imported.items()
        if not (reference / name).is_file()
        or hashlib.sha256((reference / name).read_bytes()).hexdigest()
        != pin["files"][target]
    ]
    missing = sorted(expected - imported.keys())
    # Deployment and benchmark trees are deliberately outside the Runtime pin.
    # Report them explicitly rather than silently defining all differences away.
    outside = {
        prefix: sum(name.startswith(prefix) for name in names)
        for prefix in ("docker/", "systemd/", "benchmark/", "private_runtime", "scripts/", "tests/", "skills/", "docs/", "_handbook/", ".github/")
    }
    return {
        "reference_commit": revision,
        "pinned_commit": pin["commit"],
        "same_revision": revision == pin["commit"],
        "verified_local_pins": verify(),
        "unique_imported_origins": len(imported),
        "expected_runtime_origins": len(expected),
        "missing_runtime_files": missing,
        "different_origin_bytes": different,
        "recorded_local_patches": sorted(pin.get("patches", {})),
        "recorded_generated_assets": sorted(pin.get("generated", {})),
        "outside_runtime_pin": outside,
        "excluded_non_runtime_files": excluded,
        "local_plugins": ["step-plan", "local-embedding", "event-ingress"],
        "boundary": "source comparison only; activation, accounts, external devices and functional evidence are separate",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    value = compare(args.reference)
    if args.write:
        (ROOT / "docs/status/upstream-diff.json").write_text(
            json.dumps(value, indent=2) + "\n"
        )
    print(json.dumps(value, indent=2))
    raise SystemExit(
        0
        if value["same_revision"]
        and not value["missing_runtime_files"]
        and not value["different_origin_bytes"]
        else 1
    )


if __name__ == "__main__":
    main()
