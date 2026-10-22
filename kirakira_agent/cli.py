"""Public setup commands and delegation to the shared runtime entry point."""

import argparse
import asyncio
import os
import runpy
import sys
import tomllib
from pathlib import Path

SETUP_COMMANDS = frozenset({
    "init", "upgrade", "configure-model", "configure-step-plan",
    "configure-memory", "configure-proactive",
})
RUNTIME_HELP = """运行与管理（沿 Runtime 原有命令进入）：
  gateway                       前台运行服务
  supervise                     运行 supervisor；无命令时也进入 supervisor
  exec --new|--session ID PROMPT 提交输入并等待结果
  app-server --stdio            程序化控制面
  dashboard                     单独启动工作台
  plugin-install / plugin-uninstall / plugin-status / plugin-doctor
  plugin-install-trusted-batch   安装已明确信任的离线插件批次

运行命令使用 --config PATH 和 --workspace PATH；具体帮助见 COMMAND --help。
配置命令需要先停止同一工作区实例。模型和记忆均使用项目同一个 Python 环境。
"""


def _path(value):
    return Path(value).expanduser().resolve()


def _parser():
    parser = argparse.ArgumentParser(
        prog="kirakira-agent",
        description="有长期记忆、可组合个性与主动交互的个人 AI 伙伴。",
        epilog=RUNTIME_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", title="工作区与配置")
    for name, description in (
        ("init", "创建空工作区，可选 Web 工作台"),
        ("configure-model", "配置聊天模型与凭据，并设为默认模型"),
        ("configure-memory", "配置本地向量模型与长期记忆"),
        ("configure-proactive", "启用主动交互并选择本地收件会话"),
        ("upgrade", "升级既有工作区内的已选组件，保留用户设置"),
        ("configure-step-plan", "配置可选的订阅模型提供方"),
    ):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--workspace", type=_path, required=True, help="工作区目录")
        if name == "init":
            command.add_argument("--profile", choices=("headless", "web"), default="headless")
        elif name == "configure-model":
            command.add_argument("--key-file", required=True, type=_path, help="只含 API key 的私有文件")
            command.add_argument("--model", required=True, help="提供方的模型名称")
            command.add_argument("--base-url", required=True, help="模型 API 地址")
            command.add_argument("--driver", default="openai-compatible")
            command.add_argument("--id", default="main", help="本地模型身份；切换另一模型时使用新身份")
            command.add_argument("--context-window", type=int, default=32768)
            command.add_argument("--max-output-tokens", type=int, default=4096)
            command.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
        elif name == "configure-step-plan":
            command.add_argument("--key-file", required=True, type=_path)
        elif name == "configure-memory":
            command.add_argument("--cache", required=True, type=_path, help="本机模型资产目录")
            command.add_argument("--model", help="向量模型名称；省略时使用默认中文编码器")
            command.add_argument("--download", action="store_true", help="显式允许首次下载权重")
            command.add_argument("--backfill-history", action="store_true", help="为旧闭段补算本地向量")
        elif name == "configure-proactive":
            command.add_argument("--session", required=True, help="已创建的本地收件会话 ID")
            command.add_argument("--timezone", default="Asia/Shanghai")
    return parser


def _check_workspace(parser, workspace):
    config = workspace / "config.toml"
    if not config.is_file():
        parser.error("工作区尚未初始化；先运行 kirakira-agent init --workspace PATH")
    _check_config(config)


def _check_config(config):
    if not config.is_file():
        return
    with config.open("rb") as file:
        raw = tomllib.load(file)
    if set(raw) - {"runtime"} or set(raw.get("runtime", {})) - {"workspace"}:
        raise SystemExit(
            "旧版工作区需要显式导入：python -m scripts.migrate_legacy_workspace "
            "--source OLD --destination NEW；随后使用 NEW/config.toml。"
        )


def _run_setup(parser, args):
    command, workspace = args.command, args.workspace
    if command != "init":
        _check_workspace(parser, workspace)
    if command in {"configure-model", "configure-step-plan"}:
        if not args.key_file.is_file() or not args.key_file.read_text().strip():
            parser.error("--key-file 必须指向只含 API key 的非空私有文件")
    if command == "configure-model":
        if not args.model.strip() or not args.base_url.strip() or not args.id.strip():
            parser.error("模型名称、API 地址与本地身份不能为空")
        if not 0 < args.max_output_tokens < args.context_window:
            parser.error("预算必须满足 0 < max-output-tokens < context-window")
    if command == "init":
        from .runtime import initialize_workspace

        initialize_workspace(workspace, profile=args.profile)
        print(f"已初始化 {workspace}。模型配置：kirakira-agent configure-model --help")
        print(f"启动：kirakira-agent gateway --config {workspace / 'config.toml'} --workspace {workspace}")
        return
    if command == "upgrade":
        from .setup import upgrade_workspace

        backup = upgrade_workspace(workspace)
        print(f"已更新工作区组件；原选择记录：{backup}")
        return
    if command == "configure-memory":
        from plugins.local_embedding.driver import DEFAULT_MODEL
        from .setup import prepare_embedding

        try:
            manifest = prepare_embedding(args.cache, args.model or DEFAULT_MODEL, download=args.download)
        except ModuleNotFoundError as error:
            if error.name != "fastembed":
                raise
            parser.error("本地记忆需要 memory 安装选项：uv sync --frozen --extra providers --extra memory")

    from .runtime import runtime

    async def configure():
        async with runtime(workspace) as core:
            if command == "configure-model":
                from .runtime import configure_chat_model

                await configure_chat_model(
                    core, args.key_file, model=args.model, endpoint=args.base_url,
                    driver_id=args.driver, model_id=args.id, connection_id=args.id,
                    context_window=args.context_window, max_output_tokens=args.max_output_tokens,
                    reasoning_effort=args.reasoning_effort,
                )
            elif command == "configure-step-plan":
                from .runtime import configure_step_plan

                await configure_step_plan(core, args.key_file)
            elif command == "configure-memory":
                from .setup import backfill_embeddings, configure_embedding

                await configure_embedding(core, manifest)
                if args.backfill_history:
                    print(await backfill_embeddings(core))
            else:
                from .setup import configure_proactive

                await configure_proactive(core, workspace, args.session, timezone=args.timezone)

    asyncio.run(configure())
    if command == "configure-model":
        print(f"默认聊天模型已配置：{args.id}")
    elif command == "configure-step-plan":
        print("订阅模型提供方已配置。")
    elif command == "configure-memory":
        print(f"本地记忆已配置：{manifest}")
    else:
        print(f"主动交互收件会话已配置：{args.session}")


def main():
    from .environment import configure_plugin_home, select_host_python

    select_host_python()
    configure_plugin_home()
    os.environ.setdefault("AKASHIC_EXTRA_PLUGIN_DIRS", str(Path(__file__).resolve().parents[1] / "plugins"))
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command in {"--help", "-h"}:
        _parser().print_help()
        return
    if command in SETUP_COMMANDS:
        parser = _parser()
        _run_setup(parser, parser.parse_args(args))
        return
    if not {"--help", "-h"} & set(args):
        # The runtime owns command parsing, including validation of missing values.
        config = Path("config.toml")
        if "--config" in args and args.index("--config") + 1 < len(args):
            config = _path(args[args.index("--config") + 1])
        _check_config(config)
    runpy.run_module("kirakira_agent._upstream_main", run_name="__main__")
