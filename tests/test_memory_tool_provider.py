"""Production registration must respect ownership and a background read boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.memory.engine import (
    MemoryCapability,
    MemoryQueryResult,
    MemoryToolProfile,
    MemoryToolSpec,
)
from core.schema import ToolCall
from plugins.default_memory.contract import MemoryServices
from plugins.ledger.manager import SessionManager
from plugins.memory_tools.tools import register_memory_tools
from plugins.tools import build_workspace_registry
from plugins.tools.factory import build_tool_registry
from plugins.tools.registry import ToolRegistry


def engine():
    spec = MemoryToolSpec(
        description="recall",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    custom = MemoryToolSpec(
        name="reinforce_memory",
        description="reinforce",
        parameters={"type": "object", "properties": {}},
        risk="write",
        search_hint="correction",
    )
    return SimpleNamespace(
        DESCRIPTOR=SimpleNamespace(
            capabilities={MemoryCapability.RETRIEVE_CONTEXT_BLOCK}
        ),
        tool_profile=lambda: MemoryToolProfile(recall=spec, tools=(custom,)),
        query=AsyncMock(return_value=MemoryQueryResult(trace={"engine": "fixture"})),
    )


@pytest.mark.asyncio
async def test_workspace_provider_has_no_memory_tools_and_composition_preserves_engine_profile(
    tmp_path,
):
    workspace = build_workspace_registry(tmp_path)
    try:
        assert not {
            "memorize",
            "recall_memory",
            "forget_memory",
            "compact",
            "fetch_messages",
        } & set(workspace.names())
        assert {"bash", "read_file", "web_fetch", "web_search"} <= set(
            workspace.names()
        )
    finally:
        await workspace.shutdown()
    model = engine()
    tools = build_tool_registry(tmp_path, memory_services=MemoryServices(engine=model))
    try:
        assert "memorize" not in tools.names() and "forget_memory" not in tools.names()
        assert tools.get_tool("reinforce_memory").meta.risk == "write"
        assert tools.get_tool("reinforce_memory").meta.search_hint == "correction"
        tools.set_context(session_key="chat:one", channel="web", chat_id="one")
        result = await tools.execute_async(
            ToolCall("recall", "recall_memory", {"query": "previous decision"})
        )
        assert not result.is_error
        assert json.loads(result.content)["trace"]["engine"] == "fixture"
        assert model.query.await_args.args[0].scope.session_key == "chat:one"
    finally:
        await tools.shutdown()


@pytest.mark.asyncio
async def test_background_memory_is_read_only_and_resolves_original_messages(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("web:source")
    source = session.add_message("user", "deployment codename is kirakira-7319")
    session.add_message("assistant", "Understood")
    sessions.save(session)
    model = engine()
    tools = ToolRegistry()
    register_memory_tools(
        tools,
        session_manager=sessions,
        memory_services=MemoryServices(engine=model),
        read_only=True,
    )
    try:
        assert set(tools.names()) == {
            "recall_memory",
            "search_messages",
            "fetch_messages",
        }
        for name in tools.names():
            assert tools.get_tool(name).meta.risk == "read-only"
        result = await tools.execute_async(
            ToolCall(
                "fetch", "fetch_messages", {"source_ref": source["id"], "context": 1}
            )
        )
        assert not result.is_error and "kirakira-7319" in result.content
        result = await tools.execute_async(
            ToolCall("write", "memorize", {"content": "invented"})
        )
        assert result.is_error and "Unknown tool" in result.content
        assert len(sessions.get_or_create("web:source").messages) == 2
    finally:
        sessions.close()


def test_background_without_engine_does_not_advertise_unusable_legacy_memory():
    tools = ToolRegistry()
    register_memory_tools(tools, read_only=True)
    assert set(tools.names()) == {"search_messages", "fetch_messages"}
