"""Small distribution facade over Kirakira's formal PluginManager and Root.

This module selects plugins and offers a Python entry point. Task admission,
source policy, persistence, tool execution and recovery live in native providers.
"""
from contextlib import asynccontextmanager, closing
from pathlib import Path

from agent.config_models import Config
from agent.plugins.bundles import set_plugin_choice
from agent.plugins.selection import PluginSelection
from agent.plugins.static_manifest import load_static_plugin_manifest
from bootstrap.tools import build_core_runtime
from bootstrap.workspace_lock import WorkspaceInstanceLock
from core.net.http import (
    SharedHttpResources,
    clear_default_shared_http_resources,
    configure_default_shared_http_resources,
)

ROOT = Path(__file__).resolve().parents[1]
HEADLESS = frozenset({
    "ledger", "timer", "channels", "gateway", "host_execution", "workloads",
    "managed_processes", "mcp", "commands", "projects", "models", "content",
    "context", "turn_projection", "sources", "tools", "tool_search", "assets",
    "working_directory", "vision", "skills", "shell", "files", "runtime_inspection",
    "runtime_diagnostics", "plugin_update", "local-embedding", "event-ingress",
    "prompt", "compaction", "markdown_memory", "react", "conversation",
    "reply_program", "reply", "jobs", "eventmail", "delivery", "delivery_policy",
    "message_push", "programmatic", "akashic_sender", "scheduler", "subagent", "drift", "wake",
    "ledger_invariants", "akasha", "standard_web", "web_fetch_http", "web_search_exa",
    "openai-compatible", "step-plan",
})


def initialize_workspace(workspace, *, profile="headless"):
    if profile not in {"headless", "web"}:
        raise ValueError("unknown distribution profile")
    workspace = Path(workspace).resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("initialize requires a new empty workspace; existing data must be migrated explicitly")
    workspace.mkdir(parents=True, exist_ok=True)
    from agent.migrations.runner import initialize_empty_workspace
    initialize_empty_workspace(repo_root=ROOT, workspace=workspace,
                               config_path=workspace / "config.toml")
    PluginSelection(workspace).initialize()
    selected = HEADLESS if profile == "headless" else HEADLESS | {
        "ui", "web-auth", "shell-ui", "conversation-ui", "onboarding", "akashic_clients",
        "akashic_sender", "session_title", "visuals", "terminal", "workspace_changes",
        "sites", "sites_tab", "standard_web", "web_fetch_http", "web_search_exa",
    }
    for directory in sorted((ROOT / "plugins").iterdir()):
        if (directory / "plugin.py").is_file():
            manifest = load_static_plugin_manifest(directory)
            set_plugin_choice(workspace, manifest.name, enabled=manifest.name in selected)
    from agent.plugin_composition.config_input import save_config
    save_config(workspace / "plugin-data/context-builtin", {
        "prompt_sources": {"default_prompt": "prompt", "skills": "skills",
                           "markdown_memory": "markdown_memory"},
        "summary_source": ["compaction", "compaction"],
    })
    import json
    (workspace / "config.toml").write_text("[runtime]\nworkspace = " + json.dumps(str(workspace)) + "\n")
    return workspace


@asynccontextmanager
async def runtime(workspace, *, extensions=()):
    from .environment import configure_plugin_home, select_host_python
    select_host_python()
    configure_plugin_home()
    workspace = Path(workspace).resolve()
    # Reject an old database before a native provider can acquire resources.
    import sqlite3
    database = workspace / "sessions.db"
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            legacy_log = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_messages'").fetchone()
        if legacy_log is not None or "role" in columns or "content" in columns:
            raise ValueError("legacy workspace requires explicit import into a separate directory")
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    http = SharedHttpResources()
    configure_default_shared_http_resources(http)
    core = None
    try:
        core = build_core_runtime(Config(workspace_path=workspace), workspace, http,
                                  plugin_dirs=(ROOT / "plugins", *extensions))
        await core.start()
        from agent.plugin_composition.model import FiberState
        failed = [fiber for fiber in core.plugin_manager.live_root.fibers()
                  if fiber.state is FiberState.FAILED]
        if failed:
            raise RuntimeError("native provider failed: " + "; ".join(
                fiber.name + ": " + str(fiber.error) for fiber in failed))
        yield core
    finally:
        try:
            if core is not None:
                await core.stop()
            await http.aclose()
        finally:
            clear_default_shared_http_resources(http)
            lock.release()


async def rpc(core, method, payload):
    from plugins.gateway.contract import RpcMethod
    root = core.plugin_manager.live_root
    operation = root.context.require(RpcMethod.key(method))
    result = await operation.invoke(operation.params.model_validate(payload), None)
    if method == "models/command" and isinstance(result, dict) and result.get("status", 200) >= 400:
        raise RuntimeError("Native model command rejected: " + str(result.get("body")))
    return result


async def configure_chat_model(
    core, key_file, *, model, endpoint, driver_id="openai-compatible",
    model_id="main", connection_id="main", context_window=32768,
    max_output_tokens=4096, reasoning_effort=None,
):
    """Configure a chat role through the provider-independent Models contract."""
    from plugins.models.contract import MODEL_CATALOG

    if not model.strip() or not endpoint.strip() or context_window <= max_output_tokens or max_output_tokens <= 0:
        raise ValueError("model, endpoint and a valid context/output budget are required")
    root = core.plugin_manager.live_root
    if root is None:
        raise RuntimeError("runtime is not ready")
    catalog = root.context.require(MODEL_CATALOG)
    snapshot = catalog.snapshot()
    existing = next((item for item in snapshot.models if item.model_id == model_id), None)
    if existing is not None:
        if (existing.kind != "chat" or existing.model != model
                or snapshot.connection(existing.connection_id).driver_id != driver_id):
            raise ValueError("model identity already belongs to another model or driver")
    else:
        capabilities = {"context_window": context_window, "max_output_tokens": max_output_tokens,
                        "supports_tool_calls": True, "supports_parallel_tool_calls": True}
        sources = {"context_window": "user", "max_output_tokens": "user",
                   "tool_calls": "driver", "parallel_tool_calls": "driver"}
        if reasoning_effort is not None:
            capabilities["supported_reasoning_efforts"] = ["low", "medium", "high"]
            sources["reasoning_efforts"] = "user"
        await rpc(core, "models/command", {
            "type": "create_connection_with_model",
            "connection": {
                "expected_revision": snapshot.revision, "connection_id": connection_id,
                "name": connection_id, "driver_id": driver_id, "endpoint": endpoint,
                "auth_identity": connection_id + "-key",
                "credential": {"api_key": Path(key_file).read_text().strip()},
                "driver_config": {"format_version": 1, "max_attempts": 3, "read_timeout": 180.0},
            },
            "model": {
                "expected_revision": snapshot.revision, "model_id": model_id,
                "connection_id": connection_id, "kind": "chat", "model": model,
                "default_reasoning_effort": reasoning_effort,
                "capabilities": capabilities, "capability_sources": sources,
            },
        })
    current = catalog.snapshot()
    if current.role_bindings.get("default") != model_id:
        await rpc(core, "models/command", {"type": "set_default", "expected_revision": current.revision,
                                           "role": "default", "model_id": model_id})


async def configure_step_plan(core, key_file, *, reasoning_effort="low", max_output_tokens=4096):
    """Optional subscription driver; the execution and memory contracts stay generic."""
    from plugins.step_plan.driver import BASE_URL, DRIVER_ID, MODEL

    await configure_chat_model(core, key_file, model=MODEL, endpoint=BASE_URL,
        driver_id=DRIVER_ID, model_id="step5", connection_id="step-plan",
        max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort)


async def submit(core, session_id, message_id, text, *, timeout=180, metadata=None, attachments=()):
    import asyncio
    from datetime import UTC, datetime

    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    from plugins.ledger.contract import MESSAGE_CATALOG, Control, Output
    root = core.plugin_manager.live_root
    reader = root.context.require(MESSAGE_CATALOG).reader(session_id)
    if not session_id.startswith("akashic:"):
        raise ValueError("conversation sessions must use the native akashic: address")
    accepted = await root.context.require(CHANNEL_INPUT_V2)(
        session_id, message_id,
        ChannelInboundMessage("akashic", "user", session_id.removeprefix("akashic:"), text,
                              datetime.now(UTC), metadata or {}, attachments),
    )

    async def completed():
        async for _ in reader.follow_heads():
            for message in reader.snapshot(after_seq=accepted.seq):
                if message.source != "conversation":
                    continue
                if isinstance(message.body, Output) and message.body.finish != "continue":
                    return message
                if isinstance(message.body, Control) and message.body.action in {"failure", "pause"}:
                    raise RuntimeError(f"native reply {message.body.action}: {message.body.reason}")
    return await asyncio.wait_for(completed(), timeout)
