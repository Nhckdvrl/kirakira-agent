"""Kirakira Agent learning harness module."""

import ipaddress
import json
import html
import os
import re
import socket
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from kirakira_agent.bus import MessageBus
from kirakira_agent.events import OutboundMessage
from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.schema import ToolSpec
from kirakira_agent.session import SessionManager
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.registry import ToolRegistry, object_schema

OUTPUT_LIMIT = 50000
PRIVATE_FETCH_ENV = "KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH"


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

    def bash(self, command: str, timeout: int = 120) -> str:
        if self._dangerous_shell_command(command):
            return "Error: Dangerous command blocked"
        timeout = max(1, min(int(timeout), 300))
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + result.stderr).strip()
            return truncate(output or "(no output)")
        except subprocess.TimeoutExpired:
            return "Error: Timeout (%ss)" % timeout
        except OSError as exc:
            return "Error: %s" % exc

    def read_file(self, path: str, limit: Optional[int] = None) -> str:
        lines = safe_path(self.workdir, path).read_text(encoding="utf-8", errors="replace").splitlines()
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
        target.write_text(content, encoding="utf-8")
        return "Wrote %d bytes to %s" % (len(content), path)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        target = safe_path(self.workdir, path)
        content = target.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return "Error: Text not found in %s" % path
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return "Edited %s" % path

    def load_skill(self, name: str) -> str:
        return self.skill_loader.load(name)

    def compact(self) -> str:
        return "Compacting context."

    def tool_search(self, query: str = "", limit: int = 20) -> str:
        if self.registry is None:
            return "[]"
        terms = [term.lower() for term in re.findall(r"[\w:-]+", query)]
        matches = []
        for spec in self.registry.specs():
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
        return json.dumps(matches[: max(1, int(limit))], ensure_ascii=False, indent=2)

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
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("content-type", "")
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return "Error: HTTP %s while fetching %s" % (exc.code, url)
        except urllib.error.URLError as exc:
            return "Error: Fetch failed for %s: %s" % (url, exc.reason)
        text = raw.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or "<html" in text[:500].lower():
            text = self._html_to_text(text)
        return truncate(text.strip(), max(1000, int(max_chars)))

    def web_search(self, query: str, limit: int = 5) -> str:
        q = query.strip()
        if not q:
            return "[]"
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
            return json.dumps([{"error": str(exc)}], ensure_ascii=False)
        results = []
        for match in re.finditer(r"(?is)<a[^>]+class=['\"]result__a['\"][^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html_text):
            href = html.unescape(match.group(1))
            title = self._html_to_text(match.group(2)).strip()
            if href.startswith("//duckduckgo.com/l/?"):
                parsed = urllib.parse.urlparse("https:" + href)
                qs = urllib.parse.parse_qs(parsed.query)
                href = qs.get("uddg", [href])[0]
            if title and href:
                results.append({"title": title, "url": href})
            if len(results) >= max(1, int(limit)):
                break
        if not results:
            return json.dumps(
                [{"note": "No structured results parsed; use web_fetch for a known URL.", "query": q}],
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(results, ensure_ascii=False, indent=2)

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

    def memorize(self, content: str) -> str:
        if self.memory is None:
            return "Error: Memory runtime is not enabled"
        source_ref = ""
        if self.registry is not None:
            ctx = self.registry.context
            session_key = str(ctx.get("session_key") or "")
            if session_key and self.session_manager is not None:
                source_ref = self.session_manager.peek_next_message_id(session_key)
        record = self.memory.memorize(content, source_ref=source_ref)
        return "记忆已写入: %s" % record.id

    def recall_memory(self, query: str, limit: int = 5) -> str:
        if self.memory is None:
            return "[]"
        records = self.memory.recall(query, limit=limit)
        return json.dumps([r.to_json() for r in records], ensure_ascii=False, indent=2)

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
    registry.register(
        ToolSpec(
            "bash",
            "Run a shell command in the workspace.",
            object_schema(
                {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                ["command"],
            ),
        ),
        handlers.bash,
    )
    registry.register(
        ToolSpec(
            "read_file",
            "Read file contents from inside the workspace.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
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
            "Fetch a web page or URL and return readable text.",
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
            "Search the web and return result titles and URLs.",
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
            object_schema({"content": {"type": "string"}}, ["content"]),
        ),
        handlers.memorize,
    )
    registry.register(
        ToolSpec(
            "recall_memory",
            "Search long-term memory semantically/lexically.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
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
