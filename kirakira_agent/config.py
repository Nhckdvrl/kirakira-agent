"""Kirakira Agent learning harness module."""

import os
from pathlib import Path
from typing import Dict


def load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError("Missing required environment variable: %s" % name)
    return value
