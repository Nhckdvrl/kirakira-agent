"""The Operation Bus: one owner per action, and a receipt that cannot be borrowed.

Ported from upstream akashic's Self-CLI bus. The two properties worth pinning
are the ones that make it better than a handler per entry point:

* a request_id is bound to the *call* (actor, session, argv), so replaying it
  returns that call's result and reusing the id for a different call is a
  conflict rather than someone else's answer;
* `audience="user_only"` operations are invisible and unreachable to the agent,
  which is what lets a UI-only action live on the same bus.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins.operations.contract import (
    ExtensionSpec,
    OperationRequest,
    OperationResult,
    OperationSpec,
    OptionSpec,
)
from plugins.operations.registry import OperationRegistry


class _Ctx:
    """The slice of Context the registry touches."""

    def __init__(self, plugin_id: str = "fixture") -> None:
        self.runtime = SimpleNamespace(plugin_id=plugin_id)
        self.provided: dict[object, object] = {}

    async def effect(self, setup, *, label: str = "effect"):
        detach = setup()
        return SimpleNamespace(label=label, detach=detach)

    async def provide(self, key, value, **_kwargs):
        self.provided[key] = value
        return value

    def runtime_scope(self):
        ctx = self

        class _Scope:
            async def __aenter__(self):
                ctx.entered = True

            async def __aexit__(self, *_exc):
                return False

        return _Scope()


async def _register(registry: OperationRegistry, ctx: _Ctx, **kwargs):
    calls: list[OperationRequest] = []

    async def handler(request: OperationRequest) -> OperationResult:
        calls.append(request)
        return OperationResult(True, "done", data={"seen": dict(request.args)})

    spec = OperationSpec(handler=handler, **kwargs)
    effect = await registry.register(ctx, spec)
    return spec, calls, effect


@pytest.mark.asyncio
async def test_one_owner_per_path_and_the_node_is_published():
    registry, ctx = OperationRegistry(), _Ctx()
    _, calls, effect = await _register(
        registry, ctx, operation_id="fixture.create", path=("thing", "create"),
        summary="建一个", options=(OptionSpec("name", "名字", required=True),),
        mirrors=("rpc:fixture/create",))

    # The isomorphic subtree node is published for this path.
    assert any(getattr(key, "name", "") == "cli.node:thing.create" for key in ctx.provided)
    assert registry.mirrors() == {"fixture.create": ("rpc:fixture/create",)}

    # A second owner cannot claim the same id or the same path.
    with pytest.raises(ValueError):
        await _register(registry, ctx, operation_id="fixture.create",
                        path=("thing", "other"), summary="重复 id")
    with pytest.raises(ValueError):
        await _register(registry, ctx, operation_id="other.create",
                        path=("thing", "create"), summary="重复路径")
    # Paths are validated, so a stray argv token cannot become a command.
    for bad in (("Thing",), ("--flag",), ()):
        with pytest.raises(ValueError):
            await _register(registry, ctx, operation_id=f"bad{len(bad)}.x",
                            path=bad, summary="非法路径")

    result = await registry.invoke(["thing", "create", "--name", "甲"], actor="agent")
    assert result.ok and result.data["seen"] == {"name": "甲"}
    assert calls[0].operation_id == "fixture.create" and calls[0].actor == "agent"

    # Unregistering removes the route; the bus stops answering for it.
    effect.detach()
    missing = await registry.invoke(["thing", "create", "--name", "甲"], actor="agent")
    assert not missing.ok and missing.error_code == "unknown_operation"


@pytest.mark.asyncio
async def test_arguments_are_validated_before_the_handler_runs():
    registry, ctx = OperationRegistry(), _Ctx()
    _, calls, _ = await _register(
        registry, ctx, operation_id="fixture.set", path=("thing", "set"), summary="设置",
        options=(OptionSpec("name", "名字", required=True),
                 OptionSpec("count", "次数", value_type="int", default=3),
                 OptionSpec("mode", "模式", choices=("fast", "slow")),
                 OptionSpec("flag", "开关", value_type="bool"),
                 OptionSpec("blob", "负载", value_type="json")))

    ok = await registry.invoke(
        ["thing", "set", "--name", "甲", "--flag", "--blob", '{"k":1}', "--mode=fast"],
        actor="agent")
    assert ok.ok
    # Defaults fill in, bare flags mean true, and typed values are coerced.
    assert ok.data["seen"] == {"name": "甲", "flag": True, "blob": {"k": 1},
                               "mode": "fast", "count": 3}

    for argv, code in (
        (["thing", "set"], "invalid_arguments"),                       # required missing
        (["thing", "set", "--name", "甲", "--count", "x"], "invalid_arguments"),
        (["thing", "set", "--name", "甲", "--mode", "medium"], "invalid_arguments"),
        (["thing", "set", "--name", "甲", "--nope", "1"], "invalid_arguments"),
        (["thing", "set", "--name", "甲", "--name", "乙"], "invalid_arguments"),
    ):
        bad = await registry.invoke(argv, actor="agent")
        assert not bad.ok and bad.error_code == code, argv
    # A bad JSON value must not echo the value back: it may be a credential.
    bad = await registry.invoke(["thing", "set", "--name", "甲", "--blob", "s3cret-not-json"],
                                actor="agent")
    assert not bad.ok and "s3cret" not in bad.summary
    # Nothing above reached the handler except the one valid call.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_receipt_belongs_to_its_call_and_not_to_its_id():
    registry, ctx = OperationRegistry(), _Ctx()
    _, calls, _ = await _register(
        registry, ctx, operation_id="fixture.run", path=("thing", "run"), summary="跑一次",
        options=(OptionSpec("name", "名字"),))

    first = await registry.invoke(["thing", "run", "--name", "甲"], actor="agent",
                                  session_id="s1", request_id="r1")
    assert first.ok and len(calls) == 1
    # The same call replays the receipt instead of running the side effect again.
    again = await registry.invoke(["thing", "run", "--name", "甲"], actor="agent",
                                  session_id="s1", request_id="r1")
    assert again is first or again.data == first.data
    assert len(calls) == 1

    # The same id with different argv, a different actor, or a different session
    # is a conflict -- not a way to collect someone else's result.
    for argv, actor, session in (
        (["thing", "run", "--name", "乙"], "agent", "s1"),
        (["thing", "run", "--name", "甲"], "user", "s1"),
        (["thing", "run", "--name", "甲"], "agent", "s2"),
    ):
        clash = await registry.invoke(argv, actor=actor, session_id=session, request_id="r1")
        assert not clash.ok and clash.error_code == "conflict"
    assert len(calls) == 1

    # `receipt` only answers the original caller.
    mine = await registry.invoke(["receipt", "r1"], actor="agent", session_id="s1")
    assert mine.ok
    theirs = await registry.invoke(["receipt", "r1"], actor="agent", session_id="s2")
    assert not theirs.ok and theirs.error_code == "not_found"
    assert registry.receipt("r1") is not None and registry.receipt("nope") is None


@pytest.mark.asyncio
async def test_concurrent_retries_of_one_request_id_run_the_effect_once():
    registry, ctx = OperationRegistry(), _Ctx()
    gate = asyncio.Event()
    runs = 0

    async def handler(_request: OperationRequest) -> OperationResult:
        nonlocal runs
        runs += 1
        await gate.wait()
        return OperationResult(True, "done")

    await registry.register(ctx, OperationSpec(
        operation_id="fixture.slow", path=("thing", "slow"), summary="慢", handler=handler))
    first = asyncio.create_task(registry.invoke(["thing", "slow"], actor="agent",
                                                session_id="s", request_id="r"))
    await asyncio.sleep(0)
    second = asyncio.create_task(registry.invoke(["thing", "slow"], actor="agent",
                                                 session_id="s", request_id="r"))
    await asyncio.sleep(0)
    gate.set()
    assert (await first).ok and (await second).ok
    assert runs == 1


@pytest.mark.asyncio
async def test_user_only_operations_are_invisible_and_unreachable_to_the_agent():
    registry, ctx = OperationRegistry(), _Ctx()
    await _register(registry, ctx, operation_id="fixture.danger", path=("thing", "danger"),
                    summary="仅用户", audience="user_only")
    assert "danger" not in registry.render_help(actor="agent")
    assert "danger" in registry.render_help(actor="user")
    assert registry.describe(actor="agent")["operations"] == []
    refused = await registry.invoke(["thing", "danger"], actor="agent")
    assert not refused.ok and refused.error_code == "forbidden"
    assert (await registry.invoke(["thing", "danger"], actor="user")).ok


@pytest.mark.asyncio
async def test_an_extension_adds_namespaced_options_without_the_owner_knowing():
    registry, ctx = OperationRegistry(), _Ctx()
    _, calls, _ = await _register(
        registry, ctx, operation_id="fixture.make", path=("thing", "make"), summary="造",
        options=(OptionSpec("name", "名字"),), extension_points=("thing.make",))

    seen: list[tuple[str, object]] = []

    async def validate(_request, values):
        seen.append(("validate", dict(values)))
        if values.get("tag") == "bad":
            raise ValueError("标签不允许")

    async def apply(_request, _result, values):
        seen.append(("apply", dict(values)))
        return {"tagged": values.get("tag")}

    await registry.extend(ctx, ExtensionSpec(
        namespace="label", options=(OptionSpec("tag", "标签"),),
        validate=validate, apply=apply, extension_point="thing.make"))

    # The owner's handler never learns the extension exists; its output is merged.
    result = await registry.invoke(["thing", "make", "--name", "甲", "--label.tag", "x"],
                                   actor="agent")
    assert result.ok and result.data["extensions"] == {"label": {"tagged": "x"}}
    assert calls[0].extensions == {"label": {"tag": "x"}}
    assert [kind for kind, _ in seen] == ["validate", "apply"]

    # A rejecting extension stops the call before the handler runs.
    before = len(calls)
    refused = await registry.invoke(["thing", "make", "--label.tag", "bad"], actor="agent")
    assert not refused.ok and refused.error_code == "validation_failed"
    assert len(calls) == before
    # An unknown namespace is an argument error, not a silently ignored flag.
    unknown = await registry.invoke(["thing", "make", "--nope.tag", "x"], actor="agent")
    assert not unknown.ok and unknown.error_code == "invalid_arguments"
    # Two extensions cannot claim one namespace on the same mount point.
    with pytest.raises(ValueError):
        await registry.extend(ctx, ExtensionSpec(
            namespace="label", options=(), extension_point="thing.make"))
    # An extension must pick exactly one mount point.
    for kwargs in ({}, {"target_operation": "fixture.make", "extension_point": "thing.make"}):
        with pytest.raises(ValueError):
            await registry.extend(ctx, ExtensionSpec(namespace="other", options=(), **kwargs))


@pytest.mark.asyncio
async def test_a_failing_handler_is_reported_and_does_not_escape():
    registry, ctx = OperationRegistry(), _Ctx()

    async def rejects(_request):
        raise PermissionError("不允许")

    async def breaks(_request):
        raise RuntimeError("内部坏了")

    await registry.register(ctx, OperationSpec(
        operation_id="fixture.no", path=("thing", "no"), summary="拒绝", handler=rejects))
    await registry.register(ctx, OperationSpec(
        operation_id="fixture.boom", path=("thing", "boom"), summary="崩", handler=breaks))

    rejected = await registry.invoke(["thing", "no"], actor="agent")
    assert not rejected.ok and rejected.error_code == "rejected" and "不允许" in rejected.summary
    failed = await registry.invoke(["thing", "boom"], actor="agent")
    assert not failed.ok and failed.error_code == "failed" and "RuntimeError" in failed.summary


@pytest.mark.asyncio
async def test_help_and_describe_are_reachable_without_a_command():
    registry, ctx = OperationRegistry(), _Ctx()
    assert "没有可用命令" in registry.render_help()
    await _register(registry, ctx, operation_id="fixture.one", path=("group", "one"),
                    summary="一", options=(OptionSpec("name", "名字", set_once=True),))
    for argv in ([], ["help"], ["--help"], ["-h"], ["group", "--help"]):
        assert (await registry.invoke(argv, actor="agent")).ok
    listed = await registry.invoke(["describe"], actor="agent")
    assert listed.ok and listed.data["operations"][0]["operation_id"] == "fixture.one"
    assert "[set-once]" in registry.render_help()
    assert "未找到匹配命令" in registry.render_help(("nope",))


def test_the_bus_is_actually_selected_by_the_distribution():
    """A plugin in `plugins/` that no bundle selects never loads.

    The bus is a registry other plugins mount onto, so if it is absent the
    mounts fail at composition rather than at a call site -- and a tool-surface
    diff would still report it as present. Checked here because this exact shape
    (registered but never reached) is how `recall_memory` stayed broken.
    """
    import tomllib
    from pathlib import Path

    from agent.plugins.static_manifest import load_static_plugin_manifest
    from kirakira_agent.runtime import HEADLESS

    manifest = load_static_plugin_manifest(Path("plugins/operations"))
    assert manifest.name == "operations" and manifest.api_version == 3
    assert manifest.name in HEADLESS, "the bus must be selected in the headless profile"
    for bundle in ("base", "minimal"):
        rows = tomllib.loads(Path(f"bundles/{bundle}.toml").read_text(encoding="utf-8"))["rows"]
        assert "operations" in rows, f"{bundle} does not select the bus"
        assert rows["operations"]["plugin"].startswith("operations@")
