"""Akashic-compatible plugin descriptor parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
MANIFEST_PATH = Path(".aka-plugin") / "plugin.json"


@dataclass(frozen=True)
class PluginDescriptor:
    name: str
    version: str
    description: str
    root: Path
    manifest: Dict[str, Any] = field(default_factory=dict)
    lifecycle_entry: Optional[Path] = None
    lifecycle_class: str = ""
    skill_roots: Tuple[Path, ...] = ()
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def load_plugin_descriptor(root: Path) -> Optional[PluginDescriptor]:
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Invalid plugin manifest %s: %s" % (manifest_path, exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError("Plugin manifest must be a JSON object: %s" % manifest_path)
    name = str(raw.get("name") or root.name).strip()
    if not name:
        raise ValueError("Plugin manifest has no name: %s" % manifest_path)
    paths = _dict(raw.get("paths"))
    akashic = _dict(raw.get("akashic"))
    lifecycle = _dict(akashic.get("lifecycle"))
    entry_text = str(lifecycle.get("entry") or "").strip()
    entry = _safe_child(root, entry_text) if entry_text else None
    if entry is not None and not entry.is_file():
        raise ValueError("Plugin lifecycle entry does not exist: %s" % entry)
    skill_roots = tuple(
        path
        for item in _strings(paths.get("skills"))
        for path in [_safe_child(root, item)]
        if path.is_dir()
    )
    return PluginDescriptor(
        name=name,
        version=str(raw.get("version") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        root=root.resolve(),
        manifest=raw,
        lifecycle_entry=entry,
        lifecycle_class=str(lifecycle.get("class") or "").strip(),
        skill_roots=skill_roots,
        mcp_servers=_load_mcp_servers(root, paths.get("mcp_servers")),
    )


def discover_plugin_roots(plugin_dirs: List[Path]) -> List[Path]:
    roots: List[Path] = []
    seen: set[Path] = set()
    for declared in plugin_dirs:
        candidates = [declared] if _is_plugin_root(declared) else []
        if declared.is_dir() and not candidates:
            candidates = [child for child in sorted(declared.iterdir()) if _is_plugin_root(child)]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                roots.append(resolved)
    return roots


def _load_mcp_servers(root: Path, value: object) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for item in _strings(value):
        config_path = _safe_child(root, item)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Invalid plugin MCP config %s: %s" % (config_path, exc)) from exc
        servers = _dict(payload.get("servers") if isinstance(payload, dict) else None)
        for name, raw_server in servers.items():
            server = _dict(raw_server)
            command = _strings(server.get("command"))
            if not command:
                raise ValueError("Plugin MCP server %s has no command" % name)
            normalized = [
                _normalize_command_item(root, argument) for argument in command
            ]
            cwd_text = str(server.get("cwd") or "").strip()
            cwd = str(_safe_child(root, cwd_text)) if cwd_text else str(root.resolve())
            env = {str(key): str(val) for key, val in _dict(server.get("env")).items()}
            merged[str(name)] = {"command": normalized, "cwd": cwd, "env": env}
    return merged


def _normalize_command_item(root: Path, value: str) -> str:
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    if "/" not in value and "\\" not in value and not value.startswith("."):
        return value
    return str(_safe_child(root, value))


def _safe_child(root: Path, value: str) -> Path:
    target = (root / value).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Plugin path escapes root: %s" % value) from exc
    return target


def _is_plugin_root(path: Path) -> bool:
    return path.is_dir() and ((path / "plugin.py").is_file() or (path / MANIFEST_PATH).is_file())


def _dict(value: object) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: object) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
