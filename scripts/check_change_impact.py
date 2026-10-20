#!/usr/bin/env python3
"""Select and optionally run contract tests for changed high-risk owners."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPACT_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        (
            "plugins/jobs/",
            "plugins/conversation/",
            "plugins/turn_projection/",
            "plugins/reply/",
            "plugins/reply_program/",
            "plugins/delivery/",
            "plugins/react/",
            "plugins/context/conversation.py",
        ),
        (
            "tests/test_reply_programs.py",
            "tests/test_runtime.py",
            "tests/test_runtime_lifecycle.py",
        ),
    ),
    (
        (
            "plugins/projects/factory.py",
            "plugins/ledger/owner_store.py",
            "plugins/ledger/manager.py",
            "plugins/projects/",
            "plugins/working_directory/",
            "frontend/workspace_ui.py",
            "plugins/react/reasoner.py",
            "plugins/tools/builtins.py",
            "plugins/subagent/service.py",
            "plugins/control/",
            "infra/channels/web_chat_channel.py",
        ),
        ("tests/test_workspace_features.py",),
    ),
    (
        (
            "bootstrap/runtime.py",
            "bootstrap/app.py",
            "plugins/channels/factory.py",
            "plugins/proactive_flow/factory.py",
            "plugins/tools/factory.py",
            "plugins/runtime_restart/tools.py",
        ),
        (
            "tests/test_runtime_lifecycle.py",
            "tests/test_cli.py",
            "tests/test_channels.py",
            "tests/test_plugin_extensibility.py",
            "tests/test_memory_tool_provider.py",
        ),
    ),
    (
        ("plugins/memory_tools/",),
        (
            "tests/test_memory_tool_provider.py",
            "tests/test_memory_engine_wiring.py",
            "tests/test_akasha.py",
            "tests/test_memory_m1.py",
        ),
    ),
    (
        ("plugins/ledger/", "plugins/reply_program/", "plugins/models/"),
        (
            "tests/test_session.py",
            "tests/test_runtime.py",
            "tests/test_runtime_lifecycle.py",
            "tests/test_core_execution.py",
            "tests/test_query_compaction.py",
            "tests/test_proactive_decisions.py",
            "tests/test_drift.py",
            "tests/test_memory_recovery.py",
            "tests/test_proactive_admission.py",
        ),
    ),
    (
        (
            "agent/composition/",
            "agent/plugin_composition/",
            "bootstrap/distribution.py",
            "bootstrap/application.py",
        ),
        (
            "tests/test_composition_graph.py",
            "tests/test_plugin_graph.py",
            "tests/test_runtime_lifecycle.py",
        ),
    ),
    (("plugins/akasha2/",), ("tests/test_akasha2_runtime.py",)),
    (("infra/providers/",), ("tests/test_model_recovery_transport.py",)),
    (
        ("plugins/tools/", "plugins/subagent/service.py"),
        (
            "tests/test_tools.py",
            "tests/test_runtime.py",
        ),
    ),
    (
        ("plugins/scheduler/",),
        (
            "tests/test_scheduler.py",
            "tests/test_scheduler_migration.py",
            "tests/test_memory_source_policy.py",
            "tests/test_runtime.py",
            "tests/test_memory_recovery.py",
        ),
    ),
    (
        ("proactive_v2/", "plugins/proactive_flow/", "plugins/wake_proactive/"),
        (
            "tests/test_proactive.py",
            "tests/test_proactive_lifecycle.py",
            "tests/test_interest_pool.py",
            "tests/test_proactive_decisions.py",
            "tests/test_proactive_admission.py",
        ),
    ),
    (
        (
            "plugins/akasha/",
            "plugins/ledger/embedding_store.py",
            "bus/events_lifecycle.py",
            "scripts/replay_akasha.py",
        ),
        (
            "tests/test_memory_commit_cycle.py",
            "tests/test_memory_recovery.py",
            "tests/test_akasha.py",
            "tests/test_memory_engine_wiring.py",
        ),
    ),
    (
        ("plugins/drift_flow/", "plugins/react/policy.py"),
        (
            "tests/test_drift.py",
            "tests/test_drift_journal.py",
            "tests/test_drift_proposals.py",
            "tests/test_proactive_decisions.py",
        ),
    ),
    (
        ("agent/migrations/", "migrations/", "agent/workspace_lock.py"),
        (
            "tests/test_migration_runner.py",
            "tests/test_scheduler_migration.py",
            "tests/test_yoyo_migration_append_only.py",
        ),
    ),
    (
        ("agent/plugins/", "plugins/extensions/", "plugins/mcp/"),
        (
            "tests/test_plugin_extensibility.py",
            "tests/test_mcp.py",
            "tests/test_architecture_boundaries.py",
        ),
    ),
)
ALWAYS = ("tests/test_architecture_boundaries.py",)


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
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return sorted(
        {
            line.strip()
            for line in (result.stdout + untracked.stdout).splitlines()
            if line.strip()
        }
    )


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
