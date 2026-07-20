# 插件系统

Kirakira 插件采用**全局只管启停、插件自己用代码声明能力**的模型。插件根目录必须提供
`plugin.py`，它既是入口也是发现插件的唯一标志。

**不再读取 `.aka-plugin/plugin.json`。** 早期版本用这个描述符文件声明名字、lifecycle entry、
skills 路径和 MCP 配置路径；现在这些全部由 `plugin.py` 用代码声明，描述符解析已删除。

```text
<workspace>/.kirakira/
├── manifest.toml                    ← 只记录 plugin_id 与 enabled
├── plugins/<name>/                  ← 已安装插件代码
│   └── plugin.py                    ← 必须存在
└── plugin-data/<name>/              ← 每个插件独立的持久状态
    └── kv.json
```

## 最小插件

```python
from kirakira_agent.plugins import Plugin


class DemoPlugin(Plugin):
    name = "demo"
    version = "1.0.0"
    desc = "最小插件"
```

## 声明 skills

```python
class DemoPlugin(Plugin):
    name = "demo"

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)
```

路径相对插件根解析，**声明了就必须存在**（写错路径当场失败，而不是安静地没加载）。
不声明时，若插件根下有 `skills/` 目录会作为兜底。

## 声明 MCP server

```python
from kirakira_agent.plugins import McpServerSpec, Plugin


class DemoPlugin(Plugin):
    name = "demo"

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="demo-mcp",
                command=("python", "./server.py"),
                env={"LOG_LEVEL": "INFO"},
                cwd=".",
            )
        ]
```

命令里的相对路径按插件根解析，裸命令名（如 `python`）交给 PATH。运行时会自动注入
`KIRAKIRA_PLUGIN_DATA_DIR` 环境变量指向该插件的 data dir。

插件 MCP 与 workspace MCP **共用同一套换代语义**，只是来源不同（`source="plugins"` vs
`source="workspace"`）。两个来源的 server 名字不能冲突，冲突会在发布时报错。

## 全局启停清单

`<workspace>/.kirakira/manifest.toml` 只回答"插件是否启用"：

```toml
[plugins."demo"]
enabled = true
```

**能力、路径、配置 schema 一律不写进全局清单。** 清单里没记录的插件默认启用。

清单损坏（未知顶层字段、`enabled` 不是布尔值、plugin_id 非法）会**直接失败**，不会静默当作
"全部启用"——否则一个手滑的引号会让所有插件在你不知情的情况下被启用。

## 核心约束

1. **`plugin.py` 是唯一入口**：没有它的目录不是插件，不会被发现。
2. **目录名应与 `name` 一致**：`plugin_install` 用来源目录名作为安装后的身份。
3. **安装期不导入 `plugin.py`**：`plugin_install` 只校验结构，**绝不执行刚下载的代码**，
   然后要求重启。任何"装完立刻热执行"的改法都会把这个安全边界打掉。
4. **声明路径不得越出插件根**：`skill_roots` 和 MCP `cwd` 都在插件自己的加载边界内校验，
   越界插件加载失败，但不影响其他插件。
5. **坏插件不阻塞好插件**：初始化失败会撤销该插件已注册的工具/hook，记入 `errors`，
   Runtime 继续启动。
6. **terminate 按加载逆序且幂等**。

## 4 种介入方式

| 方式 | 用途 |
| --- | --- |
| phase 模块（7 个 phase） | 在 turn 生命周期注入上下文、改写历史、提前返回 |
| `@on_tool_pre` | 拦截工具调用，可改参数或拒绝 |
| `@tool` | 注册新工具 |
| `channels()` | 提供新的消息渠道 |

phase 装饰器：`@on_before_turn`、`@on_before_reasoning`、`@on_prompt_render`、
`@on_before_step`、`@on_after_step`、`@on_after_reasoning`、`@on_after_turn`，
都支持 `priority`（大的先跑）。

`on_prompt_render` 不应再把所有内容拼进一个匿名字符串。它可以向
`system_sections_top/system_sections_bottom` 加 `PromptSectionRender(name, content, is_static)`，
通过 `disabled_sections` 禁用具名 core section，或用 `turn_injection_prompt/extra_hints` 加逐轮内容。
逐轮 hint 会进入 Context Frame；top/bottom section 进入 system。插件 section 的 `is_static` 当前用于
trace/观测，core cache 只负责内建 PromptBlock；插件若要缓存渲染结果必须自己按稳定签名管理，不能把
每轮时间戳伪装成 static。每个 context retry 都会重新调用 phase，插件逻辑必须幂等，不能在 prompt
hook 中执行外部副作用。

`ContextPrepared` 与 `ContextBudgetUpdated` 是 observer 事件，适合做本地指标或 trace；observer 失败
会记录日志但不阻断回复。不要在 observer 中改写 turn，它不是 intercept phase。

## 配置与数据

- `config.toml` / `config.local.toml`：插件配置，可选用 `ConfigModel` 校验。
- `self.context.data_dir`：持久数据目录。
- `self.context.kv_store`：原子 JSON KV。

**不要往插件仓库目录写运行状态**，那里应当视为只读代码。

## 失败会怎样

| 情况 | 结果 |
| --- | --- |
| `plugin.py` 没有 Plugin 子类 | 该插件记入 `errors`，其他插件正常加载 |
| `initialize()` 抛异常 | 撤销它已注册的工具/hook，记入 `errors`，不阻塞启动 |
| 声明的 skill 目录不存在 | 该插件加载失败，其他插件不受影响 |
| `manifest.toml` 结构非法 | **整体失败**，因为无法确定哪些插件该启用 |
| 插件 MCP 连不上 | 整批 MCP 候选作废，旧代际继续服务 |

## 排查

`plugin_list` 给出已加载插件、版本、skill 数、MCP server 名和加载错误。
`plugin_doctor` 检查结构与已加载插件的能力声明；**未加载的插件只做结构检查**，
因为检查能力需要执行它的代码。
