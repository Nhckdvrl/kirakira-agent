"""Verify that the native composition kernel and Akasha2 remain byte-identical."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify():
    count = 0
    for directory, relative in (
        ("agent/plugin_composition", False),
        ("plugins/akasha2", True),
    ):
        base = ROOT / directory
        pin = json.loads((base / "UPSTREAM.json").read_text())
        for name, expected in pin["files"].items():
            path = (base if relative else ROOT) / name
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(
                    "upstream file changed: " + str(path.relative_to(ROOT))
                )
            count += 1
    return count


if __name__ == "__main__":
    print(f"{verify()} pinned upstream files verified")
