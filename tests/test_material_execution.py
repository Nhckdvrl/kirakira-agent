"""O: independent material owners prepare together and merge in stable order."""
import asyncio
import fcntl
import os

import pytest

from agent.plugin_composition import CompositionRoot, PluginRuntime
from agent.plugin_composition import tasks as task_runtime
from agent.plugin_composition.tasks import Tasks
from plugins.context.api import decode_material
from plugins.context.materials import ContextMaterials
from plugins.models.settings import UpdateConnection
from tests.support.material_models import material_models


@pytest.mark.asyncio
async def test_provider_reuses_connections_across_real_executions_without_reusing_credentials(tmp_path):
    """Socket reuse must survive execution disposal, but not provider disposal."""
    import json
    from plugins.models.contract import MODEL_DRIVERS, CapabilitySources, ModelCapabilities, ModelRequest
    from plugins.models.settings import AddConnection, AddModel, CreateConnectionWithModel
    from plugins.openai_compatible.plugin import apply

    requests, sockets, tasks = [], [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        sockets.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                headers = dict((name.lower(), value) for line in lines[1:] if ": " in line
                               for name, value in (line.split(": ", 1),))
                body = await reader.readexactly(int(headers.get("content-length", 0)))
                if lines[0].startswith("POST"):
                    requests.append((writer, headers, json.loads(body)))
                    if json.loads(body).get("stream"):
                        payload = ('data: {"choices":[{"index":0,"delta":{"content":"OK"},'
                                   '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n').encode()
                        content_type = b"text/event-stream"
                    else:
                        payload = b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'
                        content_type = b"application/json"
                else:
                    payload, content_type = b'{"data":[]}', b"application/json"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode()
                             + b"\r\nContent-Type: " + content_type
                             + b"\r\nSet-Cookie: stale=credential; Path=/\r\n\r\n" + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.remove(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    try:
        async with material_models(tmp_path) as models:
            provider = await models.root.mount(apply, name="http-provider", inject=(MODEL_DRIVERS,))
            assert not sockets  # Registration must remain lazy.
            revision = models.store.read_snapshot().revision
            receipt = await models.settings.apply(CreateConnectionWithModel(
                AddConnection(revision, "http", "fixture", "openai-compatible", endpoint,
                              "identity", {"token": "first"}),
                AddModel(revision, "http-chat", "http", "chat", "arbitrary-model",
                         ModelCapabilities(context_window=8192, max_output_tokens=1024),
                         CapabilitySources(context_window="user", max_output_tokens="user")),
            ))
            requests.clear()  # Settings verification is a separate network probe.

            async def delta(_):
                pass

            async def send():
                async with models.chat_models.execution(model_id="http-chat") as execution:
                    bound = execution.chat("agent")
                    assert (await bound.complete(ModelRequest(messages=(
                        {"role": "user", "content": "hello"},), max_output_tokens=128,
                        on_delta=delta))).content == "OK"
                    return bound

            expired = await send()
            await models.store.credential_handle("http", "identity").refresh({"token": "second"})
            await send()
            assert requests[0][0] is requests[1][0]
            assert [row[1]["authorization"] for row in requests] == ["Bearer first", "Bearer second"]
            assert all("cookie" not in row[1] for row in requests)
            with pytest.raises(RuntimeError):
                await expired.complete(ModelRequest(messages=({"role": "user", "content": "expired"},)))

            # Endpoint/auth changes cannot borrow an old identity's pooled socket.
            await models.settings.apply(UpdateConnection(
                receipt.revision, "http", "changed", "replacement", endpoint + "/changed", {"token": "third"}))
            await send()
            changed = requests[-1]
            assert changed[0] is not requests[1][0]
            assert changed[1]["authorization"] == "Bearer third"
            await provider.dispose()
            await asyncio.sleep(0)
            assert requests[0][0].is_closing() and changed[0].is_closing()
            await models.root.mount(apply, name="http-provider-reloaded", inject=(MODEL_DRIVERS,))
            await send()
            assert requests[-1][0] is not changed[0]
    finally:
        server.close()
        await server.wait_closed()
        for writer in sockets:
            writer.close()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [True, False])
async def test_slow_context_maintenance_does_not_block_replies_and_drains_on_shutdown(tmp_path, release):
    from datetime import UTC, datetime
    from agent.plugin_composition import ServiceKey
    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    from plugins.compaction.contract import COMPACTION_SUMMARIES
    from plugins.ledger.contract import ContentPart, ContentReferences, Input, Output
    from tests.test_default_reply import application

    def gated_summary(sources):
        path = sources / "test_provider/plugin.py"
        code = path.read_text().replace("calls = []", "calls = []\n    entered, ready = __import__('asyncio').Event(), __import__('asyncio').Event()")
        code = code.replace('from plugins.compaction.message_summary import HEADINGS',
            'entered.set()\n                await ready.wait()\n                from plugins.compaction.message_summary import HEADINGS')
        code = code.replace('await ctx.provide(ServiceKey("fixture.calls"), calls)',
            'await ctx.provide(ServiceKey("fixture.calls"), calls)\n    await ctx.provide(ServiceKey("fixture.summary_gate"), (entered, ready))')
        path.write_text(code)

    async with application(tmp_path, replying=True, compaction=True, extra_sources=gated_summary) as (log, host):
        root = host.live_root.context
        sid = "test:maintenance"
        writer = log.writer(sid, author="user", source="history", body_types=(Input, Output),
                            content={"text": lambda part: ContentReferences()})
        writer.append("history-input", Input((ContentPart("text", "保留当前工作。"),)))
        writer.append("history-output", Output((ContentPart("text", "settled history " * 1500),), "complete"))
        writer.expire()
        original = log.reader(sid).snapshot()
        entered, ready = root.require(ServiceKey("fixture.summary_gate"))
        calls = root.require(ServiceKey("fixture.calls"))
        accept = root.require(CHANNEL_INPUT_V2)

        async def send(identity, text):
            accepted = await accept(sid, identity, ChannelInboundMessage(
                "test", "user", "maintenance", text, datetime.now(UTC), {}))
            async with asyncio.timeout(5):
                async for _ in log.catalog().follow():
                    if any(row.seq > accepted.seq and isinstance(row.body, Output)
                           and row.body.finish == "complete" for row in log.reader(sid).snapshot()):
                        return

        await send("first", "继续。")
        await asyncio.wait_for(entered.wait(), 5)
        assert root.require(COMPACTION_SUMMARIES).head(sid) is None
        await send("second", "在吗？")  # Completes while the summary call is held.
        assert not ready.is_set()
        if release:
            # A hard-capacity request joins the existing call instead of
            # generating a second summary or waiting for its own completion.
            third = asyncio.create_task(send("third", "当前输入 " * 1800))
            await asyncio.sleep(0.05)
            assert not third.done()
            ready.set()
            await third
            summary = root.require(COMPACTION_SUMMARIES).head(sid)
            assert summary is not None and summary.version == 2
            assert summary.source_message_ids == tuple(row.message_id for row in original)
            assert len(summary.model_call_ids) == 1
        assert sum("[Source messages]" in str(call.messages) for call in calls) == 1
        assert log.reader(sid).snapshot()[:len(original)] == original
    # The False branch exits with a blocked model call: real shutdown must
    # cancel and drain the worker, not retain a borrowed caller scope.


@pytest.mark.asyncio
async def test_compaction_has_a_real_generation_budget_and_rejects_truncated_facts(tmp_path, monkeypatch):
    from dataclasses import replace
    from plugins.compaction.message_summary import HEADINGS, SummaryError, _request, _summarize
    from plugins.models.contract import LLMResponse
    from tests.support.material_models import _FixtureChat

    async with material_models(tmp_path) as models:
        async with models.chat_models.execution() as execution:
            model = execution.chat("default")
            # Both a tighter available room and a model's declared output limit
            # must make it into the actual request, never a zero/unlimited cap.
            assert _request(model, "", (), 512, 200).max_output_tokens == 512
            assert _request(model, "", (), 16_384, 200).max_output_tokens == 1024
            requests = []

            async def truncated(_self, request):
                requests.append(request)
                return LLMResponse("\n".join(HEADINGS), finish_reason="length")

            monkeypatch.setattr(_FixtureChat, "complete", truncated)
            with pytest.raises(SummaryError, match="输出上限"):
                await _summarize(model, (), "用户明确要求保留路径和未完成任务。", 16_384)
            assert len(requests) == 1 and requests[0].max_output_tokens == 1024

            async def complete(_self, request):
                return replace(await truncated(_self, request), finish_reason="stop")

            monkeypatch.setattr(_FixtureChat, "complete", complete)
            text, calls, groups = await _summarize(model, (), "", 512)
            assert text == "\n".join(HEADINGS) and len(calls) == 1 and groups == ()
            assert requests[-1].max_output_tokens == 512


@pytest.mark.asyncio
async def test_explicit_new_context_retains_history_and_does_not_replay_old_prompt(tmp_path):
    from types import SimpleNamespace
    from datetime import UTC, datetime
    from agent.plugin_composition import ServiceKey
    from kirakira_agent.runtime import rpc
    from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
    from plugins.compaction.contract import COMPACTION_SUMMARIES
    from plugins.ledger.contract import Output
    from tests.test_default_reply import application

    async with application(tmp_path, replying=True, compaction=True) as (log, host):
        root = host.live_root.context
        accept = root.require(CHANNEL_INPUT_V2)
        sid = "test:room"
        async def send(identity, text):
            await accept(sid, identity, ChannelInboundMessage("test", "user", "room", text,
                         datetime.now(UTC), {}))
            async with asyncio.timeout(10):
                while True:
                    rows = log.reader(sid).snapshot()
                    latest = next(row for row in rows if row.message_id == identity)
                    if any(row.seq > latest.seq and isinstance(row.body, Output)
                           and row.body.finish == "complete" for row in rows):
                        return rows
                    await asyncio.sleep(0.01)

        original = await send("old", "PRIOR_CONTEXT_MARKER")
        request = {"session_id": sid, "request_id": "owner-new", "expected_head": original[-1].seq}
        core = SimpleNamespace(plugin_manager=host)
        with pytest.raises(ValueError, match="会话已变化"):
            await rpc(core, "compaction/start-new", {**request, "expected_head": original[-1].seq - 1})
        assert root.require(COMPACTION_SUMMARIES).head(sid) is None
        first = await rpc(core, "compaction/start-new", request)
        assert first == await rpc(core, "compaction/start-new", request)
        lookup = root.require(COMPACTION_SUMMARIES)
        assert lookup.head(sid).version == 3
        await send("fresh", "CURRENT_CONTEXT_MARKER")
        calls = root.require(ServiceKey("fixture.calls"))
        assert "CURRENT_CONTEXT_MARKER" in str(calls[-1].messages)
        assert "PRIOR_CONTEXT_MARKER" not in str(calls[-1].messages)
        assert log.reader(sid).snapshot()[:len(original)] == original
        before = len(calls)
        await send("command-new", "/new")
        assert len(calls) == before  # Starting over never requires a working model.
        latest_reference = lookup.head(sid).reference
        assert latest_reference != first["reference"]
        assert first == await rpc(core, "compaction/start-new", request)
        assert lookup.head(sid).reference == latest_reference
        await send("again", "AFTER_SECOND_RESET")
        assert "CURRENT_CONTEXT_MARKER" not in str(calls[-1].messages)
        assert log.reader(sid).snapshot()[:len(original)] == original


@pytest.mark.asyncio
async def test_topic_material_is_live_per_input_and_does_not_disappear_on_repeated_questions(tmp_path):
    import shutil
    from pathlib import Path
    from agent.plugin_composition.config_input import save_config
    from plugins.context.contract import MATERIALS_V4
    from plugins.ledger.contract import ContentPart, ContentReferences, Input
    from plugins.markdown_memory.documents import topic_text
    from tests.test_default_reply import application

    topic = tmp_path / "workspace/memory/topics/needle.md"

    def sources(directory):
        shutil.copytree(Path(__file__).parents[1] / "plugins/markdown_memory", directory / "markdown_memory",
                        ignore=shutil.ignore_patterns("__pycache__"))
        save_config(tmp_path / "workspace/plugin-data/context-builtin", {
            "summary_source": ["compaction", "compaction"],
            "prompt_sources": {"markdown_memory": "markdown_memory"}})
        save_config(tmp_path / "workspace/plugin-data/markdown_memory-builtin",
                    {"surface_min_score": 0.01, "consolidate": False})
        topic.parent.mkdir(parents=True)
        topic.write_text(topic_text("needle project", "- needle budget BUDGET=760."))

    async with application(tmp_path, replying=False, compaction=True, extra_sources=sources) as (log, host):
        writer = log.writer("topics", author="user", source="conversation", body_types=(Input,),
                            content={"text": lambda part: ContentReferences()})
        async with host.live_root.context.require(MATERIALS_V4).bind() as view:
            writer.append("first-topic-input", Input((ContentPart("text", "needle budget"),)))
            async def material():
                return decode_material(await view.prepare(log.reader("topics").snapshot(), "conversation"))
            first = await material()
            assert first.reminders and all(not item.replay for item in first.reminders)
            assert "BUDGET=760" in str(first.reminders)
            topic.write_text(topic_text("needle project", "- needle budget BUDGET=900."))
            assert await material() == first  # A tool continuation retains its frozen Input material.
            writer.append("second-topic-input", Input((ContentPart("text", "needle budget"),)))
            second = await material()
            assert "BUDGET=900" in str(second.reminders) and "BUDGET=760" not in str(second.reminders)
            assert all(not item.replay for item in second.reminders)
            writer.append("unrelated-input", Input((ContentPart("text", "unrelated question"),)))
            assert not (await material()).reminders


@pytest.mark.asyncio
async def test_personality_composition_preserves_owned_file_and_frozen_materials(tmp_path):
    from agent.plugin_composition.plugin_config import PLUGIN_CONFIG, PluginConfig
    from plugins.context.contract import MATERIALS_V4
    from plugins.prompt.plugin import apply, inject

    path = tmp_path / "memory/VEDA.md"
    path.parent.mkdir()
    path.write_text("你叫星灯，表达沉静，偶尔轻轻吐槽。\n")
    previous = path.read_bytes()
    root = CompositionRoot("personality-composition")
    services = []

    async def setup(ctx):
        service = ContextMaterials(ctx, prompt_sources={"default_prompt": "prompt"})
        services.append(service)
        await ctx.provide(MATERIALS_V4, service)
        await ctx.provide(PLUGIN_CONFIG, PluginConfig(None))

    await root.mount(setup, name="materials")
    await root.mount(apply, name="prompt", inject=inject,
                     runtime=PluginRuntime("prompt", "personality-generation", tmp_path, tmp_path, tmp_path,
                                           {"warmth": "warm", "candor": "direct", "humor": "off",
                                            "support": "listen_first", "suggestions": "on_request",
                                            "voice_examples": "我：我们去哪？你：月球。"},
                                           workspace_files=("memory/VEDA.md",)))
    try:
        async with services[0].bind() as view:
            material = decode_material(await view.prepare((), "conversation"))
            assert material.reminders and all(not part.replay for part in material.reminders)
            conversation = material.system_prompt
            proactive = decode_material(await view.prepare((), "wake")).system_prompt
            assert conversation == proactive
            assert "你叫星灯" in conversation
            assert "warmth=warm" in conversation and "candor=direct" in conversation
            assert "humor=off" in conversation and "- curiosity=" not in conversation
            assert "support=listen_first" in conversation and "suggestions=on_request" in proactive
            assert "月球" in conversation and "不是发生过的对话或事实" in conversation
            assert path.read_bytes() == previous
            path.write_text("你叫星灯，这是用户明确修改后的底稿。\n")
            changed = decode_material(await view.prepare((), "conversation")).system_prompt
            assert "用户明确修改后的底稿" in changed and "偶尔轻轻吐槽" not in changed
            assert "偶尔轻轻吐槽" in conversation  # An already prepared request remains immutable.
            assert "warmth=warm" in changed  # Editing the file does not erase explicit composition.
    finally:
        await root.dispose()


@pytest.mark.asyncio
async def test_existing_profile_lock_supports_nfs_and_cancelled_waiters(tmp_path, monkeypatch):
    from plugins.markdown_memory.plugin import profile_lock

    path = tmp_path / "markdown-profile.lock"
    path.write_bytes(b"existing lock metadata\n")
    flock = fcntl.flock
    waiting = asyncio.Event()

    def strict_flock(descriptor, operation):
        if operation & fcntl.LOCK_EX:
            # NFS rejects exclusive locks on descriptors opened read-only.
            assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        try:
            flock(descriptor, operation)
        except BlockingIOError:
            waiting.set()
            raise

    monkeypatch.setattr(fcntl, "flock", strict_flock)

    async def contender():
        async with profile_lock(path, create=False):
            raise AssertionError("contender acquired a held profile lock")

    async with profile_lock(path, create=False):
        job = asyncio.create_task(contender())
        try:
            await asyncio.wait_for(waiting.wait(), 2)
        finally:
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
    async with profile_lock(path, create=False):
        assert path.read_bytes() == b"existing lock metadata\n"


@pytest.mark.asyncio
async def test_independent_material_owners_overlap_on_a_real_root(tmp_path):
    root = CompositionRoot("material-execution")
    services: list[ContextMaterials] = []
    entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def setup(ctx):
        services.append(ContextMaterials(ctx, prompt_sources={}))

    await root.mount(setup, name="materials")
    service = services[0]

    async def first(ctx):
        async def prepare(snapshot, source):
            entered.set()
            await release.wait()
            return {"reminders": ({"name": "a", "text": "A", "priority": 20},)}
        await service.register(ctx, name="a", prepare=prepare, kind="context")

    async def other(ctx):
        async def prepare(snapshot, source):
            second.set()
            return {"reminders": ({"name": "b", "text": "B", "priority": 10},)}
        await service.register(ctx, name="b", prepare=prepare, kind="context")

    for name, apply in (("a", first), ("b", other)):
        await root.mount(apply, name=name,
                         runtime=PluginRuntime(name, "materials", tmp_path, tmp_path, tmp_path, {}))
    try:
        async with service.bind() as view:
            job = asyncio.create_task(view.prepare((), "conversation"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(second.wait(), 1)
            finally:
                release.set()
                result = await job
            assert [item.text for item in decode_material(result).reminders] == ["B", "A"]
    finally:
        release.set()
        await root.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_material_failure_and_cancellation_drain_real_owners(tmp_path, cancel):
    root = CompositionRoot("material-drain")
    services: list[ContextMaterials] = []
    entered, release = asyncio.Event(), asyncio.Event()
    drained = []
    first_error = ValueError("first owner failed")

    async def setup(ctx):
        services.append(ContextMaterials(ctx, prompt_sources={}))

    await root.mount(setup, name="materials")
    service = services[0]

    async def first(ctx):
        async def prepare(snapshot, source):
            try:
                if cancel:
                    await release.wait()
                raise first_error
            finally:
                drained.append("a")
        await service.register(ctx, name="a", prepare=prepare, kind="context")

    async def second(ctx):
        async def prepare(snapshot, source):
            entered.set()
            try:
                await release.wait()
                raise RuntimeError("second owner failed")
            finally:
                drained.append("b")
        await service.register(ctx, name="b", prepare=prepare, kind="context")

    for name, apply in (("a", first), ("b", second)):
        await root.mount(apply, name=name,
                         runtime=PluginRuntime(name, "materials", tmp_path, tmp_path, tmp_path, {}))
    try:
        async with service.bind() as view:
            job = asyncio.create_task(view.prepare((), "conversation"))
            await asyncio.wait_for(entered.wait(), 2)
            assert not job.done()
            if cancel:
                job.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await job
            else:
                release.set()
                with pytest.raises(ValueError) as caught:
                    await job
                assert caught.value is first_error
            assert sorted(drained) == ["a", "b"]
    finally:
        release.set()
        await root.dispose()


@pytest.mark.asyncio
async def test_material_models_keep_parent_revision_and_open_own_bindings(tmp_path):
    """A settings update cannot change models halfway through joined material work."""
    entered, release, sibling = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with material_models(tmp_path) as models:
        services = []
        parent_models = []
        seen = []

        async def setup(ctx):
            services.append(ContextMaterials(ctx, prompt_sources={}))

        await models.root.mount(setup, name="materials")
        service = services[0]

        async def first(ctx):
            async def embed():
                async with models.embeddings.bind() as model:
                    seen.append(model.descriptor.model_revision)
                    assert len((await model.embed(["material"])).vectors[0]) == 3

            async def prepare(snapshot, source):
                entered.set()
                await release.wait()
                await embed()
                # Nested joined children carry data, never the parent's driver.
                await asyncio.create_task(embed(), context=task_runtime.child_task_context())
                async with models.chat_models.execution() as execution:
                    model = execution.chat("agent")
                    seen.append(model.descriptor.model_revision)
                    assert model is not parent_models[0]
                return {"reminders": ({"name": "a", "text": "A", "priority": 20},)}
            await service.register(ctx, name="a", prepare=prepare, kind="recall")

        async def other(ctx):
            async def prepare(snapshot, source):
                sibling.set()
                return {"reminders": ({"name": "b", "text": "B", "priority": 10},)}
            await service.register(ctx, name="b", prepare=prepare, kind="context")

        for name, apply in (("a", first), ("b", other)):
            await models.root.mount(apply, name=name,
                runtime=PluginRuntime(name, "materials", tmp_path, tmp_path, tmp_path, {}))

        async def run():
            async with models.chat_models.execution() as execution, service.bind() as view:
                parent_models.append(execution.chat("agent"))
                return await view.prepare((), "conversation")

        job = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(sibling.wait(), 2)
            parent = parent_models[0].descriptor
            await models.settings.apply(UpdateConnection(
                expected_revision=parent.model_revision, connection_id=parent.connection_id,
                name="changed during materials", auth_identity=parent.auth_identity,
                endpoint="https://changed.invalid",
            ))
        finally:
            release.set()
        result = await asyncio.wait_for(job, 5)
        assert seen == [parent.model_revision] * 3
        assert [item.text for item in decode_material(result).reminders] == ["B", "A"]
        assert all(item.endpoint == "https://fixture.invalid" for item in models.driver.opened[-3:])
        assert models.driver.closed == len(models.driver.opened)


@pytest.mark.asyncio
async def test_raw_model_children_reject_borrowed_execution_and_independent_tasks_use_new_settings(tmp_path):
    """Joined material work does not weaken raw-child or independent-Task contracts."""
    async with material_models(tmp_path) as models:
        async def embed():
            async with models.embeddings.bind() as model:
                await model.embed(["independent"])
                return model.descriptor.model_revision

        async with models.chat_models.execution() as execution:
            parent = execution.chat("agent").descriptor
            with pytest.raises(RuntimeError, match="不能由子 task 继承"):
                await asyncio.create_task(embed())
            changed = await models.settings.apply(UpdateConnection(
                expected_revision=parent.model_revision, connection_id=parent.connection_id,
                name="new independent work", auth_identity=parent.auth_identity,
                endpoint="https://changed.invalid",
            ))

            async def joined():
                tasks = Tasks()
                try:
                    task = await tasks.admit("embedding", lambda slot: slot.start(lambda _: embed()))
                    assert await task.join() == changed.revision
                    async with models.chat_models.independent_execution() as independent:
                        assert independent.chat("agent").descriptor.model_revision == changed.revision
                finally:
                    await tasks.close()

            await asyncio.create_task(joined(), context=task_runtime.child_task_context())
            assert execution.chat("agent").descriptor.model_revision == parent.model_revision
        assert models.driver.closed == len(models.driver.opened)


@pytest.mark.asyncio
async def test_cancelled_material_drains_real_model_binding(tmp_path):
    """Cancellation closes both the child's embedding and parent's chat connection."""
    release = asyncio.Event()
    async with material_models(tmp_path, release=release) as models:
        services = []

        async def setup(ctx):
            service = ContextMaterials(ctx, prompt_sources={})
            services.append(service)
            async def prepare(snapshot, source):
                async with models.embeddings.bind() as model:
                    await model.embed(["cancelled"])
                return {}
            await service.register(ctx, name="embedding", prepare=prepare, kind="recall")

        await models.root.mount(setup, name="materials",
            runtime=PluginRuntime("materials", "materials", tmp_path, tmp_path, tmp_path, {}))

        async def run():
            async with models.chat_models.execution(), services[0].bind() as view:
                return await view.prepare((), "conversation")

        job = asyncio.create_task(run())
        try:
            await asyncio.wait_for(models.driver.entered.wait(), 2)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
            assert models.driver.closed == len(models.driver.opened) == 2
        finally:
            release.set()
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
