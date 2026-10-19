"""Functional Project/directory parity over real SQLite, filesystem and pipeline."""

import asyncio
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from bootstrap.runtime import build_runtime
from core.schema import ModelResponse, ToolCall
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChannel
from plugins.control.client import ControlClient, RemoteControlError
from plugins.dashboard.service import DashboardService
from plugins.ledger.manager import SessionManager
from plugins.ledger.owner_store import StateConflict
from plugins.projects.factory import build_workspace_services
from plugins.working_directory.service import read_agents


def identity():
    return "p_" + uuid4().hex


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    sessions = SessionManager(root)
    services = build_workspace_services(sessions)
    try:
        yield root, sessions, services
    finally:
        sessions.close()


def test_project_defaults_are_snapshotted_once_and_old_sessions_never_backfill(
    workspace, tmp_path
):
    root, sessions, services = workspace
    project = identity()
    services.projects.create(project, "One")
    first = sessions.get_or_create("first", metadata={"project_id": project})
    assert services.directories.snapshot(first.key).path is None
    code = tmp_path / "code"
    code.mkdir()
    services.projects.bind_directory(project, str(code))
    second = sessions.get_or_create("second", metadata={"project_id": project})
    assert services.directories.snapshot(first.key).path is None
    assert services.directories.snapshot(second.key).path == str(code)
    services.projects.update(project, name="Renamed", archived=True)
    assert sessions.get_or_create("second") is second
    with pytest.raises(ValueError, match="archived"):
        sessions.get_or_create("rejected", metadata={"project_id": project})
    assert not sessions.session_exists("rejected")
    assert services.directories.snapshot("rejected").revision is None
    assert services.projects.list()[0]["directory"] == str(code)
    with pytest.raises(ValueError, match="cannot change"):
        sessions.get_or_create("first", metadata={"project_id": identity()})


def test_creation_rollback_includes_directory_owner_and_does_not_cache_partial_session(
    workspace,
):
    _, sessions, services = workspace

    def fail(session, tx):
        raise RuntimeError("later initializer failed")

    sessions.register_initializer("failure", fail)
    with pytest.raises(RuntimeError, match="initializer"):
        sessions.get_or_create("rollback")
    assert not sessions.session_exists("rollback")
    assert services.directories.snapshot("rollback").revision is None


def test_legacy_sessions_stay_unset_and_messages_survive_owner_upgrade(tmp_path):
    sessions = SessionManager(tmp_path)
    old = sessions.get_or_create("old")
    old.add_message("user", "raw fact")
    old.add_message("assistant", "original answer")
    sessions.save(old)
    original = json.dumps(old.messages, sort_keys=True)
    sessions.close()
    sessions = SessionManager(tmp_path)
    services = build_workspace_services(sessions)
    try:
        assert services.directories.snapshot("old").revision is None
        assert (
            json.dumps(sessions.get_or_create("old").messages, sort_keys=True)
            == original
        )
        assert services.directories.snapshot("old").revision is None
    finally:
        sessions.close()


def test_project_receipts_preserve_initial_intent_and_directory_binding_is_one_winner(
    workspace, tmp_path
):
    _, _, services = workspace
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    project = identity()
    original = services.projects.create(project, "Original", str(a))
    services.projects.update(project, name="Renamed", archived=True)
    a.rmdir()
    replay = services.projects.create(project, "Original", str(a))
    assert (
        replay["created_at"] == original["created_at"] and replay["name"] == "Renamed"
    )
    with pytest.raises(StateConflict):
        services.projects.create(project, "Different", str(a))
    project = identity()
    services.projects.create(project, "Concurrent")
    a.mkdir()

    def bind(path):
        try:
            return services.projects.bind_directory(project, str(path))["directory"]
        except StateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, (a, b)))
    assert results.count("conflict") == 1
    winner = next(value for value in results if value != "conflict")
    assert services.projects.bind_directory(project, winner)["directory"] == winner


def test_switch_receipt_cas_independent_sessions_and_missing_directory(
    workspace, tmp_path
):
    _, sessions, services = workspace
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sessions.get_or_create("a")
    sessions.get_or_create("b")
    before = services.directories.snapshot("a")
    effect = services.directories.switch(
        "a", str(a), receipt_key="one", expected_version=before.revision
    )
    assert services.directories.snapshot("b").path is None
    with pytest.raises(StateConflict):
        services.directories.switch(
            "a", str(b), receipt_key="stale", expected_version=before.revision
        )
    # Explicit null means "still absent", not "skip the revision check".
    with pytest.raises(StateConflict):
        services.directories.switch(
            "a", str(b), receipt_key="stale-absent", expected_version=None
        )
    a.rmdir()
    assert services.directories.switch("a", str(a), receipt_key="one") == effect
    with pytest.raises((ValueError, OSError)):
        services.directories.resolve_target("a", "relative/file")
    assert not a.exists()
    target, dependency = services.directories.resolve_target(
        "a", str(b / "absolute.txt")
    )
    assert target == b / "absolute.txt" and dependency is None
    assert services.directories.current_info("a")["status"] == "unavailable"


def test_deleted_session_can_recreate_without_old_directory_or_receipts(
    workspace, tmp_path
):
    _, sessions, services = workspace
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    project = identity()
    services.projects.create(project, "Kept project", str(a))
    sessions.get_or_create("deleted", metadata={"project_id": project})
    sessions.get_or_create("other", metadata={"project_id": project})
    services.directories.switch("deleted", str(b), receipt_key="deleted-effect")
    services.directories.switch("other", str(b), receipt_key="other-effect")
    assert sessions.delete_session("deleted")
    assert services.directories.snapshot("deleted").revision is None
    assert (
        services.directories.store.read("working_directory", "switch:deleted-effect")
        is None
    )
    assert services.directories.snapshot("other").path == str(b)
    assert (
        services.directories.store.read("working_directory", "switch:other-effect")
        is not None
    )
    assert services.projects.list()[0]["id"] == project
    sessions.get_or_create("deleted", metadata={"project_id": project})
    assert services.directories.snapshot("deleted").path == str(a)


def test_later_delete_owner_failure_rolls_back_session_messages_and_directory(
    workspace,
):
    _, sessions, services = workspace
    session = sessions.get_or_create("delete-rollback")
    session.add_message("user", "retained original")
    sessions.save(session)

    def fail(key, tx):
        raise RuntimeError("delete owner failed")

    sessions.register_initializer(
        "late-owner", lambda session, tx: None, on_delete=fail
    )
    with pytest.raises(RuntimeError, match="delete owner failed"):
        sessions.delete_session(session.key)
    restored = sessions.get_or_create(session.key)
    assert restored.messages[0]["content"] == "retained original"
    assert services.directories.snapshot(session.key).revision == 0


def test_repository_rules_are_live_nearest_root_override_bounded_and_all_or_nothing(
    tmp_path,
):
    root, nested = tmp_path / "repo", tmp_path / "repo/sub"
    nested.mkdir(parents=True)
    (root / ".git").write_text("gitdir: fixture")
    (tmp_path / "AGENTS.md").write_text("outside repository")
    (root / "AGENTS.md").write_text("ROOT_RULE")
    (nested / "AGENTS.md").write_text("ignored normal rule")
    override = nested / "AGENTS.override.md"
    override.write_text("OVERRIDE_RULE")
    rules = read_agents(str(nested))
    assert [row["text"] for row in rules["files"]] == ["ROOT_RULE", "OVERRIDE_RULE"]
    override.unlink()
    assert read_agents(str(nested))["files"][-1]["text"] == "ignored normal rule"
    override.write_bytes(b"x" * 32768)
    rules = read_agents(str(nested))
    assert rules["status"] == "unavailable" and rules["files"] == []


async def runtime_at(root, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "fixture")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://example.test/v1")
    root.mkdir(exist_ok=True)
    (root / "config.toml").write_text("[memory]\nenabled=false\n")
    return await build_runtime(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_window", [0, 100_000])
async def test_pipeline_rejects_mixed_switch_then_refreshes_rules_and_reads_new_directory(
    tmp_path, monkeypatch, context_window
):
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "AGENTS.md").write_text("OLD_DIRECTORY_RULE_713")
    (b / "AGENTS.md").write_text("NEW_DIRECTORY_RULE_814")
    (b / "value.txt").write_text("B_VALUE")
    runtime.session_manager.get_or_create("verify:directory")
    runtime.working_directories.switch("verify:directory", str(a), receipt_key="setup")
    runtime.reasoner.config.context_window = context_window
    calls = []

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                assert "OLD_DIRECTORY_RULE_713" in messages[0]["content"]
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "bad-switch", "set_working_directory", {"path": str(b)}
                        ),
                        ToolCall(
                            "bad-write",
                            "write_file",
                            {"path": "must-not-exist", "content": "bad"},
                        ),
                    ]
                )
            if len(calls) == 2:
                assert all(m.get("is_error") for m in messages[-2:])
                return ModelResponse(
                    tool_calls=[
                        ToolCall("switch", "set_working_directory", {"path": str(b)})
                    ]
                )
            if len(calls) == 3:
                assert "NEW_DIRECTORY_RULE_814" in messages[0]["content"]
                assert "OLD_DIRECTORY_RULE_713" not in messages[0]["content"]
                return ModelResponse(
                    tool_calls=[ToolCall("read", "read_file", {"path": "value.txt"})]
                )
            assert messages[-1]["content"] == "B_VALUE"
            return ModelResponse(text="done")

    runtime.reasoner._model_io.model_client = Model()
    try:
        result = await runtime.process_direct(
            "switch and read",
            session_key="verify:directory",
            skip_memory_retrieval=True,
            skip_post_memory=True,
        )
        assert result.content == "done" and len(calls) == 4
        assert (
            not (a / "must-not-exist").exists() and not (b / "must-not-exist").exists()
        )
        history = json.dumps(
            runtime.session_manager.get_or_create("verify:directory").messages
        )
        assert (
            "OLD_DIRECTORY_RULE_713" not in history
            and "NEW_DIRECTORY_RULE_814" not in history
        )
        runs = runtime.reasoner.execution_store.recent()
        assert runtime.reasoner.execution_store.unresolved(runs[0]["run_id"]) == []
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_control_project_thread_creation_and_directory_survive_restart(
    tmp_path, monkeypatch
):
    work, code = tmp_path / "runtime", tmp_path / "code"
    code.mkdir()
    runtime = await runtime_at(work, monkeypatch)
    project = identity()
    service = runtime.control_service
    created = service.workspace_call(
        "project/create",
        {"projectId": project, "name": "Real project", "directory": str(code)},
    )
    record = service.start_thread({"project_id": project})
    thread = record["id"]
    try:
        assert runtime.working_directories.snapshot(thread).path == str(code)
        assert created["directory"] == str(code)
    finally:
        await runtime.stop_background([])
    runtime = await build_runtime(work)
    try:
        info = runtime.control_service.workspace_call(
            "directory/current", {"threadId": thread}
        )
        assert info["path"] == str(code) and info["revision"] == 0
        assert (
            runtime.control_service.workspace_call("project/list", {})["items"][0]["id"]
            == project
        )
    finally:
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_shell_keeps_process_directory_and_control_switch_waits_for_turn_owner(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    key = runtime.control_service.start_thread({})["id"]
    runtime.working_directories.switch(key, str(a), receipt_key="setup")
    token = runtime.tools.set_context(session_key=key)
    try:
        response = await runtime.tools.execute_async(
            ToolCall("pwd", "bash", {"command": "pwd", "login": False})
        )
        assert str(a) in response.content
        async with runtime.pipeline.admission.hold(key):
            switch = asyncio.create_task(
                runtime.control_service.set_directory(
                    {
                        "threadId": key,
                        "path": str(b),
                        "requestId": "switch",
                        "expectedRevision": 1,
                    }
                )
            )
            await asyncio.sleep(0)
            assert not switch.done()
            assert runtime.working_directories.snapshot(key).path == str(a)
        await switch
        response = await runtime.tools.execute_async(
            ToolCall("pwd2", "bash", {"command": "pwd", "login": False})
        )
        assert str(b) in response.content
    finally:
        runtime.tools.reset_context(token)
        await runtime.stop_background([])


def test_concurrent_new_session_has_one_creation_and_one_cached_identity(workspace):
    _, sessions, services = workspace
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda _: sessions.get_or_create("shared"), range(32)))
    assert all(session is created[0] for session in created)
    assert services.directories.snapshot("shared").revision == 0


@pytest.mark.asyncio
async def test_subagent_inherits_directory_and_switches_only_its_own_session(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    runtime.session_manager.get_or_create("parent")
    runtime.working_directories.switch("parent", str(a), receipt_key="parent-setup")
    children = []

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            child = runtime.tools.context["session_key"]
            children.append(child)
            if len(children) == 1:
                assert runtime.working_directories.snapshot(child).path == str(a)
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "child-switch", "set_working_directory", {"path": str(b)}
                        )
                    ]
                )
            assert runtime.working_directories.snapshot(child).path == str(b)
            return ModelResponse(text="child done")

    runtime.reasoner._model_io.model_client = Model()
    token = runtime.tools.set_context(
        session_key="parent", channel="test", chat_id="parent"
    )
    try:
        result = json.loads(
            await runtime.subagents.spawn("switch only the child", profile="general")
        )
        assert result["status"] == "completed" and len(children) == 2
        assert runtime.working_directories.snapshot("parent").path == str(a)
        assert runtime.working_directories.snapshot(result["session_key"]).path == str(
            b
        )
    finally:
        runtime.tools.reset_context(token)
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_real_socket_project_directory_protocol_is_validated_and_restartable(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    code = tmp_path / "code"
    code.mkdir()
    await runtime.control_server.start()
    client = await ControlClient.connect(str(runtime.control_server.endpoint))
    project = identity()
    try:
        await client.request(
            "project/create",
            {"projectId": project, "name": "Socket project", "directory": str(code)},
        )
        record = await client.request(
            "thread/start", {"metadata": {"project_id": project}}
        )
        info = await client.request("directory/current", {"threadId": record["id"]})
        assert info["path"] == str(code)
        with pytest.raises(RemoteControlError) as error:
            await client.request(
                "directory/set",
                {
                    "threadId": record["id"],
                    "path": "relative",
                    "requestId": "bad",
                    "expectedRevision": -1,
                },
            )
        assert error.value.code == -32602
    finally:
        await client.close()
        await runtime.stop_background([])


@pytest.mark.asyncio
async def test_web_project_entry_uses_the_same_owner_and_routes_chat_to_project_directory(
    tmp_path, monkeypatch
):
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    code = tmp_path / "code"
    code.mkdir()
    project = identity()
    board = DashboardService(
        runtime.session_manager.workspace,
        session_manager=runtime.session_manager,
        extra={"workspace_control": runtime.control_service},
    )
    web = WebChannel(port=0, dashboard=board)
    context = ChannelContext(
        runtime.bus,
        runtime.session_manager,
        runtime.event_bus,
        runtime.session_manager.workspace,
        logging.getLogger("workspace-web-test"),
    )
    await web.start(context)
    tasks = await runtime.start_background(start_channels=False)

    class Model:
        async def acomplete(self, messages, *args, **kwargs):
            assert str(code) in messages[0]["content"]
            if messages[-1]["role"] == "user":
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "write",
                            "write_file",
                            {"path": "from-web.txt", "content": "project file"},
                        )
                    ]
                )
            return ModelResponse(text="saved")

    runtime.reasoner._model_io.model_client = Model()
    url = f"http://127.0.0.1:{web._server.server_port}"
    try:
        async with httpx.AsyncClient(base_url=url, timeout=15) as client:
            page = await client.get("/workspace")
            assert page.status_code == 200 and "项目与会话工作目录" in page.text
            response = await client.post(
                "/api/workspace",
                json={
                    "method": "project/create",
                    "params": {
                        "projectId": project,
                        "name": "Web project",
                        "directory": str(code),
                    },
                },
            )
            assert response.status_code == 200, response.text
            invalid = await client.post(
                "/api/workspace",
                headers={"Origin": "https://another.example"},
                json={"method": "project/list"},
            )
            assert invalid.status_code == 400
            result = await client.post(
                "/message",
                json={
                    "session_id": "project-chat",
                    "project_id": project,
                    "text": "write the file",
                },
            )
            assert result.status_code == 200 and result.json()["content"] == "saved", (
                result.text
            )
            assert (code / "from-web.txt").read_text() == "project file"
            assert not (runtime.session_manager.workspace / "from-web.txt").exists()
            key = result.json()["session_id"]
            assert (
                runtime.session_manager.get_or_create(key).metadata["project_id"]
                == project
            )
            deleted = await client.delete("/api/session", params={"key": key})
            assert deleted.status_code == 200
            assert runtime.working_directories.snapshot(key).revision is None
            recreated = await client.post(
                "/message",
                json={
                    "session_id": "project-chat",
                    "project_id": project,
                    "text": "write again after deleting the previous session",
                },
            )
            assert (
                recreated.status_code == 200 and recreated.json()["content"] == "saved"
            )
            assert runtime.working_directories.snapshot(key).path == str(code)
    finally:
        await web.stop()
        await runtime.stop_background(tasks)


@pytest.mark.asyncio
async def test_actual_git_worktree_and_running_pty_keep_their_own_directories(
    tmp_path, monkeypatch
):
    repo, tree = tmp_path / "repo", tmp_path / "worktree"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args):
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            env=env,
            check=True,
            capture_output=True,
        )

    git("init")
    (repo / "AGENTS.md").write_text("REAL_WORKTREE_RULE")
    git("add", "AGENTS.md")
    git("commit", "-m", "fixture")
    git("worktree", "add", "-b", "fixture-worktree", str(tree))
    assert (tree / ".git").is_file()
    runtime = await runtime_at(tmp_path / "runtime", monkeypatch)
    runtime.session_manager.get_or_create("pty")
    runtime.working_directories.switch("pty", str(repo), receipt_key="setup")
    token = runtime.tools.set_context(session_key="pty")
    try:
        started = await runtime.tools.execute_async(
            ToolCall(
                "pty",
                "bash",
                {
                    "command": "printf READY; read answer; pwd",
                    "tty": True,
                    "login": False,
                    "yield_time_ms": 100,
                },
            )
        )
        payload = json.loads(started.content)
        execution_id = payload["task_id"]
        assert payload["process_status"] == "running"
        runtime.working_directories.switch("pty", str(tree), receipt_key="move")
        written = await runtime.tools.execute_async(
            ToolCall(
                "write",
                "write_file",
                {"path": "only-worktree.txt", "content": "new directory"},
            )
        )
        assert not written.is_error and (tree / "only-worktree.txt").exists()
        assert not (repo / "only-worktree.txt").exists()
        completed = await runtime.tools.execute_async(
            ToolCall(
                "stdin",
                "write_stdin",
                {"execution_id": execution_id, "chars": "go\n", "yield_time_ms": 1000},
            )
        )
        assert str(repo) in completed.content and str(tree) not in completed.content
        assert read_agents(str(tree))["files"][0]["text"] == "REAL_WORKTREE_RULE"
    finally:
        runtime.tools.reset_context(token)
        await runtime.stop_background([])
