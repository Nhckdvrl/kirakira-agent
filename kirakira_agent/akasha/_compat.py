"""akasha 与 kirakira 之间的边界适配。

Reference 的 akasha 依赖两处框架设施:`infra/persistence/json_store.atomic_write_text`
与 `agent/plugins/manifest` 的插件数据目录解析。kirakira 没有同名模块,所以在这里补齐
**同语义**实现,而不是去改 akasha 自己的源文件——镜像文件保持逐字节可比对,doctor 的
漂移审计才能继续报 `drifted=[]`(同 `coremem/compat_worker.py` 的边界纪律)。

三处语义都照 Reference 保留:
- 原子写:同目录临时文件 + `os.replace` + fsync 父目录;
- 插件数据目录:`<workspace>/plugin-data/<name>-<marketplace>`,身份必须是单一安全路径段;
- 路径校验:必须归属 workspace,且现有路径逐级不得穿过符号链接。
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

_TEMP_ATTEMPTS = 8


def atomic_write_text(path: Path, content: str, *, domain: str = "json_store") -> None:
    """原子写入 UTF-8 文本,并在替换后持久化父目录。"""
    _ = domain  # Reference 用它做遥测分域;kirakira 无该设施,保留形参以免改调用点
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    for _attempt in range(_TEMP_ATTEMPTS):
        candidate = path.parent / ("%s.%s.tmp" % (path.name, secrets.token_hex(16)))
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        temporary = candidate
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        # 替换后持久化父目录,断电时目录项不至于丢失
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    raise OSError("无法为原子写入创建临时文件: %s" % path)


def workspace_plugin_data_dir(workspace: Path, plugin_name: str, marketplace: str) -> Path:
    """解析 workspace 内的插件数据目录,不创建或迁移数据。"""
    for label, value in (("name", plugin_name), ("marketplace", marketplace)):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
            raise ValueError("插件 %s 不是安全路径段: %r" % (label, value))
    return workspace.resolve(strict=False) / "plugin-data" / ("%s-%s" % (plugin_name, marketplace))


def builtin_plugin_data_dir(plugin_name: str, workspace: Path) -> Path:
    return workspace_plugin_data_dir(workspace, plugin_name, "builtin")


def validate_workspace_plugin_data_path(path: Path, workspace: Path) -> None:
    """校验插件数据路径归属 workspace,且现有路径不穿过符号链接。"""
    root = workspace.resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("插件数据目录越界: %s" % path) from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("插件数据目录不能穿过符号链接: %s" % current)


def ensure_workspace_plugin_data_dir(path: Path, workspace: Path) -> None:
    """安全创建 workspace 内的数据目录,并拒绝中间符号链接。"""
    validate_workspace_plugin_data_path(path, workspace)
    path.mkdir(parents=True, exist_ok=True)
    validate_workspace_plugin_data_path(path, workspace)
