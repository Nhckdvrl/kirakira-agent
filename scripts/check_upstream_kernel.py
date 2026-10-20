"""Verify the complete imported native runtime, not only its composition kernel."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def verify():
    pin = json.loads((ROOT / "UPSTREAM.json").read_text())
    for patch in pin.get("patches", {}).values():
        if hashlib.sha256((ROOT / patch["patch"]).read_bytes()).hexdigest() != patch["patch_sha256"]:
            raise RuntimeError("upstream patch artifact changed")
    failures = []
    for name, expected in pin["files"].items():
        allowed = {pin.get("patches", {}).get(name, {}).get("sha256", expected)}
        generated = pin.get("generated", {}).get(name)
        if generated is not None:
            if hashlib.sha256((ROOT / "package-lock.json").read_bytes()).hexdigest() != generated["lock_sha256"]:
                raise RuntimeError("generated artifact npm lock changed")
            allowed.add(generated["sha256"])
        path = ROOT / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() not in allowed:
            failures.append(name)
    if failures:
        raise RuntimeError("upstream source changed: " + ", ".join(failures))
    return len(pin["files"])

if __name__ == "__main__":
    print(f"{verify()} pinned native runtime files verified")
