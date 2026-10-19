"""Distribution adapter over the unmodified upstream Fiber/Effect kernel.

There is no second dependency graph here. Module is a distribution declaration;
Context only adapts the older synchronous provider/extension registration API.
All activation, dependency withdrawal, admission and disposal belong to the
native Root. New extensions receive the native Context directly.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from agent.plugin_composition import RUNTIME_STARTED, PluginRuntime, ServiceKey
from agent.plugin_composition import CompositionRoot as NativeRoot
from agent.plugin_composition.effect import _join_cleanup
from agent.plugin_composition.model import CompositionError, FiberState

from .scope import ResourceScope
from .services import ServiceRegistry, ServiceUnavailable


@dataclass(frozen=True)
class Module:
    name: str
    apply: Callable
    inject: tuple[ServiceKey, ...] = ()
    provides: tuple[ServiceKey, ...] | None = ()
    config: Mapping = field(default_factory=dict)
    parent: str | None = None
    native: bool = False


class Activation:
    """Legacy tool adapter to an exact native Fiber activation, not a new owner."""

    def __init__(self, context):
        self.context = context
        self.handle = context.fiber
        self.token = self.handle.activation_token
        self.generation_id = uuid4().hex
        self._calls = {}

    @property
    def state(self):
        state = self.handle.state.value
        if self.handle.activation_token is not self.token:
            return (
                "closed"
                if state == "active"
                else {"unloading": "draining", "disposed": "closed"}.get(state, state)
            )
        return {
            "unloading": "draining",
            "disposed": "closed",
            "pending": "waiting",
        }.get(state, state)

    @property
    def refs(self):
        return sum(len(calls) for calls in self._calls.values())

    def acquire(self):
        try:
            call = self.handle.acquire_call(self.token)
        except CompositionError as error:
            raise ServiceUnavailable(str(error)) from error
        self._calls.setdefault(asyncio.current_task(), []).append(call)

    def release(self):
        task = asyncio.current_task()
        calls = self._calls[task]
        calls.pop().release()
        if not calls:
            del self._calls[task]


class Context:
    """Adapter only for Kirakira's existing synchronous provider declarations."""

    def __init__(self, root, native, module):
        self._root, self.native, self.module = root, native, module
        self.config = module.config
        self._activation = Activation(native)
        self._scope = ResourceScope()
        self._values = {}
        self._starters, self._stoppers, self._drainers, self._readiers = [], [], [], []
        self._started = False

    @property
    def owner(self):
        return self._activation

    @property
    def runtime_options(self):
        return self._root.runtime_options

    def inject(self, key):
        if key not in self.module.inject:
            raise PermissionError("undeclared dependency: " + key.name)
        return self.native.get(key)

    def provide(self, key, value):
        if self.module.provides is not None and key not in self.module.provides:
            raise PermissionError("undeclared export: " + key.name)
        if key in self._values:
            raise ValueError("duplicate export: " + key.name)
        self._values[key] = value

    def on_close(self, closer):
        self._scope.on_close(closer)

    def on_start(self, action):
        self._starters.append(action)

    def on_stopping(self, action):
        self._stoppers.append(action)

    def on_draining(self, action):
        self._drainers.append(action)

    def on_ready(self, action):
        self._readiers.append(action)

    async def effect(self, setup):
        return await self.native.effect(setup)

    async def spawn(self, coroutine, *, name=None):
        return await self.native.spawn(coroutine, name=name or self.module.name)

    async def install(self, module):
        return await self._root._mount(module, parent=self.native)

    async def start(self):
        if not self._started:
            self._started = True
            for action in self._starters:
                await _invoke(action)

    async def close(self):
        for action in self._stoppers:
            await _invoke(action)
        for action in self._drainers:
            await _invoke(action)
        await self._scope.close()


async def _invoke(action):
    result = action()
    return await result if inspect.isawaitable(result) else result


class CompositionRoot:
    """Select and replace distribution modules using one native Root."""

    def __init__(self, *, services=None):
        if services is not None:
            raise TypeError("service directories are owned by their native Root")
        self.native = NativeRoot(uuid4().hex)
        self.services = ServiceRegistry(self.native)
        self._modules, self._contexts, self._fibers = {}, {}, {}
        self._closed = self._started = False
        self.runtime_options = {}
        self._starts, self._readies = {}, {}

    @staticmethod
    async def _physical(operation):
        task = asyncio.create_task(operation)
        await _join_cleanup(task)
        return task.result()

    async def _mount(self, module, *, parent=None):
        module = replace(module, config={**module.config, "_host": self})
        self._modules[module.name] = module

        async def apply(native):
            ctx = Context(self, native, module)
            self._contexts[module.name] = ctx
            if module.native:
                await module.apply(native)
                return
            await native.effect(lambda: ctx.close, label="legacy-extension-resources")
            result = module.apply(ctx)
            if inspect.isawaitable(result):
                await result
            for key, value in ctx._values.items():
                await native.provide(key, value)
            if self._started:
                await ctx.start()
                for action in ctx._readiers:
                    await _invoke(action)

        if parent is None and module.parent is not None:
            parent = self._contexts[module.parent].native
        workspace = Path(module.config.get("workspace", "/tmp"))
        runtime = PluginRuntime(
            module.name.removeprefix("plugin:"),
            self.native.generation_id,
            Path(module.config.get("plugin_dir", workspace)),
            workspace
            / ".kirakira"
            / "plugin-data"
            / module.name.removeprefix("plugin:"),
            workspace,
            module.config,
        )
        if parent is None:
            fiber = await self.native.mount(
                apply, name=module.name, inject=module.inject, runtime=runtime
            )
        else:
            fiber = await self.native._mount(
                parent=parent._fiber,
                plugin=apply,
                name=module.name,
                inject=module.inject,
                required_for_readiness=True,
                runtime=runtime,
                plugin_module=None,
            )
        self._fibers[module.name] = fiber
        if fiber.state == FiberState.FAILED:
            raise fiber.error or RuntimeError("activation failed: " + module.name)
        return fiber

    async def start(self, modules):
        try:
            for module in modules:
                await self._mount(module)
            self.native.require_ready(self.native.fibers())
        except BaseException:
            await self.close()
            raise

    async def register_start(self, ctx, action, *, ready=False):
        queue = self._readies if ready else self._starts
        identity = object()

        async def start_once():
            pending = queue.pop(identity, None)
            if pending is not None:
                with ctx.call_scope():
                    await _invoke(action)

        queue[identity] = start_once
        await ctx.effect(lambda: lambda: queue.pop(identity, None), label="launch-gate")

        async def started(_):
            if self._started and not ready:
                await start_once()

        await ctx.on(RUNTIME_STARTED, started)

    async def start_runtime(self, **options):
        self.runtime_options = options
        self._started = True
        for ctx in tuple(self._contexts.values()):
            if ctx.owner.state == "active" and not ctx.module.native:
                await ctx.start()
        for action in tuple(self._starts.values()):
            await action()
        for ctx in tuple(self._contexts.values()):
            if ctx.owner.state == "active" and not ctx.module.native:
                for action in ctx._readiers:
                    await _invoke(action)
        for action in tuple(self._readies.values()):
            await action()

    async def _flush_ready(self):
        for action in tuple(self._readies.values()):
            await action()

    async def install(self, module):
        async def install():
            fiber = await self._mount(module)
            if self._started:
                await self._flush_ready()
            return fiber

        return await self._physical(install())

    def check_change(self, name):
        # Preserve the original caller for the native reentrancy checks before
        # joining a cancellation-shielded update task.
        owner = self._fibers[name]
        for fiber in (owner, *self.native.consumers((owner,))):
            fiber._reject_self_call_wait()
            fiber._reject_direct_reentrant_wait("distribution update")

    async def replace(self, name, module):
        self.check_change(name)

        async def change():
            await self._fibers[name].dispose()
            fiber = await self._mount(module)
            if self._started:
                await self._flush_ready()
            return fiber

        return await self._physical(change())

    async def remove(self, name):
        self.check_change(name)

        async def change():
            await self._fibers[name].dispose()
            self._modules.pop(name, None)

        return await self._physical(change())

    async def close(self):
        await self.native.dispose()
        self._closed = True

    def activation(self, name):
        fiber = self._fibers.get(name)
        if fiber is None or fiber.state == FiberState.DISPOSED:
            return None
        ctx = self._contexts.get(name)
        return ctx.owner if ctx is not None else None

    def tasks(self):
        return tuple(task for fiber in self.native.fibers() for task in fiber._tasks)

    def catalog(self):
        return tuple(
            {
                "name": f.name,
                "state": {"unloading": "draining", "pending": "waiting"}.get(
                    f.state.value, f.state.value
                ),
                "generation": getattr(self.activation(f.name), "generation_id", None),
                "calls": getattr(self.activation(f.name), "refs", 0),
                "error": str(f.error) if f.error else None,
                "inject": tuple(key.name for key in f.dependencies),
            }
            for f in self.native.fibers()
            if f.state != FiberState.DISPOSED
        )
