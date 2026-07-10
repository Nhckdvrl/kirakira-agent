"""Kirakira Agent learning harness module."""

import subprocess
import json
from pathlib import Path
from typing import Optional

from kirakira_agent.memory import MemoryRuntime
from kirakira_agent.schema import ToolSpec
from kirakira_agent.session import SessionManager
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.registry import ToolRegistry, object_schema

OUTPUT_LIMIT = 50000


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
    ) -> None:
        self.workdir = workdir.resolve()
        self.skill_loader = skill_loader
        self.memory = memory
        self.session_manager = session_manager
        self.registry = registry

    def bash(self, command: str, timeout: int = 120) -> str:
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(item in command for item in dangerous):
            return "Error: Dangerous command blocked"
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
        lines = safe_path(self.workdir, path).read_text().splitlines()
        if limit is not None and limit >= 0 and limit < len(lines):
            lines = lines[:limit] + ["... (%d more lines)" % (len(lines) - limit)]
        return truncate("\n".join(lines))

    def write_file(self, path: str, content: str) -> str:
        target = safe_path(self.workdir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return "Wrote %d bytes to %s" % (len(content), path)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        target = safe_path(self.workdir, path)
        content = target.read_text()
        if old_text not in content:
            return "Error: Text not found in %s" % path
        target.write_text(content.replace(old_text, new_text, 1))
        return "Edited %s" % path

    def load_skill(self, name: str) -> str:
        return self.skill_loader.load(name)

    def compact(self) -> str:
        return "Compacting context."

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


def build_default_registry(
    workdir: Path,
    skills_dir: Optional[Path] = None,
    memory: MemoryRuntime | None = None,
    session_manager: SessionManager | None = None,
) -> ToolRegistry:
    skill_loader = SkillLoader(skills_dir or (workdir / "skills"))
    registry = ToolRegistry()
    handlers = WorkspaceTools(workdir, skill_loader, memory, session_manager, registry)
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
