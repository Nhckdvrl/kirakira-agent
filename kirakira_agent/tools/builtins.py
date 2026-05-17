"""Kirakira Agent learning harness module."""

import subprocess
from pathlib import Path
from typing import Optional

from kirakira_agent.schema import ToolSpec
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
    def __init__(self, workdir: Path, skill_loader: SkillLoader) -> None:
        self.workdir = workdir.resolve()
        self.skill_loader = skill_loader

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


def build_default_registry(workdir: Path, skills_dir: Optional[Path] = None) -> ToolRegistry:
    skill_loader = SkillLoader(skills_dir or (workdir / "skills"))
    handlers = WorkspaceTools(workdir, skill_loader)
    registry = ToolRegistry()
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
    return registry
