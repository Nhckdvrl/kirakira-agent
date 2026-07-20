"""Kirakira Agent learning harness module."""

import asyncio
import base64
from dataclasses import dataclass
import gzip
import ipaddress
import json
import html
import os
import re
import signal
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import zlib
from pathlib import Path
from typing import Optional
from uuid import uuid4

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.schema import ToolSpec
from kirakira_agent.snapshot import SnapshotToolView, get_current_runtime_snapshot
from kirakira_agent.session import SessionManager
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.registry import ToolRegistry, object_schema

OUTPUT_LIMIT = 50000
PRIVATE_FETCH_ENV = "KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH"
SHELL_FOREGROUND_SECONDS = 15


@dataclass
class _ShellTask:
    task_id: str
    command: str
    process: asyncio.subprocess.Process
    log_path: Path
    pump_task: asyncio.Task[None]
    timeout_task: asyncio.Task[None] | None
    started_at: float
    started_at_ms: int


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "enabled")


def safe_path(workdir: Path, path: str) -> Path:
    target = (workdir / path).resolve()
    root = workdir.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Path escapes workspace: %s" % path)
    return target


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (%d characters truncated)" % (len(text) - limit)


class WorkspaceTools:
    def __init__(
        self,
        workdir: Path,
        skill_loader: SkillLoader,
        memory: MemoryRuntime | None = None,
        session_manager: SessionManager | None = None,
        registry: ToolRegistry | None = None,
        bus: MessageBus | None = None,
    ) -> None:
        self.workdir = workdir.resolve()
        self.skill_loader = skill_loader
        self.memory = memory
        self.session_manager = session_manager
        self.registry = registry
        self.bus = bus
        self._mutation_locks: dict[str, threading.Lock] = {}
        self._shell_tasks: dict[str, _ShellTask] = {}
        self._shell_dir = self.workdir / ".kirakira" / "shell-tasks"

    async def bash(
        self,
        command: str,
        timeout: Optional[int] = None,
        run_in_background: bool = False,
        auto_promote: bool = True,
    ) -> str:
        if self._dangerous_shell_command(command):
            return "Error: Dangerous command blocked"
        explicit_timeout = timeout is not None
        hard_timeout = max(1, min(int(timeout or 120), 21_600))
        if auto_promote:
            hard_timeout = min(hard_timeout, 600)
        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            task = self._register_shell_task(
                command,
                process,
                hard_timeout if explicit_timeout or not run_in_background else None,
            )
            if run_in_background:
                return self._shell_task_payload(task, auto_promoted=False)
            foreground_wait = (
                min(SHELL_FOREGROUND_SECONDS, hard_timeout)
                if auto_promote
                else hard_timeout
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()), timeout=foreground_wait
                )
            except asyncio.TimeoutError:
                if auto_promote and hard_timeout > foreground_wait:
                    return self._shell_task_payload(task, auto_promoted=True)
                await self._stop_shell_task(task, remove=False)
                output = self._read_shell_log(task)
                self._remove_shell_task(task)
                return "Error: Timeout (%ss)\n%s" % (hard_timeout, output)
            await task.pump_task
            output = self._read_shell_log(task)
            self._remove_shell_task(task)
            if process.returncode:
                return truncate(
                    "Error: Command exited with code %d\n%s"
                    % (process.returncode, output)
                )
            return truncate(output)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                self._kill_process_group(process)
                await process.wait()
            raise
        except TimeoutError:
            return "Error: Timeout (%ss)" % hard_timeout
        except OSError as exc:
            return "Error: %s" % exc

    async def task_output(
        self,
        task_id: str,
        block: bool = False,
        timeout_ms: int = 30000,
        offset: int = 0,
    ) -> str:
        task = self._shell_tasks.get(task_id)
        if task is None:
            return "Error: Unknown background task '%s'" % task_id
        if block and task.process.returncode is None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task.process.wait()),
                    timeout=max(0.0, min(int(timeout_ms), 30_000) / 1000.0),
                )
            except asyncio.TimeoutError:
                pass
        if task.process.returncode is not None:
            await asyncio.gather(task.pump_task, return_exceptions=True)
        text = self._read_shell_log(task)
        offset = max(0, min(int(offset), len(text)))
        elapsed_ms = int((time.monotonic() - task.started_at) * 1000)
        return json.dumps(
            {
                "task_id": task_id,
                "status": "running" if task.process.returncode is None else "completed",
                "done": task.process.returncode is not None,
                "exit_code": task.process.returncode,
                "elapsed_ms": elapsed_ms,
                "next_offset": len(text),
                "output": truncate(text[offset:]),
            },
            ensure_ascii=False,
        )

    async def task_stop(self, task_id: str) -> str:
        task = self._shell_tasks.get(task_id)
        if task is None:
            return json.dumps({"task_id": task_id, "status": "not_found"})
        await self._stop_shell_task(task, remove=True)
        return json.dumps({"task_id": task_id, "status": "stopped"})

    async def shutdown(self) -> None:
        for task in list(self._shell_tasks.values()):
            await self._stop_shell_task(task, remove=True)

    def read_file(
        self, path: str, limit: Optional[int] = None, offset: int = 0
    ) -> str:
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
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
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

    async def vision(self, image_paths, prompt: str = "请详细描述并分析图片。") -> str:
        from kirakira_agent.models.openai_compatible import OpenAICompatibleClient

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
            total += len(data)
            if total > 10 * 1024 * 1024:
                return "Error: Total image input exceeds 10 MB"
            mime = self._image_mime(data)
            if not mime:
                return "Error: Unsupported or invalid image: %s" % raw_path
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:%s;base64,%s"
                        % (mime, base64.b64encode(data).decode("ascii"))
                    },
                }
            )
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

    def compact(self) -> str:
        return "Compacting context."

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
                }
                for name in selected
            ]
            return json.dumps(
                {"matched": matched, "unlocked": selected, "missing": missing},
                ensure_ascii=False,
                indent=2,
            )
        terms = [term.lower() for term in re.findall(r"[\w:-]+", query)]
        matches = []
        for spec in view.specs():
            haystack = ("%s %s" % (spec.name, spec.description)).lower()
            score = sum(1 for term in terms if term in haystack) if terms else 1
            if score:
                matches.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "score": score,
                        "input_schema": spec.input_schema,
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

    def web_fetch(self, url: str, max_chars: int = 12000) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "Error: URL must start with http:// or https://"
        validation_error = self._validate_fetch_target(parsed)
        if validation_error:
            return validation_error
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "kirakira-agent/0.1 (+https://github.com/Nhckdvrl/kirakira-agent)",
                "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            method="GET",
        )
        owner = self

        class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, redirect_req, fp, code, msg, headers, newurl):
                redirect = urllib.parse.urlparse(newurl)
                if redirect.scheme not in ("http", "https") or not redirect.netloc:
                    raise urllib.error.URLError("unsafe redirect URL")
                error = owner._validate_fetch_target(redirect)
                if error:
                    raise urllib.error.URLError(error)
                return super().redirect_request(
                    redirect_req, fp, code, msg, headers, newurl
                )

        opener = urllib.request.build_opener(SafeRedirectHandler())
        try:
            with opener.open(req, timeout=30) as resp:
                content_type = resp.headers.get("content-type", "")
                content_encoding = resp.headers.get("content-encoding", "")
                final_url = resp.geturl()
                declared = int(resp.headers.get("content-length") or "0")
                if declared > 5 * 1024 * 1024:
                    return "Error: Response exceeds 5 MB"
                raw = resp.read(5 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            return "Error: HTTP %s while fetching %s" % (exc.code, url)
        except urllib.error.URLError as exc:
            return "Error: Fetch failed for %s: %s" % (url, exc.reason)
        if len(raw) > 5 * 1024 * 1024:
            return "Error: Response exceeds 5 MB"
        try:
            raw = self._decompress_web_body(raw, content_encoding)
        except (OSError, EOFError, zlib.error, ValueError) as exc:
            return "Error: Could not decode %s response from %s: %s" % (
                content_encoding or "compressed",
                url,
                exc,
            )
        if len(raw) > 5 * 1024 * 1024:
            return "Error: Decompressed response exceeds 5 MB"
        lowered_type = content_type.lower()
        if lowered_type and not any(
            item in lowered_type
            for item in ("text/", "json", "xml", "javascript", "x-www-form-urlencoded")
        ):
            return "Error: Unsupported response content type: %s" % content_type
        if self._looks_like_binary(raw):
            return "Error: Response appears to be binary data"
        text, charset = self._decode_web_body(raw, content_type)
        if self._looks_severely_garbled(text):
            return "Error: Response text is severely garbled or uses an unsupported encoding"
        title = self._extract_html_title(text)
        published_at = self._extract_published_at(text)
        if "html" in content_type.lower() or "<html" in text[:500].lower():
            text = self._html_to_text(text)
        source = {
            "url": final_url,
            "title": title or self._fallback_web_title(final_url),
        }
        if published_at:
            source["published_at"] = published_at
        if content_type:
            source["content_type"] = content_type
        if charset:
            source["charset"] = charset
        return json.dumps(
            {
                "source": source,
                "content": truncate(text.strip(), max(1000, int(max_chars))),
            },
            ensure_ascii=False,
            indent=2,
        )

    def web_search(self, query: str, limit: int = 5) -> str:
        q = query.strip()
        if not q:
            return "Error: Web search query must not be empty"
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "kirakira-agent/0.1"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                html_text = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            return "Error: Web search failed for %r: %s" % (q, exc)
        results = []
        for match in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", html_text):
            attributes = match.group(1)
            class_match = re.search(r"(?i)class=['\"]([^'\"]*)['\"]", attributes)
            if not class_match or "result__a" not in class_match.group(1).split():
                continue
            href_match = re.search(r"(?i)href=['\"]([^'\"]+)['\"]", attributes)
            if not href_match:
                continue
            href = html.unescape(href_match.group(1))
            title = self._html_to_text(match.group(2)).strip()
            if href.startswith("//duckduckgo.com/l/?") or href.startswith("/l/?"):
                parsed = urllib.parse.urlparse(
                    "https:" + href
                    if href.startswith("//")
                    else "https://duckduckgo.com" + href
                )
                qs = urllib.parse.parse_qs(parsed.query)
                href = qs.get("uddg", [href])[0]
            if title and href:
                results.append({"title": title, "url": href})
            if len(results) >= max(1, int(limit)):
                break
        if not results:
            return (
                "Error: Web search returned no structured results for %r. "
                "Try a different query or use web_fetch with a verified URL."
            ) % q
        # Preserve the original top-level list contract for existing callers.
        return json.dumps(results, ensure_ascii=False, indent=2)

    @staticmethod
    def _decompress_web_body(raw: bytes, content_encoding: str) -> bytes:
        encodings = [
            value.strip().lower()
            for value in content_encoding.split(",")
            if value.strip() and value.strip().lower() != "identity"
        ]
        for encoding in reversed(encodings):
            if encoding in ("gzip", "x-gzip"):
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            else:
                raise ValueError("unsupported content encoding %r" % encoding)
        return raw

    @staticmethod
    def _looks_like_binary(raw: bytes) -> bool:
        if not raw:
            return False
        sample = raw[:8192]
        if b"\x00" in sample:
            return True
        binary_controls = sum(
            1 for value in sample if value < 32 and value not in (9, 10, 12, 13)
        )
        return binary_controls / len(sample) > 0.08

    def _decode_web_body(self, raw: bytes, content_type: str) -> tuple[str, str]:
        if not raw:
            return "", "utf-8"
        declared_match = re.search(
            r"(?i)charset\s*=\s*['\"]?\s*([a-z0-9._:-]+)", content_type
        )
        html_head = raw[:4096].decode("ascii", errors="ignore")
        meta_match = re.search(
            r"(?i)(?:charset\s*=\s*['\"]?\s*|charset\s*['\"]?\s+content\s*=\s*['\"][^'\"]*charset=)([a-z0-9._:-]+)",
            html_head,
        )
        candidates = []
        if raw.startswith(b"\xef\xbb\xbf"):
            candidates.append("utf-8-sig")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            candidates.append("utf-16")
        for candidate in (
            declared_match.group(1) if declared_match else "",
            meta_match.group(1) if meta_match else "",
            "utf-8",
            "gb18030",
            "shift_jis",
        ):
            normalized = candidate.strip().lower()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        decoded = []
        for encoding in candidates:
            try:
                value = raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
            decoded.append((self._text_quality_score(value), value, encoding))
            if encoding in (
                declared_match.group(1).lower() if declared_match else "",
                meta_match.group(1).lower() if meta_match else "",
            ):
                return value, encoding
        if decoded:
            _score, value, encoding = max(decoded, key=lambda item: item[0])
            return value, encoding
        return raw.decode("utf-8", errors="replace"), "utf-8-replacement"

    @staticmethod
    def _text_quality_score(value: str) -> float:
        if not value:
            return 0.0
        replacements = value.count("\ufffd")
        controls = sum(
            1 for char in value if ord(char) < 32 and char not in "\t\n\r\f"
        )
        c1_controls = sum(1 for char in value if 0x80 <= ord(char) <= 0x9F)
        mojibake = sum(value.count(marker) for marker in ("Ã", "Â", "â€", "ï¿½"))
        return 1.0 - (
            replacements * 8 + controls * 8 + c1_controls * 4 + mojibake * 2
        ) / len(value)

    @staticmethod
    def _looks_severely_garbled(value: str) -> bool:
        if not value:
            return False
        replacements = value.count("\ufffd")
        controls = sum(
            1
            for char in value
            if (ord(char) < 32 and char not in "\t\n\r\f")
            or 0x80 <= ord(char) <= 0x9F
        )
        return replacements / len(value) > 0.02 or controls / len(value) > 0.03

    @staticmethod
    def _extract_html_title(value: str) -> str:
        for pattern in (
            r"(?is)<meta[^>]+(?:property|name)=['\"](?:og:title|twitter:title)['\"][^>]+content=['\"]([^'\"]+)",
            r"(?is)<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name)=['\"](?:og:title|twitter:title)['\"]",
            r"(?is)<title[^>]*>(.*?)</title>",
        ):
            match = re.search(pattern, value)
            if match:
                title = html.unescape(re.sub(r"(?is)<[^>]+>", " ", match.group(1)))
                title = re.sub(r"\s+", " ", title).strip()
                if title:
                    return title[:500]
        return ""

    @staticmethod
    def _extract_published_at(value: str) -> str:
        patterns = (
            r"(?is)<meta[^>]+(?:property|name|itemprop)=['\"](?:article:published_time|datePublished|date|pubdate|publishdate|publish-date)['\"][^>]+content=['\"]([^'\"]+)",
            r"(?is)<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name|itemprop)=['\"](?:article:published_time|datePublished|date|pubdate|publishdate|publish-date)['\"]",
            r'(?is)["\']datePublished["\']\s*:\s*["\']([^"\']+)',
        )
        for pattern in patterns:
            match = re.search(pattern, value)
            if match:
                published_at = html.unescape(match.group(1)).strip()
                if published_at:
                    return published_at[:100]
        return ""

    @staticmethod
    def _fallback_web_title(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).rstrip("/")
        return (path.rsplit("/", 1)[-1] if path else parsed.netloc) or url

    async def message_push(self, channel: str, chat_id: str, message: str) -> str:
        if self.bus is None:
            return "Error: Message bus is not available"
        channel = channel.strip()
        chat_id = chat_id.strip()
        if not channel or not chat_id:
            return "Error: channel and chat_id are required"
        await self.bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=message)
        )
        return "已发送"

    def memorize(self, content: str, memory_type: str = "requested_memory") -> str:
        if self.memory is None:
            return "Error: Memory runtime is not enabled"
        source_ref = ""
        if self.registry is not None:
            ctx = self.registry.context
            session_key = str(ctx.get("session_key") or "")
            if session_key and self.session_manager is not None:
                source_ref = self.session_manager.peek_next_message_id(session_key)
        record = self.memory.memorize(
            content, source_ref=source_ref, memory_type=memory_type
        )
        return "记忆已写入: %s" % record.id

    def recall_memory(
        self,
        query: str,
        limit: int = 5,
        memory_types=None,
        since: str = "",
        until: str = "",
    ) -> str:
        if self.memory is None:
            return "[]"
        if isinstance(memory_types, str):
            memory_types = [memory_types]
        records = self.memory.recall(
            query,
            limit=limit,
            memory_types=[str(item) for item in (memory_types or [])],
            since=since,
            until=until,
        )
        return json.dumps(
            [r.to_public_json() for r in records], ensure_ascii=False, indent=2
        )

    def forget_memory(self, ids) -> str:
        if self.memory is None:
            return "Error: Memory runtime is not enabled"
        if isinstance(ids, str):
            ids = [ids]
        forgotten = self.memory.forget([str(item) for item in ids])
        return json.dumps({"superseded_ids": forgotten}, ensure_ascii=False)

    def search_messages(self, query: str, limit: int = 10) -> str:
        if self.session_manager is None:
            return "[]"
        return json.dumps(
            self.session_manager.search_messages(query, limit=limit),
            ensure_ascii=False,
            indent=2,
        )

    def fetch_messages(self, source_ref: str, context: int = 2) -> str:
        if self.session_manager is None:
            return "[]"
        return json.dumps(
            self.session_manager.fetch_messages(source_ref, context=context),
            ensure_ascii=False,
            indent=2,
        )

    def _html_to_text(self, value: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _validate_fetch_target(self, parsed: urllib.parse.ParseResult) -> str:
        if _env_bool(PRIVATE_FETCH_ENV):
            return ""
        host = parsed.hostname
        if not host:
            return "Error: URL host is required"
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            return "Error: Could not resolve host %s: %s" % (host, exc)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return (
                    "Error: Refusing to fetch private/local address %s. "
                    "Set %s=true only for trusted local tests."
                ) % (ip, PRIVATE_FETCH_ENV)
        return ""

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

    async def _drain_stream(self, stream) -> str:
        if stream is None:
            return ""
        chunks = bytearray()
        while True:
            data = await stream.read(8192)
            if not data:
                break
            if len(chunks) < OUTPUT_LIMIT:
                chunks.extend(data[: OUTPUT_LIMIT - len(chunks)])
        return chunks.decode("utf-8", errors="replace")

    def _register_shell_task(
        self,
        command: str,
        process: asyncio.subprocess.Process,
        timeout: int | None,
    ) -> _ShellTask:
        self._shell_dir.mkdir(parents=True, exist_ok=True)
        task_id = "shell_%s" % uuid4().hex[:12]
        log_path = self._shell_dir / (task_id + ".log")
        log_path.touch()
        pump = asyncio.create_task(
            self._pump_shell_log(process.stdout, log_path),
            name="shell-pump:%s" % task_id,
        )
        task = _ShellTask(
            task_id=task_id,
            command=command,
            process=process,
            log_path=log_path,
            pump_task=pump,
            timeout_task=None,
            started_at=time.monotonic(),
            started_at_ms=int(time.time() * 1000),
        )
        if timeout is not None:
            task.timeout_task = asyncio.create_task(
                self._enforce_shell_timeout(task, timeout),
                name="shell-timeout:%s" % task_id,
            )
        self._shell_tasks[task_id] = task
        return task

    @staticmethod
    async def _pump_shell_log(stream, path: Path) -> None:
        if stream is None:
            return
        with path.open("ab") as handle:
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                handle.write(chunk)
                handle.flush()

    async def _enforce_shell_timeout(self, task: _ShellTask, timeout: int) -> None:
        try:
            await asyncio.sleep(timeout)
            if task.process.returncode is None:
                self._kill_process_group(task.process)
                await task.process.wait()
        except asyncio.CancelledError:
            raise

    def _shell_task_payload(self, task: _ShellTask, *, auto_promoted: bool) -> str:
        return json.dumps(
            {
                "background_task_id": task.task_id,
                "status": "running",
                "auto_promoted": auto_promoted,
                "started_at_ms": task.started_at_ms,
                "message": "Use task_output to poll and task_stop to cancel.",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _read_shell_log(task: _ShellTask) -> str:
        try:
            return task.log_path.read_text(encoding="utf-8", errors="replace").strip() or "(no output)"
        except FileNotFoundError:
            return "(no output)"

    async def _stop_shell_task(self, task: _ShellTask, *, remove: bool) -> None:
        if task.process.returncode is None:
            self._kill_process_group(task.process)
            await task.process.wait()
        await asyncio.gather(task.pump_task, return_exceptions=True)
        if remove:
            self._remove_shell_task(task)

    def _remove_shell_task(self, task: _ShellTask) -> None:
        self._shell_tasks.pop(task.task_id, None)
        current = asyncio.current_task()
        if task.timeout_task is not None and task.timeout_task is not current:
            task.timeout_task.cancel()
        try:
            task.log_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

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


def build_default_registry(
    workdir: Path,
    skills_dir: Optional[Path] = None,
    memory: MemoryRuntime | None = None,
    session_manager: SessionManager | None = None,
    bus: MessageBus | None = None,
) -> ToolRegistry:
    skill_loader = SkillLoader(skills_dir or (workdir / "skills"))
    registry = ToolRegistry()
    handlers = WorkspaceTools(workdir, skill_loader, memory, session_manager, registry, bus)
    registry.add_shutdown_callback(handlers.shutdown)
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
                },
                ["command"],
            ),
        ),
        handlers.bash,
    )
    registry.register(
        ToolSpec(
            "task_output",
            "Poll output and status for a background shell task.",
            object_schema(
                {
                    "task_id": {"type": "string"},
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
            "task_stop",
            "Stop and clean up a background shell task.",
            object_schema({"task_id": {"type": "string"}}, ["task_id"]),
        ),
        handlers.task_stop,
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
    )
    registry.register(
        ToolSpec(
            "list_dir",
            "List files and directories inside the workspace.",
            object_schema({"path": {"type": "string"}}, []),
        ),
        handlers.list_dir,
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
            "compact",
            "Compress the current conversation context.",
            object_schema({}, []),
        ),
        handlers.compact,
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
            "web_fetch",
            "Fetch a verified web URL and return readable content with structured source metadata (URL, title, and publication date when available). Use it to verify important claims found by search; HTTP, binary, encoding, and decoding failures are returned as errors and are not evidence.",
            object_schema(
                {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
                ["url"],
            ),
        ),
        handlers.web_fetch,
    )
    registry.register(
        ToolSpec(
            "web_search",
            "Search the web and return structured result titles and URLs for source discovery. Empty/unparseable results are errors. For time-sensitive news, prices, or status claims, fetch reliable sources, note their dates, cross-check important facts, and disclose when evidence is insufficient.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                ["query"],
            ),
        ),
        handlers.web_search,
    )
    registry.register(
        ToolSpec(
            "message_push",
            "Send a message to a channel/chat through the MessageBus.",
            object_schema(
                {
                    "channel": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                ["channel", "chat_id", "message"],
            ),
        ),
        handlers.message_push,
    )
    registry.register(
        ToolSpec(
            "memorize",
            "Write a stable user fact or preference into long-term memory.",
            object_schema(
                {
                    "content": {"type": "string"},
                    "memory_type": {
                        "type": "string",
                        "enum": [
                            "requested_memory",
                            "identity",
                            "preference",
                            "procedure",
                            "event",
                        ],
                    },
                },
                ["content"],
            ),
        ),
        handlers.memorize,
    )
    registry.register(
        ToolSpec(
            "recall_memory",
            "Search long-term memory semantically/lexically.",
            object_schema(
                {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "memory_types": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                },
                ["query"],
            ),
        ),
        handlers.recall_memory,
    )
    registry.register(
        ToolSpec(
            "forget_memory",
            "Mark memory items as forgotten by id.",
            object_schema({"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]),
        ),
        handlers.forget_memory,
    )
    registry.register(
        ToolSpec(
            "search_messages",
            "Keyword search persisted chat messages and return source refs.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                ["query"],
            ),
        ),
        handlers.search_messages,
    )
    registry.register(
        ToolSpec(
            "fetch_messages",
            "Fetch persisted chat messages around a source_ref.",
            object_schema(
                {"source_ref": {"type": "string"}, "context": {"type": "integer"}},
                ["source_ref"],
            ),
        ),
        handlers.fetch_messages,
    )
    return registry
