"""Kirakira setup commands; execution delegates to the native host CLI."""
import argparse
import asyncio
import os
import runpy
import sys
from pathlib import Path


def main():
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", os.environ.get(
        "KIRAKIRA_PLUGIN_HOME", str(Path.home() / ".kirakira/plugins")))
    os.environ.setdefault("AKASHIC_EXTRA_PLUGIN_DIRS", str(Path(__file__).resolve().parents[1] / "plugins"))
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    # A legacy Kirakira config must not enter upstream's unrelated installation
    # migration chain. Import into a separate workspace with the explicit tool.
    if not {"--help", "-h"} & set(sys.argv[1:]):
        import tomllib
        args = sys.argv[1:]
        config = Path("config.toml")
        if "--config" in args and args.index("--config") + 1 < len(args):
            config = Path(args[args.index("--config") + 1])
        if command not in {"init", "configure-step-plan"} and config.is_file():
            with config.open("rb") as file:
                raw = tomllib.load(file)
            if set(raw) - {"runtime"} or set(raw.get("runtime", {})) - {"workspace"}:
                raise SystemExit("Legacy Kirakira configuration: import into a separate workspace with python -m scripts.migrate_legacy_workspace --source OLD --destination NEW, then use NEW/config.toml.")
    if command in {"init", "configure-step-plan"}:
        parser = argparse.ArgumentParser(prog="kirakira-agent " + command)
        parser.add_argument("--workspace", type=Path, required=True)
        if command == "init":
            parser.add_argument("--profile", choices=("headless", "web"), default="headless")
            args = parser.parse_args(sys.argv[2:])
            from .runtime import initialize_workspace
            workspace = initialize_workspace(args.workspace, profile=args.profile)
            print(f"Initialized {workspace}. Start: kirakira-agent gateway --config {workspace / 'config.toml'} --workspace {workspace}")
        else:
            parser.add_argument("--key-file", required=True, type=Path)
            args = parser.parse_args(sys.argv[2:])
            from .runtime import configure_step_plan, runtime
            async def configure():
                async with runtime(args.workspace) as core:
                    await configure_step_plan(core, args.key_file)
            asyncio.run(configure())
            print("Step5 configured on Step Token Plan.")
        return
    runpy.run_module("kirakira_agent._upstream_main", run_name="__main__")
