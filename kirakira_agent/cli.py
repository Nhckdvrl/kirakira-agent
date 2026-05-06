import os
from pathlib import Path
from typing import List

from kirakira_agent.agent import Agent, DEFAULT_SYSTEM
from kirakira_agent.config import load_dotenv, require_env
from kirakira_agent.models import OpenAICompatibleClient
from kirakira_agent.schema import JsonDict
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools import build_default_registry


def build_agent(workdir: Path) -> Agent:
    load_dotenv(workdir / ".env")
    model = require_env("MODEL_ID")
    client = OpenAICompatibleClient()
    registry = build_default_registry(workdir)
    skills = SkillLoader(workdir / "skills")
    system = (
        DEFAULT_SYSTEM
        + "\nCurrent workspace: %s\nAvailable skills:\n%s" % (workdir, skills.descriptions())
    )
    return Agent(client, registry, model=model, workdir=workdir, system=system)


def print_response_text(response_text: str) -> None:
    if response_text:
        print(response_text)


def repl(agent: Agent, workdir: Path) -> None:
    history: List[JsonDict] = []
    skill_loader = SkillLoader(workdir / "skills")
    print("kirakira-agent ready. /tools /skills /compact /exit")
    while True:
        try:
            query = input("kirakira >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query in ("/exit", "exit", "q", "quit"):
            break
        if query == "/tools":
            print("\n".join(agent.tool_registry.names()))
            continue
        if query == "/skills":
            skill_loader.reload()
            print(skill_loader.descriptions())
            continue
        if query == "/compact":
            if history:
                history[:] = agent.compact(history)
                print("Context compacted.")
            else:
                print("No context to compact.")
            continue

        history.append({"role": "user", "content": query})
        response = agent.run(history)
        print_response_text(response.text)


def main() -> None:
    workdir = Path(os.getcwd()).resolve()
    agent = build_agent(workdir)
    repl(agent, workdir)
