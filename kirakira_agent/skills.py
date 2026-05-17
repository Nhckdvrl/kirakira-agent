"""Kirakira Agent learning harness module."""

import re
from pathlib import Path
from typing import Dict, List, Tuple


class SkillLoader:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._skills: Dict[str, Dict[str, str]] = {}
        self.reload()

    def reload(self) -> None:
        self._skills = {}
        if not self.skills_dir.exists():
            return
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            text = path.read_text()
            meta, body = self._parse(text)
            name = meta.get("name") or path.parent.name
            self._skills[name] = {
                "name": name,
                "description": meta.get("description", "-"),
                "body": body,
                "path": str(path),
            }

    def names(self) -> List[str]:
        return sorted(self._skills.keys())

    def descriptions(self) -> str:
        if not self._skills:
            return "(no skills)"
        return "\n".join(
            " - %s: %s" % (name, self._skills[name].get("description", "-"))
            for name in self.names()
        )

    def load(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(self.names()) or "(none)"
            return "Error: Unknown skill '%s'. Available: %s" % (name, available)
        return '<skill name="%s">\n%s\n</skill>' % (name, skill["body"])

    def _parse(self, text: str) -> Tuple[Dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text.strip()
        meta: Dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip('"')
        return meta, match.group(2).strip()


def list_skills(skills_dir: Path) -> str:
    return SkillLoader(skills_dir).descriptions()
