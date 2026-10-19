"""Run ordered plugin phases with shared halt semantics."""

from __future__ import annotations

import inspect
from typing import Any, List


class LiveContributions(list):
    """Enumerate currently active providers when the phase is invoked."""

    provider = None

    def __iter__(self):
        yield from super().__iter__()
        if self.provider is not None:
            yield from self.provider()


async def _run_plugin_modules(modules: List[object], ctx: Any) -> Any:
    current = ctx
    for module in modules:
        runner = getattr(module, "run", None)
        if runner is None and callable(module):
            runner = module
        if runner is None:
            continue
        result = runner(current)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            current = result
    return current
