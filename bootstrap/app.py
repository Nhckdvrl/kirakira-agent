"""CLI selection and serving the composed runtime."""

import argparse
import asyncio
import importlib.util
import os
import signal
import sys
from pathlib import Path

from agent.config import config_value, load_dotenv, load_toml_config
from agent.core.runtime import CoreRuntime
from agent.supervisor import RESTART_EXIT_CODE
from bootstrap.runtime import build_runtime
from core.schema import JsonDict


async def runtime_repl(
    runtime: CoreRuntime, workdir: Path, session_id: str | None = None
) -> None:
    """Backward-compatible name for the line-oriented streaming client."""

    from frontend.tui.plain import runtime_plain_repl

    await runtime_plain_repl(runtime, workdir, session_id=session_id)


async def runtime_serve(runtime: CoreRuntime) -> int:
    """跑后台服务直到停止信号或 agent 自请求重启。

    返回进程退出码:重启提交成立时返回 RESTART_EXIT_CODE(75),supervisor 收到
    75 + 私有管道上的有效 commit 帧才会拉起下一代(照 Reference main.py:serve)。
    """
    tasks = await runtime.start_background()
    readiness = None
    boot_id = (
        os.getenv("AKASHIC_BOOT_ID", "") or os.getenv("KIRAKIRA_BOOT_ID", "")
    ).strip()
    supervised = (
        os.getenv("AKASHIC_SUPERVISED") == "1"
        or os.getenv("KIRAKIRA_SUPERVISED") == "1"
    )
    if supervised and boot_id:
        from bootstrap.runtime_readiness import RuntimeReadiness

        readiness = RuntimeReadiness(runtime.session_manager.workspace, boot_id)
        readiness.mark_ready()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    registered_signals: list[signal.Signals] = []
    for watched in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(watched, stop_event.set)
            registered_signals.append(watched)
        except (NotImplementedError, RuntimeError):
            pass
    restart_requested = False
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown_signal")
    restart_task = (
        asyncio.create_task(
            runtime.restart_coordinator.wait_committed(),
            name="restart_committed",
        )
        if runtime.restart_coordinator is not None
        else None
    )
    try:
        print("kirakira-agent server running. Ctrl+C to stop.")
        watched_tasks = {stop_task}
        if restart_task is not None:
            watched_tasks.add(restart_task)
        done, _ = await asyncio.wait(watched_tasks, return_when=asyncio.FIRST_COMPLETED)
        if restart_task is not None and restart_task in done:
            await restart_task
            restart_requested = True
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for pending_task in (stop_task, restart_task):
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()
        for watched in registered_signals:
            loop.remove_signal_handler(watched)
        if readiness is not None:
            readiness.clear()
        await runtime.stop_background(tasks)
    return RESTART_EXIT_CODE if restart_requested else 0


def resolve_workspace(
    cli_workspace: str | None,
    app_config: JsonDict,
    *,
    default: Path,
) -> Path:
    """按 --workspace > KIRAKIRA_WORKSPACE > config [runtime].workspace > 默认解析。

    运行时可写状态（session、记忆、附件、插件数据、workspace MCP）全部落在这个根下，
    不同 workspace 之间互不共享。
    """

    env_value = os.getenv("KIRAKIRA_WORKSPACE")
    configured = str(config_value(app_config, "runtime", "workspace", default="") or "")
    for candidate in (cli_workspace, env_value, configured):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        return Path(text).expanduser().resolve()
    return default


async def _main_async(args: argparse.Namespace, workdir: Path) -> int:
    from bootstrap.workspace_lock import WorkspaceInstanceLock

    workspace_lock = WorkspaceInstanceLock(workdir)
    workspace_lock.acquire()
    try:
        runtime = await build_runtime(
            workdir,
            enable_web=args.web,
            enable_telegram=args.telegram,
            enable_qq=args.qq,
            enable_qqbot=args.qqbot,
            config_path=args.config_path,
        )
        if getattr(args, "proactive", False):
            await _run_proactive_once(runtime)
        elif args.serve or args.web or args.telegram or args.qq or args.qqbot:
            return await runtime_serve(runtime)
        else:
            mode = choose_cli_mode(force_tui=args.tui, force_plain=args.plain)
            if mode == "tui":
                try:
                    from frontend.tui.app import runtime_tui
                except ModuleNotFoundError as exc:
                    if exc.name != "textual":
                        raise
                    print(
                        "Textual 未安装，自动回退到 plain 流式模式。"
                        "安装项目依赖后可使用全屏 TUI。",
                        file=sys.stderr,
                    )
                    await runtime_repl(runtime, workdir, session_id=args.session)
                else:
                    await runtime_tui(runtime, workdir, session_id=args.session)
            else:
                await runtime_repl(runtime, workdir, session_id=args.session)
        return 0
    finally:
        workspace_lock.release()


async def _run_proactive_once(runtime: CoreRuntime) -> None:
    """手动跑一次主动 tick 并打印状态，供演示/调试（不等电量定时器）。"""
    import json as _json

    tasks = []
    try:
        loop = runtime.proactive_loop
        if loop is None:
            print(
                "主动链路未启用。请在 config.toml 的 [proactive] 设 enabled=true "
                "并填好 [proactive.target]。"
            )
            return
        if runtime.channel_host is not None:
            await runtime.channel_host.start_all()
        tasks.append(
            asyncio.create_task(runtime.bus.dispatch_outbound(), name="bus_dispatch")
        )
        print("→ 执行一次主动 tick ...")
        await loop.tick_once()
        await runtime.bus.drain(timeout=10.0)
        print(_json.dumps(loop.status(), ensure_ascii=False, indent=2))
    finally:
        await runtime.stop_background(tasks)


def choose_cli_mode(
    *,
    force_tui: bool = False,
    force_plain: bool = False,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
    textual_available: bool | None = None,
) -> str:
    """Select a UI without importing optional TUI dependencies in scripts/CI."""

    if force_plain:
        return "plain"
    if force_tui:
        return "tui"
    stdin_tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    stdout_tty = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
    available = (
        importlib.util.find_spec("textual") is not None
        if textual_available is None
        else textual_available
    )
    return "tui" if stdin_tty and stdout_tty and available else "plain"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Kirakira Agent")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["memory"],
        help="Administrative command group (currently: memory).",
    )
    parser.add_argument(
        "memory_action",
        nargs="?",
        choices=[
            "doctor",
            "backup",
            "migrate",
            "verify",
            "rollback",
            "clear",
            "repair-kinds",
        ],
        help="Memory administration action.",
    )
    parser.add_argument(
        "--backup-id", default="", help="Backup id for memory verify/rollback."
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Explicit confirmation token for destructive memory actions.",
    )
    parser.add_argument(
        "--include-sessions",
        action="store_true",
        help="Also delete all persisted sessions during memory clear.",
    )
    parser.add_argument(
        "--clear-self",
        action="store_true",
        help="Also reset memory/SELF.md during memory clear.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned changes without writing (memory repair-kinds).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run background agent loop and configured channels.",
    )
    parser.add_argument("--web", action="store_true", help="Enable stdlib web channel.")
    parser.add_argument(
        "--telegram", action="store_true", help="Enable Telegram Bot API channel."
    )
    parser.add_argument(
        "--qq", action="store_true", help="Enable QQ OneBot webhook channel."
    )
    parser.add_argument(
        "--qqbot", action="store_true", help="Enable official Tencent QQBot channel."
    )
    parser.add_argument(
        "--proactive",
        action="store_true",
        help="Run one proactive tick now (demo/debug), print status, then exit.",
    )
    ui_group = parser.add_mutually_exclusive_group()
    ui_group.add_argument(
        "--tui",
        action="store_true",
        help="Force the full-screen Textual client for local interaction.",
    )
    ui_group.add_argument(
        "--plain",
        action="store_true",
        help="Use the line-oriented streaming client (also selected for pipes/CI).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "Named local conversation to resume. Without this flag, each launch "
            "starts a fresh empty conversation."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Runtime state root (sessions, memory, plugin data, workspace MCP). "
            "Overrides KIRAKIRA_WORKSPACE and config [runtime].workspace."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml. Defaults to ./config.toml.",
    )
    args = parser.parse_args(argv)
    # config 先于 workspace 解析：workspace 可以写在 config 里，但 config 本身不住在
    # workspace 内，否则会形成先有鸡还是先有蛋。
    cwd = Path(os.getcwd()).resolve()
    args.config_path = (
        Path(args.config).expanduser().resolve() if args.config else cwd / "config.toml"
    )
    # 解析 workspace 就要展开 config 里的 ${...} 引用（含 secret），所以先加载与
    # config 同目录的 .env；build_runtime 之后会再按 workspace 加载一次，setdefault 保证
    # 早绑定的值不被覆盖。
    load_dotenv(args.config_path.parent / ".env")
    workdir = resolve_workspace(
        args.workspace, load_toml_config(args.config_path), default=cwd
    )
    workdir.mkdir(parents=True, exist_ok=True)
    if not (
        os.environ.get("AKASHIC_SUPERVISED") == "1"
        or os.environ.get("KIRAKIRA_SUPERVISED") == "1"
    ):
        from agent.migrations import migrate_installation

        try:
            outcome = migrate_installation(args.config_path, workdir)
        except RuntimeError as exc:
            parser.exit(2, "workspace migration failed: %s\n" % exc)
        if outcome.state == "migrated":
            print("workspace migrations applied: %s" % len(outcome.migrations))
    if args.command == "memory":
        import json as _json

        from bootstrap.memory_admin import (
            backup,
            clear,
            doctor,
            migrate,
            repair_kinds,
            rollback,
            verify,
        )

        if not args.memory_action:
            parser.error(
                "memory requires doctor/backup/migrate/verify/rollback/clear/repair-kinds"
            )
        try:
            if args.memory_action == "doctor":
                result = doctor(workdir, project_root=cwd)
            elif args.memory_action == "backup":
                result = backup(workdir)
            elif args.memory_action == "migrate":
                result = migrate(workdir)
            elif args.memory_action == "verify":
                result = verify(workdir, backup_id=args.backup_id)
            elif args.memory_action == "repair-kinds":
                result = repair_kinds(workdir, dry_run=args.dry_run)
            elif args.memory_action == "rollback":
                result = rollback(workdir, backup_id=args.backup_id)
            else:
                result = clear(
                    workdir,
                    confirm=args.confirm,
                    include_sessions=args.include_sessions,
                    clear_self=args.clear_self,
                )
        except Exception as exc:
            parser.exit(2, "memory %s failed: %s\n" % (args.memory_action, exc))
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return
    exit_code = asyncio.run(_main_async(args, workdir))
    if exit_code:
        # 重启换代(75)必须作为进程退出码传给 supervisor;正常退出保持 return 语义。
        sys.exit(exit_code)
