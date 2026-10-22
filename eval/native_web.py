"""Real-provider workbench, project scope and directory acceptance."""

import argparse
import asyncio
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import httpx

from eval.models import EvaluationModel
from eval.native_runtime import text
from kirakira_agent.runtime import (
    initialize_workspace,
    runtime,
    submit,
)
from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.ui.contract import PLUGIN_UI


class ModuleOrder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.import_map = False
        self.module_count = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and attrs.get("type") == "importmap":
            self.import_map = True
        if (tag == "script" and attrs.get("type") == "module") or (
            tag == "link" and attrs.get("rel") == "modulepreload"
        ):
            assert self.import_map, (
                "module evaluation/preload appeared before the import map"
            )
            self.module_count += 1


async def evaluate(workspace, key_file, *, model_config=None):
    model_config = model_config or EvaluationModel.read(None)
    initialize_workspace(workspace, profile="web")
    async with runtime(workspace) as core:
        await model_config.configure(core, key_file)
        await model_config.configure(core, key_file)
        root = core.plugin_manager.live_root
        queries = root.context.require(PLUGIN_UI)
        catalog = await queries.catalog()
        project = next(row for row in catalog["items"] if row["id"] == "projects")
        project_id = "p_" + uuid4().hex
        directory = workspace / "project-directory"
        directory.mkdir()
        await queries.query(
            "projects",
            project["revision"],
            "project.create",
            {
                "project_id": project_id,
                "name": "Native project",
                "directory": str(directory),
            },
            session_id=None,
            turn_id=None,
        )
        nonce = uuid4().hex[:10]
        answer = await submit(
            core,
            "akashic:project",
            "project-input",
            "Use write_file to create project-proof.txt in the current working directory. "
            "Set its content to the exact JSON string value "
            + json.dumps("PROJECT_" + nonce)
            + ", with no extra whitespace, punctuation or trailing newline. Report after writing.",
            metadata={"session_dimensions": {"project": project_id}},
        )
        assert (directory / "project-proof.txt").read_text() == "PROJECT_" + nonce
        reader = root.context.require(MESSAGE_CATALOG).reader("akashic:project")
        assert reader.attributes.dimension("project") == project_id
        statuses = {}
        gate_headers = {
            "Authorization": "Bearer "
            + (workspace / "web-auth/secret").read_text().strip()
        }
        for name, path in (
            ("dashboard", "/dashboard/"),
            ("web-chat", "/assets/chat-app.js"),
        ):
            socket = (
                workspace
                / "runtime"
                / ("dashboard.sock" if name == "dashboard" else "web-chat.sock")
            )
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                base_url="http://native",
            ) as client:
                response = await client.get(
                    path, follow_redirects=True, headers=gate_headers
                )
                assert response.status_code == 200, (name, response.status_code)
                assert "/login" not in str(response.url)
                if name == "dashboard":
                    order = ModuleOrder()
                    order.feed(response.text)
                    assert order.import_map and order.module_count >= 2
                else:
                    bootstrap = await client.get(
                        "/api/chat/web-ui/bootstrap", headers=gate_headers
                    )
                    assert bootstrap.status_code == 200 and bootstrap.json()["modules"]
                statuses[name] = {
                    "status": response.status_code,
                    "bytes": len(response.content),
                    "authenticated": True,
                }

        return {
            "model_config": model_config.description(),
            "passed": True,
            "project_scope": project_id,
            "answer": text(answer),
            "project_file_verified": True,
            "http": statuses,
            "importmap_before_modules": True,
            "plugin_ui_count": len(catalog["items"]),
            "fibers": len(root.fibers()),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    args = parser.parse_args()
    os.environ.setdefault(
        "AKASHIC_PLUGIN_HOME",
        str(args.workspace.parent / (args.workspace.name + "-home")),
    )
    result = asyncio.run(evaluate(args.workspace.resolve(), args.key_file, model_config=EvaluationModel.read(args.model_config)))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
