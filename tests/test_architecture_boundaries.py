"""Guard canonical owners and the external-reference boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    "agent",
    "bootstrap",
    "bus",
    "core",
    "eval",
    "frontend",
    "infra",
    "kirakira_agent",
    "memory2",
    "migrations",
    "plugin_packages",
    "plugins",
    "proactive_v2",
    "plugins/ledger",
    "utils",
)


def test_production_code_does_not_read_reference_checkout() -> None:
    violations: list[str] = []
    for source_root in SOURCE_ROOTS:
        for path in (ROOT / source_root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant)
                    and node.right.value == "Reference"
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Path"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "Reference"
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_brand_package_is_only_a_public_entry_shell() -> None:
    files = {
        path.name
        for path in (ROOT / "kirakira_agent").iterdir()
        if path.is_file() and path.suffix == ".py"
    }
    assert files == {"__init__.py", "__main__.py"}

    violations: list[str] = []
    for source_root in SOURCE_ROOTS:
        if source_root == "kirakira_agent":
            continue
        for path in (ROOT / source_root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(
                    name == "kirakira_agent" or name.startswith("kirakira_agent.")
                    for name in names
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


from scripts.check_change_impact import impacted_tests


def test_context_change_selects_semantic_and_runtime_contracts() -> None:
    selected = impacted_tests(["plugins/models/query_compaction.py"])
    assert "tests/test_session.py" in selected
    assert "tests/test_query_compaction.py" in selected


def test_scheduler_change_stays_targeted_but_keeps_global_boundaries() -> None:
    selected = impacted_tests(["plugins/scheduler/service.py"])
    assert "tests/test_scheduler.py" in selected
    assert "tests/test_architecture_boundaries.py" in selected
    assert "tests/test_proactive.py" not in selected
