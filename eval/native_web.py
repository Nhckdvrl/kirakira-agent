"""Step5 through the native workbench, project scope and directory providers."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from eval.native_runtime import text
from kirakira_agent.runtime import (
    configure_step_plan,
    initialize_workspace,
    runtime,
    submit,
)
from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.ui.contract import PLUGIN_UI


async def evaluate(workspace, key_file):
    initialize_workspace(workspace, profile="web")
    async with runtime(workspace) as core:
        await configure_step_plan(core, key_file)
        await configure_step_plan(core, key_file)
        root = core.plugin_manager.live_root
        queries = root.context.require(PLUGIN_UI)
        catalog = await queries.catalog()
        project = next(row for row in catalog["items"] if row["id"] == "projects")
        project_id = "p_" + uuid4().hex
        directory = workspace / "project-directory"
        directory.mkdir()
        await queries.query("projects", project["revision"], "project.create",
            {"project_id": project_id, "name": "Native project", "directory": str(directory)},
            session_id=None, turn_id=None)
        nonce = uuid4().hex[:10]
        answer = await submit(core, "akashic:project", "project-input",
            "Use write_file to create project-proof.txt in the current working directory containing exactly PROJECT_" + nonce + ". Report after writing.",
            metadata={"session_dimensions": {"project": project_id}})
        assert (directory / "project-proof.txt").read_text() == "PROJECT_" + nonce
        reader = root.context.require(MESSAGE_CATALOG).reader("akashic:project")
        assert reader.attributes.dimension("project") == project_id
        statuses = {}
        for name, path in (("dashboard", "/dashboard"), ("web-chat", "/assets/chat-app.js")):
            socket = workspace / "runtime" / ("dashboard.sock" if name == "dashboard" else "web-chat.sock")
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)), base_url="http://native") as client:
                response = await client.get(path, follow_redirects=True)
                assert response.status_code == 200, (name, response.status_code)
                statuses[name] = {"status": response.status_code, "bytes": len(response.content)}
        return {"passed": True, "project_scope": project_id, "answer": text(answer),
                "project_file_verified": True, "http": statuses,
                "plugin_ui_count": len(catalog["items"]), "fibers": len(root.fibers())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", str(args.workspace.parent / (args.workspace.name + "-home")))
    result = asyncio.run(evaluate(args.workspace.resolve(), args.key_file))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
