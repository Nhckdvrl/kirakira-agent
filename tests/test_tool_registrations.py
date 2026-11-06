"""Every registered tool must actually open, checked against the registration.

A tool is registered with an `open` that the catalog enters as an async context
manager. A bare async generator satisfies the type annotation and fails only
when something opens it, which is why `recall_memory` shipped broken from
2026-10-20: the behavioural test for explicit recall defined its own decorated
`open_recall` and exercised that, so it verified the test's copy rather than
the one the plugin registers.

These checks read the plugins themselves. They are cheap, they need no runtime,
and they would have caught it the day it landed.
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


def _generators_without_the_decorator(tree: ast.Module, names: set[str]) -> list[str]:
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name not in names:
            continue
        yields = any(isinstance(inner, (ast.Yield, ast.YieldFrom))
                     for inner in ast.walk(node))
        decorated = any("asynccontextmanager" in ast.unparse(d) for d in node.decorator_list)
        if yields and not decorated:
            offenders.append(f"{node.name} (line {node.lineno})")
    return offenders


@pytest.mark.parametrize("keyword", ["open", "open_recall"])
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
        broken += [f"{path}:{where}" for where in
                   _generators_without_the_decorator(tree, names)]
    assert not broken, (
        f"passed as {keyword}= but not an async context manager: " + ", ".join(broken))


def test_the_recall_tool_in_particular_opens() -> None:
    """Named explicitly because this is the one that shipped broken."""
    tree = ast.parse((PLUGINS / "akasha/plugin.py").read_text(encoding="utf-8"))
    opener = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "open_recall")
    assert any("asynccontextmanager" in ast.unparse(d) for d in opener.decorator_list)
    # It is registered as a tool and handed to MemoryQueries; both enter it.
    source = (PLUGINS / "akasha/plugin.py").read_text(encoding="utf-8")
    assert "open=open_recall" in source
    assert "open_recall=open_recall" in source
