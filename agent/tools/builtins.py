"""Kirakira Agent learning harness module."""

import asyncio
import base64
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from agent.plugins.snapshot import SnapshotToolView, get_current_runtime_snapshot
from agent.skills import SkillLoader
from agent.tools.registry import ToolRegistry, object_schema
from agent.tools.shell_command import resolve_shell
from agent.tools.text import truncate
from agent.tools.unified_exec import (
    DEFAULT_HARD_TIMEOUT_S,
    DEFAULT_INITIAL_YIELD_TIME_MS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_HARD_TIMEOUT_S,
    ShellProcessManager,
    format_execution_result,
)
from agent.tools.web import register_web_tools
from bus.events import (
    AttachmentKind,
    ChannelAttachment,
    ChannelMessage,
    DeliveryStatus,
    OutboundMessage,
)
from bus.queue import MessageBus
from core.schema import ToolResult, ToolSpec
from utils.process_group import owned_process_env


def safe_path(workdir: Path, path: str) -> Path:
    target = (workdir / path).resolve()
    root = workdir.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Path escapes workspace: %s" % path)
    return target


_VL_MAX_FILE_BYTES = 20 * 1024 * 1024  # 单张原始文件上限
_VL_MAX_DATA_URI_BYTES = 8 * 1024 * 1024  # base64 编码后 data URI 上限
_VL_MAX_EDGE = 4096  # 最长边像素上限，超限自动缩放


def _encode_image_data_uri(raw: bytes, mime: str) -> str:
    """读取图片并编码为 data URI，大图自动缩放压缩（参考 akashic-agent vision）。

    超限时抛 ValueError，带可操作的错误信息。
    """
    import io

    if len(raw) > _VL_MAX_FILE_BYTES:
        raise ValueError(
            "图片文件过大（%.1fMB），上限为 %.0fMB。请压缩或裁剪后重试。"
            % (len(raw) / 1024 / 1024, _VL_MAX_FILE_BYTES / 1024 / 1024)
        )
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise ValueError("当前环境未安装 Pillow，无法处理图片。") from exc

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
    except Exception as exc:  # noqa: BLE001 - Pillow 各种解码异常统一归一
        raise ValueError("图片文件无法解码或已损坏。请确认这是有效图片。") from exc

    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            canvas = Image.new("RGB", img.size, (255, 255, 255))
            alpha = img.getchannel("A") if "A" in img.getbands() else None
            canvas.paste(img.convert("RGB"), mask=alpha)
            img = canvas
        elif img.mode == "L":
            img = img.convert("RGB")

        raw_b64_len = len(base64.b64encode(raw).decode())
        if max(img.size) > _VL_MAX_EDGE or raw_b64_len > _VL_MAX_DATA_URI_BYTES:
            img.thumbnail((_VL_MAX_EDGE, _VL_MAX_EDGE))

        # 原图已在预算内：无损保留原格式（JPEG 高质量 / PNG）。
        if raw_b64_len <= _VL_MAX_DATA_URI_BYTES and max(img.size) <= _VL_MAX_EDGE:
            buf = io.BytesIO()
            if mime == "image/jpeg":
                img.save(buf, format="JPEG", quality=95, optimize=True)
                clean_mime = "image/jpeg"
            else:
                img.save(buf, format="PNG", optimize=True)
                clean_mime = "image/png"
            clean_b64 = base64.b64encode(buf.getvalue()).decode()
            if len(clean_b64) <= _VL_MAX_DATA_URI_BYTES:
                return "data:%s;base64,%s" % (clean_mime, clean_b64)

        best: bytes | None = None
        for quality in (85, 75, 65, 55, 45):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            candidate = buf.getvalue()
            best = candidate
            candidate_b64 = base64.b64encode(candidate).decode()
            if len(candidate_b64) <= _VL_MAX_DATA_URI_BYTES:
                return "data:image/jpeg;base64,%s" % candidate_b64

    if best is None:
        raise ValueError("图片压缩失败")
    raise ValueError(
        "图片压缩后仍然过大（%.1fMB base64），上限 %.0fMB。请继续压缩或裁剪。"
        % (
            len(base64.b64encode(best).decode()) / 1024 / 1024,
            _VL_MAX_DATA_URI_BYTES / 1024 / 1024,
        )
    )


def _toolsearch_normalize(query: str) -> set[str]:
    """把 query 归一化为搜索词集合（参考 akashic-agent search_backend）。

    lowercase 整串 + 空格切词 + CJK/非 CJK 边界切词 + CJK bigram/单字，
    让中文查询能命中工具 name/description，无需外部分词库。
    """
    query_lower = query.lower().strip()
    tokens: set[str] = {query_lower}
    for part in query_lower.split():
        tokens.add(part)
    for segment in re.split(r"([一-鿿]+)", query_lower):
        segment = segment.strip()
        if segment:
            tokens.add(segment)
    cjk = [c for c in query_lower if "一" <= c <= "鿿"]
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    tokens.update(cjk)
    tokens.discard("")
    return tokens


def _toolsearch_score(name: str, description: str, keywords: set[str]) -> int:
    """字段加权评分（参考 akashic-agent _score，仅用 name/description 字段）。

    名称 part 精确命中 10 / 部分命中 5 / 全名兜底 3；描述命中额外 +2。
    """
    name_parts = [p for p in name.lower().split("_") if p]
    name_lower = name.lower()
    desc_lower = description.lower()
    score = 0
    for kw in keywords:
        if kw in name_parts:
            score += 10
        elif any(kw in part or part in kw for part in name_parts):
            score += 5
        elif kw in name_lower:
            score += 3
        if kw in desc_lower:
            score += 2
    return score


class WorkspaceTools:
    def __init__(
        self,
        workdir: Path,
        skill_loader: SkillLoader,
        registry: ToolRegistry | None = None,
        bus: MessageBus | None = None,
        push_tool: Any = None,
    ) -> None:
        self.workdir = workdir.resolve()
        self.skill_loader = skill_loader
        self.registry = registry
        self.bus = bus
        self.push_tool = push_tool
        self._mutation_locks: dict[str, threading.Lock] = {}
        self._shell_processes = ShellProcessManager()

    async def bash(
        self,
        command: str,
        timeout: Optional[int] = None,
        run_in_background: bool = False,
        auto_promote: bool = True,
        tty: bool = False,
        shell: str | None = None,
        login: bool = True,
        yield_time_ms: int = DEFAULT_INITIAL_YIELD_TIME_MS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> str:
        if self._dangerous_shell_command(command):
            return "Error: Dangerous command blocked"
        hard_timeout = max(
            1, min(int(timeout or DEFAULT_HARD_TIMEOUT_S), MAX_HARD_TIMEOUT_S)
        )
        if auto_promote:
            hard_timeout = min(hard_timeout, 600)
        try:
            resolved = resolve_shell(shell)
            result = await self._shell_processes.exec_command(
                command=command,
                argv=resolved.derive_argv(command, login=login),
                cwd=self.workdir,
                env=owned_process_env({}),
                tty=bool(tty),
                yield_time_ms=max(0, int(yield_time_ms)),
                max_output_tokens=max(0, int(max_output_tokens)),
                hard_timeout_s=hard_timeout,
                owner_session_key=self._shell_owner(),
                return_immediately=bool(run_in_background),
            )
            return self._compat_shell_payload(
                result,
                command=command,
                background_start=bool(run_in_background),
                auto_promoted=(
                    not run_in_background and result.execution_id is not None
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return "Error: %s" % exc

    async def task_output(
        self,
        task_id: str,
        block: bool = False,
        timeout_ms: int = 30000,
        offset: int = 0,
    ) -> str:
        del offset  # unified executions return new output chunks, not cumulative logs.
        try:
            execution_id = self._execution_id(task_id)
            result = await self._shell_processes.write_stdin(
                execution_id=execution_id,
                chars="",
                yield_time_ms=max(0, min(int(timeout_ms), 300_000)),
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                owner_session_key=self._shell_owner(),
                return_immediately=not bool(block),
            )
            return self._compat_shell_payload(result, task_id=execution_id)
        except (RuntimeError, ValueError) as exc:
            return "Error: %s" % exc

    async def write_stdin(
        self,
        execution_id: int,
        chars: str = "",
        yield_time_ms: int = 1000,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> str:
        try:
            result = await self._shell_processes.write_stdin(
                execution_id=int(execution_id),
                chars=chars,
                yield_time_ms=int(yield_time_ms),
                max_output_tokens=max(0, int(max_output_tokens)),
                owner_session_key=self._shell_owner(),
            )
            return self._compat_shell_payload(result, task_id=int(execution_id))
        except (RuntimeError, ValueError) as exc:
            return "Error: %s" % exc

    async def task_stop(self, task_id: str) -> str:
        try:
            execution_id = self._execution_id(task_id)
        except ValueError:
            return json.dumps({"task_id": task_id, "status": "not_found"})
        stopped = await self._shell_processes.terminate_execution(
            execution_id,
            owner_session_key=self._shell_owner(),
        )
        # Stopping an already completed task is idempotent for the legacy API.
        return json.dumps(
            {
                "task_id": execution_id,
                "execution_id": execution_id,
                "status": "stopped" if stopped else "stopped",
            }
        )

    async def shutdown(self) -> None:
        report = await self._shell_processes.shutdown()
        if report.failures:
            failed = ",".join(str(item) for item in report.failed_execution_ids)
            raise RuntimeError("shell cleanup failed for execution ids: %s" % failed)

    async def cleanup_shell_owner(self, owner: str) -> None:
        report = await self._shell_processes.terminate_owner(owner)
        if report.failures:
            failed = ",".join(str(item) for item in report.failed_execution_ids)
            raise RuntimeError(
                "shell cleanup failed for owner %s execution ids: %s" % (owner, failed)
            )

    def _shell_owner(self) -> str:
        if self.registry is not None:
            session_key = str(self.registry.context.get("session_key") or "").strip()
            if session_key:
                return session_key
        return "workspace:%s" % self.workdir

    @staticmethod
    def _execution_id(task_id: object) -> int:
        try:
            value = int(task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown background task '%s'" % task_id) from exc
        if value <= 0:
            raise ValueError("Unknown background task '%s'" % task_id)
        return value

    @staticmethod
    def _compat_shell_payload(
        result: Any,
        *,
        command: str | None = None,
        task_id: int | None = None,
        background_start: bool = False,
        auto_promoted: bool = False,
    ) -> str:
        payload = json.loads(format_execution_result(result, command=command))
        execution_id = (
            result.execution_id if result.execution_id is not None else task_id
        )
        if execution_id is not None:
            payload["task_id"] = execution_id
        if background_start and execution_id is not None:
            payload["background_task_id"] = execution_id
        running = payload["process_status"] == "running"
        payload["status"] = "running" if running else "completed"
        payload["done"] = not running
        payload["elapsed_ms"] = payload["wall_time_ms"]
        payload["auto_promoted"] = bool(auto_promoted)
        return json.dumps(payload, ensure_ascii=False)

    def read_file(self, path: str, limit: Optional[int] = None, offset: int = 0) -> str:
        target = safe_path(self.workdir, path)
        if not target.is_file():
            return "Error: File does not exist: %s" % path
        with target.open("rb") as handle:
            head = handle.read(4096)
        if b"\x00" in head:
            return "Error: Binary file cannot be read as text: %s" % path
        all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(0, int(offset))
        lines = all_lines[offset:]
        if limit is not None and limit >= 0 and limit < len(lines):
            lines = lines[:limit] + ["... (%d more lines)" % (len(lines) - limit)]
        return truncate("\n".join(lines))

    def list_dir(self, path: str = ".") -> str:
        target = safe_path(self.workdir, path)
        if not target.exists():
            return "Error: Path does not exist: %s" % path
        if not target.is_dir():
            return "Error: Path is not a directory: %s" % path
        rows = []
        for item in sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            rel = item.relative_to(self.workdir)
            kind = "dir" if item.is_dir() else "file"
            size = "" if item.is_dir() else str(item.stat().st_size)
            rows.append("%s\t%s\t%s" % (kind, rel, size))
        return truncate("\n".join(rows) or "(empty)")

    def write_file(self, path: str, content: str) -> str:
        target = safe_path(self.workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock(target):
            self._atomic_write_text(target, content)
        return "Wrote %d bytes to %s" % (len(content), path)

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        target = safe_path(self.workdir, path)
        if not old_text:
            return "Error: old_text cannot be empty"
        with self._mutation_lock(target):
            content = target.read_text(encoding="utf-8", errors="replace")
            count = content.count(old_text)
            if count == 0:
                return "Error: Text not found in %s" % path
            if count > 1 and not replace_all:
                return (
                    "Error: Text occurs %d times in %s; provide a unique old_text "
                    "or set replace_all=true" % (count, path)
                )
            replacements = count if replace_all else 1
            self._atomic_write_text(
                target, content.replace(old_text, new_text, replacements)
            )
        return "Edited %s (%d replacement%s)" % (
            path,
            replacements,
            "s" if replacements != 1 else "",
        )

    def load_skill(self, name: str) -> str:
        return self.skill_loader.load(name)

    def request_user_confirmation(self, prompt: str) -> ToolResult:
        """显式标记本轮需要用户确认后才能继续。

        照 Reference agent/tools/request_user_confirmation.py:这是一个**标记**,
        不是执行前闸门——它不阻止任何工具运行,只把 ``mobile_attention`` 抬到
        turn 级,让渠道能把这一轮渲染成"等待确认"。真正的拦截能力是
        ``tool_hooks`` 的 pre-hook ``deny``。
        """
        text = str(prompt).strip()
        if not text:
            return ToolResult("", "Error: prompt 不能为空", is_error=True)
        if len(text) > 500:
            return ToolResult("", "Error: prompt 不能超过 500 字符", is_error=True)
        return ToolResult(
            "",
            "已标记等待用户确认：%s" % text,
            mobile_attention="confirmation",
        )

    async def vision(self, image_paths, prompt: str = "请详细描述并分析图片。") -> str:
        from infra.providers.llm_provider import OpenAICompatibleClient

        if isinstance(image_paths, str):
            image_paths = [image_paths]
        if not isinstance(image_paths, list) or not image_paths:
            return "Error: image_paths must contain at least one image"
        model = os.getenv("VISION_MODEL_ID", "").strip()
        if not model:
            return "Error: VISION_MODEL_ID is not configured"
        content = [{"type": "text", "text": prompt}]
        total = 0
        for raw_path in image_paths[:8]:
            path = safe_path(self.workdir, str(raw_path))
            if not path.is_file():
                return "Error: Image does not exist: %s" % raw_path
            data = path.read_bytes()
            mime = self._image_mime(data)
            if not mime:
                return "Error: Unsupported or invalid image: %s" % raw_path
            # 参考 akashic-agent：大图用 Pillow 自动缩放/压缩到预算内，避免直接失败。
            try:
                data_uri = await asyncio.to_thread(_encode_image_data_uri, data, mime)
            except ValueError as exc:
                return "Error: %s: %s" % (raw_path, exc)
            total += len(data_uri)
            if total > 24 * 1024 * 1024:
                return "Error: Total image input exceeds the data budget"
            content.append({"type": "image_url", "image_url": {"url": data_uri}})
        client = OpenAICompatibleClient(
            base_url=os.getenv("VISION_BASE_URL")
            or os.getenv("OPENAI_COMPATIBLE_BASE_URL"),
            api_key=os.getenv("VISION_API_KEY")
            if os.getenv("VISION_API_KEY") is not None
            else os.getenv("OPENAI_COMPATIBLE_API_KEY", ""),
        )
        response = await asyncio.to_thread(
            client.complete,
            [{"role": "user", "content": content}],
            [],
            "你是视觉分析助手。只根据提供的图片和问题回答。",
            model,
            2048,
        )
        return response.text or "Error: Vision model returned an empty response"

    async def tool_search(self, query: str = "", limit: int = 20) -> str:
        # 必须是 async：只有在 turn 自己的 task 里才能读到本轮锁定的快照，
        # 而 MCP 工具只存在于快照中，不在基础注册表里。
        if self.registry is None:
            return "[]"
        view = SnapshotToolView(self.registry, get_current_runtime_snapshot())
        query = query.strip()
        if not query:
            return json.dumps(
                {"matched": [], "unlocked": [], "tip": "query is required"},
                ensure_ascii=False,
            )
        selected = []
        if query.lower().startswith("select:"):
            requested = [item.strip() for item in query[7:].split(",") if item.strip()]
            selected = [name for name in requested if view.has(name)]
            missing = [name for name in requested if not view.has(name)]
            matched = [
                {
                    "name": name,
                    "description": view.get_tool(name).spec.description,
                    "input_schema": view.get_tool(name).spec.input_schema,
                    "risk": view.get_tool(name).meta.risk,
                    "source_type": view.get_tool(name).meta.source_type,
                    "source_name": view.get_tool(name).meta.source_name,
                }
                for name in selected
            ]
            return json.dumps(
                {"matched": matched, "unlocked": selected, "missing": missing},
                ensure_ascii=False,
                indent=2,
            )
        keywords = _toolsearch_normalize(query)
        matches = []
        for spec in view.specs():
            score = (
                _toolsearch_score(spec.name, spec.description, keywords)
                if keywords
                else 1
            )
            if score:
                matches.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "score": score,
                        "input_schema": spec.input_schema,
                        "risk": view.get_tool(spec.name).meta.risk,
                        "source_type": view.get_tool(spec.name).meta.source_type,
                        "source_name": view.get_tool(spec.name).meta.source_name,
                    }
                )
        matches.sort(key=lambda item: (item["score"], item["name"]), reverse=True)
        matched = matches[: max(1, min(20, int(limit)))]
        return json.dumps(
            {
                "matched": matched,
                "unlocked": [item["name"] for item in matched],
            },
            ensure_ascii=False,
            indent=2,
        )

    async def message_push(
        self,
        channel: str,
        chat_id: str,
        message: str = "",
        file: str = "",
        image: str = "",
    ) -> str:
        channel = channel.strip()
        chat_id = chat_id.strip()
        if not channel or not chat_id:
            return "Error: channel and chat_id are required"
        if not message and not file and not image:
            return "Error: message, file, or image is required"
        if self.push_tool is not None:
            attachments = []
            if file:
                attachments.append(
                    ChannelAttachment(AttachmentKind.FILE, file, Path(file).name)
                )
            if image:
                attachments.append(ChannelAttachment(AttachmentKind.IMAGE, image))
            receipt = await self.push_tool.dispatch(
                ChannelMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=message,
                    attachments=tuple(attachments),
                )
            )
            if receipt.status is DeliveryStatus.SUCCESS:
                return "消息已发送"
            if receipt.status is DeliveryStatus.PARTIAL:
                return "消息部分送达：%s" % (receipt.detail or "渠道未提交全部内容")
            return "发送失败：%s" % (receipt.detail or "渠道未提交消息")
        if self.bus is None:
            return "Error: Message delivery is not available"
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=message,
                media=[item for item in (file, image) if item],
            )
        )
        return "已发送"

    def _dangerous_shell_command(self, command: str) -> bool:
        normalized = re.sub(r"\s+", " ", command.strip().lower())
        blocked_literals = [
            "sudo ",
            "shutdown",
            "reboot",
            "> /dev/",
            "mkfs",
            "dd if=",
            ":(){",
            "chmod -r 777 /",
            "chown -r ",
        ]
        if any(item in normalized for item in blocked_literals):
            return True
        blocked_patterns = [
            r"\brm\s+-[^\n;|&]*r[^\n;|&]*f[^\n;|&]*(?:/|\$home|~)",
            r"\brm\s+-[^\n;|&]*f[^\n;|&]*r[^\n;|&]*(?:/|\$home|~)",
        ]
        return any(re.search(pattern, normalized) for pattern in blocked_patterns)

    @staticmethod
    def _image_mime(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _mutation_lock(self, path: Path) -> threading.Lock:
        return self._mutation_locks.setdefault(str(path.resolve()), threading.Lock())

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        temp = path.with_name(".%s.%s.tmp" % (path.name, uuid4().hex))
        try:
            temp.write_text(content, encoding="utf-8")
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def build_workspace_registry(
    workdir: Path,
    skills_dir: Optional[Path] = None,
    bus: MessageBus | None = None,
    push_tool: Any = None,
) -> ToolRegistry:
    skill_loader = SkillLoader(skills_dir or (workdir / "skills"))
    registry = ToolRegistry()
    handlers = WorkspaceTools(
        workdir,
        skill_loader,
        registry,
        bus,
        push_tool,
    )
    registry.add_shutdown_callback(handlers.shutdown)
    registry.add_owner_cleanup_callback(handlers.cleanup_shell_owner)
    registry.register(
        ToolSpec(
            "bash",
            "Run a shell command in the workspace.",
            object_schema(
                {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "run_in_background": {"type": "boolean"},
                    "auto_promote": {"type": "boolean"},
                    "tty": {"type": "boolean"},
                    "shell": {"type": "string"},
                    "login": {"type": "boolean"},
                    "yield_time_ms": {"type": "integer"},
                    "max_output_tokens": {"type": "integer"},
                },
                ["command"],
            ),
        ),
        handlers.bash,
        risk="external-side-effect",
    )
    registry.register(
        ToolSpec(
            "task_output",
            "Poll output and status for a background shell task.",
            object_schema(
                {
                    "task_id": {"type": "integer"},
                    "block": {"type": "boolean"},
                    "timeout_ms": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                ["task_id"],
            ),
        ),
        handlers.task_output,
    )
    registry.register(
        ToolSpec(
            "write_stdin",
            "Write to or wait on a running shell execution and return the next output chunk.",
            object_schema(
                {
                    "execution_id": {"type": "integer"},
                    "chars": {"type": "string"},
                    "yield_time_ms": {"type": "integer"},
                    "max_output_tokens": {"type": "integer"},
                },
                ["execution_id"],
            ),
        ),
        handlers.write_stdin,
        risk="external-side-effect",
    )
    registry.register(
        ToolSpec(
            "task_stop",
            "Stop and clean up a background shell task.",
            object_schema({"task_id": {"type": "integer"}}, ["task_id"]),
        ),
        handlers.task_stop,
        risk="external-side-effect",
    )
    registry.register(
        ToolSpec(
            "read_file",
            "Read file contents from inside the workspace.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                ["path"],
            ),
        ),
        handlers.read_file,
        parallel_safe=True,
    )
    registry.register(
        ToolSpec(
            "list_dir",
            "List files and directories inside the workspace.",
            object_schema({"path": {"type": "string"}}, []),
        ),
        handlers.list_dir,
        parallel_safe=True,
    )
    registry.register(
        ToolSpec(
            "write_file",
            "Write content to a file inside the workspace.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                ["path", "content"],
            ),
        ),
        handlers.write_file,
        risk="write",
    )
    registry.register(
        ToolSpec(
            "edit_file",
            "Replace exact text in a workspace file.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                ["path", "old_text", "new_text"],
            ),
        ),
        handlers.edit_file,
        risk="write",
    )
    registry.register(
        ToolSpec(
            "load_skill",
            "Load specialized knowledge by skill name.",
            object_schema({"name": {"type": "string"}}, ["name"]),
        ),
        handlers.load_skill,
    )
    registry.register(
        ToolSpec(
            "request_user_confirmation",
            "当任务必须等待用户做出明确选择、授权或确认时调用。"
            "调用后，在本轮最终回复中清楚列出要确认的事项；"
            "普通提问、补充信息或修辞问句不要调用。",
            object_schema(
                {
                    "prompt": {
                        "type": "string",
                        "description": "需要用户明确确认的具体事项。",
                    }
                },
                ["prompt"],
            ),
        ),
        handlers.request_user_confirmation,
    )
    registry.register(
        ToolSpec(
            "vision",
            "Analyze one or more local image attachments using the configured vision model.",
            object_schema(
                {
                    "image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "prompt": {"type": "string"},
                },
                ["image_paths"],
            ),
        ),
        handlers.vision,
    )
    registry.register(
        ToolSpec(
            "tool_search",
            "Search available tools by name or description.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                [],
            ),
        ),
        handlers.tool_search,
    )
    registry.register(
        ToolSpec(
            "message_push",
            "Send one complete logical message to a configured channel/chat.",
            object_schema(
                {
                    "channel": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "message": {"type": "string"},
                    "file": {"type": "string"},
                    "image": {"type": "string"},
                },
                ["channel", "chat_id"],
            ),
        ),
        handlers.message_push,
    )
    register_web_tools(registry)
    return registry
