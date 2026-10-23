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
