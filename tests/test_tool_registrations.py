"""Every registered tool must actually open, checked against the registration.

A tool is registered with an `open` that the catalog enters as an async context
manager. Both ways of getting the decorator wrong type-check and both fail only
when something actually opens the thing:

  * none at all -- `async with` on a bare async generator raises TypeError:
    'async_generator' object does not support the asynchronous context manager
    protocol. This is how `recall_memory` broke. A new helper was inserted
    directly beneath an existing `@asynccontextmanager` and took it, leaving
    `open_recall` undecorated.
  * two of them -- the helper that took the decorator then carried its own as
    well, so entering it raised TypeError: '_AsyncGeneratorContextManager'
    object is not an async iterator. Fixing the first fault by hand restored
    `open_recall` and left this one in place, and the mailbox evaluation timed
    out on all 48 trials until it was found.

Neither was caught by a unit test, because the behavioural test for explicit
recall defines its own decorated `open_recall` and exercises that: it verifies
the test's copy, not the one the plugin registers.

These checks read the plugins themselves. They are cheap, they need no runtime,
and they would have caught both the day each landed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PLUGINS = Path("plugins")


def _module_sources() -> list[tuple[Path, ast.Module]]:
    found = []
    for path in sorted(PLUGINS.rglob("*.py")):
        try:
            found.append((path, ast.parse(path.read_text(encoding="utf-8"))))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
    return found


def _names_passed_as(keyword: str, tree: ast.Module) -> set[str]:
    return {node.value.id for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == keyword
            and isinstance(node.value, ast.Name)}


def _wrongly_decorated(tree: ast.Module, names: set[str]) -> list[str]:
    """Exactly one `@asynccontextmanager` on a generator handed out to be opened."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name not in names:
            continue
        if not any(isinstance(inner, (ast.Yield, ast.YieldFrom))
                   for inner in ast.walk(node)):
            continue
        count = sum("asynccontextmanager" in ast.unparse(d) for d in node.decorator_list)
        if count != 1:
            offenders.append(
                f"{node.name} (line {node.lineno}) has {count} @asynccontextmanager")
    return offenders


def _every_double_decoration(tree: ast.Module) -> list[str]:
    """Stacked decorators are never right, whatever the function is passed as.

    Checked over every function in the plugin tree, not only the ones a
    keyword hands out, so the next one is caught wherever it lands.
    """
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        count = sum("asynccontextmanager" in ast.unparse(d) for d in node.decorator_list)
        if count > 1:
            offenders.append(f"{node.name} (line {node.lineno}) x{count}")
    return offenders


# Every keyword this repository uses to hand a context manager to something
# else. A new one should be added here rather than discovered in production.
@pytest.mark.parametrize("keyword", ["open", "open_recall", "open_session",
                                     "open_embedding", "live", "scope", "session"])
def test_everything_opened_as_a_context_manager_is_one(keyword: str) -> None:
    """`async with` on a bare async generator raises only when it is opened.

    The annotation says AsyncGenerator either way, so nothing catches this until
    a request reaches the tool -- in production, not in a unit test.
    """
    broken = []
    for path, tree in _module_sources():
        names = _names_passed_as(keyword, tree)
        if not names:
            continue
        broken += [f"{path}:{where}" for where in _wrongly_decorated(tree, names)]
    assert not broken, (
        f"passed as {keyword}= but not an async context manager: " + ", ".join(broken))


def test_no_plugin_stacks_the_decorator_twice() -> None:
    """Entering a doubly-wrapped generator raises, and the annotation hides it.

    `live_graph` was inserted under `open_recall`'s decorator while carrying its
    own, which both stole one and doubled the other. One edit, two faults, and
    this check catches the half that a missing-decorator check cannot see.
    """
    doubled = []
    for path, tree in _module_sources():
        doubled += [f"{path}:{where}" for where in _every_double_decoration(tree)]
    assert not doubled, "@asynccontextmanager applied more than once: " + ", ".join(doubled)


def test_the_recall_tool_in_particular_opens() -> None:
    """Named explicitly because this is the one that shipped broken."""
    tree = ast.parse((PLUGINS / "akasha/plugin.py").read_text(encoding="utf-8"))
    opener = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "open_recall")
    assert sum("asynccontextmanager" in ast.unparse(d)
               for d in opener.decorator_list) == 1
    # The helper beneath it is the one that took the decorator in the first place.
    live = next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "live_graph")
    assert sum("asynccontextmanager" in ast.unparse(d)
               for d in live.decorator_list) == 1
    assert "live=live_graph" in (PLUGINS / "akasha/plugin.py").read_text(encoding="utf-8")
    # It is registered as a tool and handed to MemoryQueries; both enter it.
    source = (PLUGINS / "akasha/plugin.py").read_text(encoding="utf-8")
    assert "open=open_recall" in source
    assert "open_recall=open_recall" in source
