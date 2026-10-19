# 插件与业务提供者

所有普通业务 provider 接收上游原生 Context，通过声明式 `inject` 取得能力，通过 `provide` 发布服务，
通过 `effect` 持有清理责任。`bootstrap/distribution.py` 选择默认组合，通用内核不导入业务实现。

可安装的原生插件以 `plugin.py` 的 `async def apply(ctx)` 为入口：

```python
from agent.plugin_composition import ServiceKey

api_version = 3
name = "example"
inject = (ServiceKey("models.main.v1"),)

async def apply(ctx):
    model = ctx.require(inject[0])
    await ctx.provide(ServiceKey("example.reader.v1"), model)
```

原生 Context 由内核按 activation 创建，不能保存旧 Context 并期望它在重载后获得新权限。
插件配置和路径由 PluginRuntime 投影；业务数据位于 `.kirakira/plugin-data/<id>`。

`plugins/extensions` 负责文件发现、安装、启停、校验和重新挂载。已安装插件作为该 owner 的子树，
拥有独立的运行身份。替换父 provider 时，子树随父生命周期重新接入。

现有类式 Plugin API 3 通过集中适配继续支持工具、七个 phase、hook、Channel、Proactive source、
Skill、MCP、job 和托管服务。每个插件持有自己的 job/service host；没有全局类注册器。
`agent.plugins` 只是这个 SDK 的兼容导入入口。

工具通过实际 owner 调用许可保护。在原生插件中可使用
`await tools.register_owned(ctx, spec, handler, ...)`；工具撤销和在途调用归同一 Fiber。
phase/hook 只在真正调用贡献者时保留许可，下一次 phase 枚举当前贡献者。

安装、卸载与更新保留 plugin-data，禁止路径越界；当前代码失败时报告失败，资源清理失败保留责任。
详细生命周期见[所有权与局部更新](./ownership-and-reload.md)，操作入口见[插件手册](../handbook/plugins.md)。
