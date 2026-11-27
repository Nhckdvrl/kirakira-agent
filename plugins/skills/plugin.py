"""技能：内置、工作区与用户目录中的 Skill 目录、提示词材料、load_skill 工具和输入框技能建议。"""
from agent.plugin_composition import Context
from plugins.operations.contract import (
    OPERATIONS_V1,
    OperationRequest,
    OperationResult,
    OperationSpec,
)
from plugins.assets.contract import INSTALLED_ASSETS
from plugins.context.contract import MATERIALS_V4
from plugins.host_execution.contract import PROCESSES
from plugins.ui.contract import UI_SLOTS, PluginUiDefinition, PluginUiRpcInvalidRequest

from ._tool_boundary import TOOLS
from .skills import register_skills

api_version = 3
name = "skills"
version = "1.0.0"
desc = "提供技能目录与技能读取工具"
inject = (TOOLS, PROCESSES, MATERIALS_V4, INSTALLED_ASSETS)


async def apply(ctx: Context) -> None:
    _ = await ctx.require(INSTALLED_ASSETS).register(ctx, "skills", "skills")
    _ = await ctx.require(TOOLS).declare_group(ctx, always_on=True, description=desc)
    skills = await register_skills(ctx)

    # 输入框的 / 与 @ 技能建议是本插件自己的界面；宿主只提供插槽。
    async def plugin_ui(child: Context) -> None:
        async def query(method: str, payload: dict[str, object], *,
                        session_id: str | None, turn_id: str | None) -> dict[str, object]:
            if method != "skills.list" or payload:
                raise PluginUiRpcInvalidRequest("技能界面只支持读取技能目录")
            return {"items": list(await skills.list_skills())}

        _ = await child.require(UI_SLOTS).register_plugin_ui(
            child, PluginUiDefinition(module="plugin_ui.js", slots=("composer.suggest",)), query=query,
        )

    _ = await ctx.inject((UI_SLOTS,), plugin_ui, name="plugin-ui")

    # 技能目录原来只有输入框的建议界面能读。Agent 想知道"我现在有哪些技能"要么靠
    # 提示词里写死，要么没有路径；镜像成 operation 之后两边读同一个 list_skills。
    async def register_ops(child: Context) -> None:
        async def handle_list(_request: OperationRequest) -> OperationResult:
            items = list(await skills.list_skills())
            return OperationResult(True, f"已装载 {len(items)} 个技能",
                                   data={"items": items})

        _ = await child.require(OPERATIONS_V1).register(child, OperationSpec(
            operation_id="skills.list",
            path=("skills", "list"),
            summary="列出当前已装载的技能目录",
            handler=handle_list,
            mirrors=("ui:skills/skills.list",),
        ))

    _ = await ctx.inject((OPERATIONS_V1,), register_ops, name="operations")
