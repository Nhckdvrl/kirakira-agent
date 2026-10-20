"""Choose the active virtualenv before the native host freezes its environment."""

import os
import sys
from pathlib import Path


def configure_plugin_home():
    """CLI and embedded Runtime share the same default plugin artifact home."""
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", os.environ.get(
        "KIRAKIRA_PLUGIN_HOME", str(Path.home() / ".kirakira/plugins")))


def select_host_python():
    if sys.prefix == sys.base_prefix:
        return
    directory = str(Path(sys.executable).parent)
    entries = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [directory, *(p for p in entries if p != directory)]
    )
