#!/usr/bin/env python3
"""Select and optionally run contract tests for changed high-risk owners."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPACT_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("session/", "agent/core/", "agent/model_runtime/"), (
        "tests/semantic/test_context_history_contract.py",
        "tests/test_runtime.py",
        "tests/test_core_execution.py",
        "tests/test_query_compaction.py",
        "tests/test_proactive_decisions.py",
        "tests/test_drift.py",
        "tests/test_memory_recovery.py",
        "tests/test_proactive_admission.py",
    )),
    (("agent/composition/",), ("tests/test_component_capabilities.py",)),
    (("plugins/akasha2/",), ("tests/test_akasha2_runtime.py",)),
    (("infra/providers/",), ("tests/test_model_recovery_transport.py",)),
    (("agent/tools/", "agent/subagent.py"), (
        "tests/test_tools.py",
        "tests/test_runtime.py",
    )),
    (("agent/scheduler.py",), ("tests/test_scheduler.py",)),
    (("proactive_v2/", "plugins/proactive_flow/", "plugins/wake_proactive/"), (
        "tests/test_proactive.py",
        "tests/test_proactive_lifecycle.py",
        "tests/test_interest_pool.py",
        "tests/test_proactive_decisions.py",
        "tests/test_proactive_admission.py",
    )),
    (("plugins/akasha/", "session/embedding_store.py", "bus/events_lifecycle.py", "scripts/replay_akasha.py"), (
        "tests/test_memory_commit_cycle.py",
        "tests/test_memory_recovery.py",
        "tests/test_akasha.py",
        "tests/test_memory_engine_wiring.py",
    )),
    (("plugins/drift_flow/", "agent/turns/policy.py"), (
        "tests/test_drift.py", "tests/test_drift_journal.py", "tests/test_drift_proposals.py", "tests/test_proactive_decisions.py",
    )),
    (("agent/migrations/", "migrations/", "bootstrap/workspace_lock.py"), (
        "tests/test_migration_runner.py",
        "tests/test_yoyo_migration_append_only.py",
    )),
    (("agent/plugins/", "agent/mcp/"), (
        "tests/test_plugin_extensibility.py",
        "tests/test_mcp.py",
        "tests/test_architecture_boundaries.py",
    )),
)
ALWAYS = (
    "tests/semantic/test_reference_independence_contract.py",
    "tests/test_architecture_boundaries.py",
)


def impacted_tests(paths: list[str]) -> list[str]:
    selected = set(ALWAYS)
    for path in paths:
        for prefixes, tests in IMPACT_RULES:
            if any(path == prefix or path.startswith(prefix) for prefix in prefixes):
                selected.update(tests)
    return sorted(selected)


def changed_paths(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", base, "--"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    untracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return sorted({line.strip() for line in (result.stdout + untracked.stdout).splitlines()
                   if line.strip()})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    paths = changed_paths(args.base)
    tests = impacted_tests(paths)
    missing = [test for test in tests if not (ROOT / test).is_file()]
    print(json.dumps({"changed": paths, "tests": tests, "missing": missing}, indent=2))
    if missing:
        return 2
    if not args.run:
        return 0
    return subprocess.run(
        ["uv", "run", "pytest", "-q", *tests], cwd=ROOT, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
