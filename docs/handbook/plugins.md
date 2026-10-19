# 插件开发与管理

插件用代码声明能力。每个插件根目录必须有 `plugin.py`，其中导出 `async def apply(ctx)`；已有 `Plugin` 子类继续由 SDK 适配；旧版
`.aka-plugin/plugin.json` 已不再使用。

```text
<workspace>/.kirakira/
├── manifest.toml          # 只记录启用状态
├── plugins/<plugin_id>/   # 插件代码
│   └── plugin.py
└── plugin-data/<plugin_id>/ # 插件持久数据
```

## 最小插件

```python
from agent.plugins import Plugin


class DemoPlugin(Plugin):
    name = "demo"
    version = "1.0.0"
    desc = "演示插件"
```

插件还可以声明：

- lifecycle phase 模块；
- `@tool` 工具和 `@on_tool_pre` 工具拦截器；
- skills 目录；
- MCP server；
- Channel；
- Proactive source 和模块。

详细扩展面见[插件架构](../architecture/plugins.md)。

## 管理工具

| 工具 | 作用 | 生效时间 |
| --- | --- | --- |
| `plugin_install` | 从本地目录或 HTTPS Git 地址安装 | 热重载后生效 |
| `plugin_enable` | 启用已安装插件 | 热重载后生效 |
| `plugin_disable` | 停用插件 | 热重载后生效 |
| `plugin_uninstall` | 卸载代码，保留 plugin-data | 热重载后生效 |
| `plugin_list` | 查看插件、版本、能力和加载错误 | 立即 |
| `plugin_doctor` | 检查目录结构与能力声明 | 立即 |

这些操作不要求重启 runtime。安装来源中的代码会在随后的插件换代中加载，因此只应安装可信代码。
每次模型生成获取具体工具菜单。实际调用持有当前 owner；旧菜单不会自动改绑新实现，下一次生成重新读取可用工具。

## 启停清单

`<workspace>/.kirakira/manifest.toml` 只记录启用状态：

```toml
[plugins."demo"]
enabled = true
```

能力、路径和配置 schema 仍由 `plugin.py` 声明。清单中没有记录的插件默认启用。清单结构非法时
整体加载失败，避免把错误配置误解成“全部启用”。

## Skills 与 MCP

```python
from agent.plugins import McpServerSpec, Plugin


class DemoPlugin(Plugin):
    name = "demo"

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="demo-mcp",
                command=("python", "./server.py"),
                cwd=".",
            )
        ]
```

相对路径按插件根解析，不能越出插件目录。运行 MCP 时会注入
`KIRAKIRA_PLUGIN_DATA_DIR`。插件 MCP 与 Workspace MCP 使用同一套逐连接 owner 和实际调用排空语义，server
名称不能冲突。

## 配置与数据

- `config.toml`：插件默认配置；
- `config.local.toml`：本机覆盖，适合密钥和私有地址；
- `self.context.data_dir`：插件持久数据目录；
- `self.context.kv_store`：原子 JSON KV。

不要把运行状态写进插件代码目录。

## Lifecycle 注意事项

七个 phase 是 `before_turn`、`before_reasoning`、`prompt_render`、`before_step`、`after_step`、
`after_reasoning`、`after_turn`。模块可以用 priority 和 slot dependency 排序。

`prompt_render` 每次 context retry 都会重新运行，因此必须幂等，不应执行外部副作用。需要观测
上下文时使用 `ContextPrepared`、`ContextBudgetUpdated` 等 observer 事件；observer 失败只记录日志，
不会阻断回复。

## 失败边界

- 单个插件导入或初始化失败：撤销该插件已注册的能力，其他插件继续加载；
- skill 路径或 MCP `cwd` 越界：该插件加载失败；
- manifest 非法：整体失败，因为启用集合不再可信；
- 当前 MCP 连接失败：保留选择和错误，不自动恢复旧实现；
- terminate 按加载逆序执行，并要求幂等。

## API 3 的服务依赖与资源

API 3 不再通过 `context.memory` / `context.session_manager` 取得任意业务对象。
提供者使用稳定的版本合同发布能力，消费者显式声明并借用：

```python
from agent.plugins import Plugin, ServiceKey

COUNTER = ServiceKey[int]('example.counter.v1')

class Provider(Plugin):
    name = 'provider'
    async def prepare(self):
        self.context.provide(COUNTER, 42)

class Consumer(Plugin):
    name = 'consumer'
    @classmethod
    def requires(cls):
        return (COUNTER,)
    async def handle_request(self):
        async with self.context.use(COUNTER) as number:
            return number
```

两个类分别放在各自插件包；真实对象合同放在提供者可稳定导入的 contract 模块。
ServiceKey 按名字识别，类型参数用于静态检查。提供者维护稳定合同；破坏性 ABI 变更应使用新名字。
prepare 只构造贡献，不应借用尚未发布的依赖。宿主不会隐式安装缺失提供者。

用 `context.spawn(coroutine)` 登记任务，用 `context.on_close(closer)` 登记资源。
卸载与换代等待旧借用结束，再取消任务、等待 finally、逆序关闭资源并 terminate。
`use` 的作用域应覆盖整个实际使用过程，不能把对象泄露到租约外长期保存。
