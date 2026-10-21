"""Native settings adoption, unchanged memory graph and real tool defaults."""

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import httpx

from eval.models import EvaluationModel, provider_slot, source_identity
from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime, submit
from kirakira_agent.setup import configure_embedding
from plugins.akasha.contract import AKASHA_RECORDS_VIEW
from plugins.akasha.infrastructure.consumption import load_message_nodes
from plugins.akasha.infrastructure.persistence import logical_state_sha256
from plugins.ledger.contract import MESSAGE_CATALOG, ToolResult


async def evaluate(args):
    initialize_workspace(args.workspace, profile="web")
    extensions = args.workspace.parent / (args.workspace.name + "-fixtures")
    shutil.copytree(
        ROOT / "eval/fixtures/alignment",
        extensions / "alignment",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    model = EvaluationModel.read(args.model_config)
    report = {
        "implementation": source_identity(),
        "model_config": model.description(),
        "cases": [],
    }
    expected = {}

    async def case(name, operation):
        started = time.monotonic()
        try:
            result = {"name": name, "passed": True, "detail": await operation()}
        except Exception as error:  # noqa: BLE001 - retain independent failures
            result = {
                "name": name,
                "passed": False,
                "error": type(error).__name__ + ": " + str(error)[:1200],
            }
        result["duration_s"] = round(time.monotonic() - started, 3)
        report["cases"].append(result)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps({k: v for k, v in result.items() if k != "detail"}), flush=True
        )

    async def connect(core):
        gate = {
            "Authorization": "Bearer "
            + (args.workspace / "web-auth/secret").read_text().strip()
        }
        dashboard = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                uds=str(args.workspace / "runtime/dashboard.sock")
            ),
            base_url="http://native",
            headers=gate,
        )
        chat = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                uds=str(args.workspace / "runtime/web-chat.sock")
            ),
            base_url="http://native",
            headers=gate,
        )

        async def request(plugin, method="GET", body=None):
            response = await chat.get("/api/chat/web-ui/bootstrap")
            response.raise_for_status()
            snapshot = response.json()
            module = next(
                (item for item in snapshot["modules"] if item["pluginId"] == plugin),
                None,
            )
            if module is None:
                raise LookupError("settings module is changing: " + plugin)
            headers = {
                "X-Akashic-Web-Snapshot": snapshot["snapshotId"],
                "X-Akashic-Web-Catalog": snapshot["catalogId"],
                "X-Akashic-Web-Module": plugin,
                "X-Akashic-Web-Generation": module["generationId"],
            }
            return await dashboard.request(
                method,
                "/api/dashboard/" + plugin + "/config",
                json=body,
                headers=headers,
            )

        async def read(plugin):
            response = await request(plugin)
            response.raise_for_status()
            return response.json()

        async def update(plugin, values):
            previous = await read(plugin)
            response = await request(
                plugin,
                "POST",
                {
                    "request_id": uuid4().hex,
                    "expected_input": previous["input_ref"],
                    "values": values,
                },
            )
            assert response.status_code in {200, 202}, (
                plugin,
                response.status_code,
                response.text[:500],
            )
            async with asyncio.timeout(60):
                while True:
                    try:
                        current = await read(plugin)
                        if current["input_ref"] != previous["input_ref"]:
                            break
                    except httpx.HTTPStatusError, LookupError:
                        pass
                    await asyncio.sleep(0.1)
            for key, value in values.items():
                if key == "enabled":
                    continue
                assert (
                    (key not in current["values"])
                    if value is None
                    else current["values"].get(key) == value
                ), current
            expected[plugin] = current["values"]
            return current

        return dashboard, chat, read, request, update

    async with runtime(args.workspace, extensions=(extensions,)) as core:
        await model.configure(core, args.key_file)
        await configure_embedding(core, args.embedding_manifest)
        await submit(
            core,
            "akashic:settings-history",
            "settings-original",
            "星桥项目需要先完成匿名数据检查，再做周五的演示。请只回复已记下，无需编辑档案文件。",
        )
        async with asyncio.timeout(90):
            while True:
                paths = list((args.workspace / "memory").rglob("akasha.db"))
                if paths and "settings-original" in load_message_nodes(paths[0]):
                    path = paths[0]
                    break
                await asyncio.sleep(0.1)
        graph = logical_state_sha256(path)
        dashboard, chat, read, request, update = await connect(core)
        try:

            async def memory_settings():
                current = await update(
                    "akasha",
                    {
                        "enabled": True,
                        "context_recall_limit": 1,
                        "inject_max_chars": 500,
                    },
                )
                assert logical_state_sha256(path) == graph
                return {"values": current["values"], "learned_graph_unchanged": True}

            await case("recall_parameters_adopt_without_relearning", memory_settings)

            async def invalid():
                before = await read("akasha")
                response = await request(
                    "akasha",
                    "POST",
                    {
                        "request_id": uuid4().hex,
                        "expected_input": before["input_ref"],
                        "values": {"enabled": True, "context_recall_limit": 0},
                    },
                )
                assert response.status_code == 422, response.status_code
                assert (await read("akasha"))["input_ref"] == before["input_ref"]
                assert logical_state_sha256(path) == graph
                return {
                    "status": response.status_code,
                    "configuration_and_graph_unchanged": True,
                }

            await case("invalid_recall_limit_is_rejected_before_adoption", invalid)

            for plugin, values in (
                ("markdown_memory", {"memory_budget": 2048, "consolidate": False}),
                ("reply", {"max_steps": 12}),
                ("subagent", {"max_active": 3, "max_steps": 8, "result_chars": 2048}),
                ("shell", {"timeout": 15, "max_output_tokens": 500}),
                ("jobs", {"completion_delivery": "quiet", "max_consecutive_wakes": 2}),
                ("scheduler", {"max_active_jobs": 4, "max_steps": 8}),
                ("compaction", {"keep_recent_tokens": 2048}),
                ("session_title", {"max_title_chars": 40}),
                ("standard_web", {"default_results": 3, "default_fetch_timeout": 5}),
                (
                    "web_fetch_http",
                    {
                        "max_text_chars": 1000,
                        "user_agent": "kirakira-settings-acceptance/1.0",
                    },
                ),
            ):

                async def adopt(plugin=plugin, values=values):
                    value = await update(plugin, values)
                    return {
                        "values": value["values"],
                        "native_generation_adopted": True,
                    }

                await case(plugin + "_settings_adopt", adopt)

            async def actual_recall():
                session = "akashic:settings-query"
                await rpc(core, "evaluation/seed", {"session_id": session})
                await submit(
                    core,
                    session,
                    "settings-query",
                    "请实际调用 recall_memory 查询星桥项目，再依据返回材料回答演示前要检查什么。不要指定 limit。",
                )
                catalog = core.plugin_manager.live_root.context.require(MESSAGE_CATALOG)
                proofs = [
                    part.value
                    for row in catalog.reader(session).snapshot()
                    if isinstance(row.body, ToolResult)
                    and row.body.outcome == "success"
                    for part in row.body.parts
                    if part.kind == "akasha.recall"
                ]
                assert proofs
                records = core.plugin_manager.live_root.context.require(
                    AKASHA_RECORDS_VIEW
                )()
                record = records.read(proofs[-1]["retrieval_ref"])
                assert record.max_chars == 500, record.max_chars
                assert logical_state_sha256(path) == graph
                return {
                    "actual_tool_budget": record.max_chars,
                    "raw_history_preserved": True,
                    "learned_graph_unchanged": True,
                }

            await case("actual_model_recall_uses_adopted_budget", actual_recall)

            async def reset_default():
                value = await update("shell", {"timeout": None})
                assert (
                    "timeout" not in value["values"]
                    and value["values"]["max_output_tokens"] == 500
                )
                return {
                    "default_timeout": value["defaults"]["timeout"],
                    "other_explicit_override_preserved": True,
                }

            await case("clearing_one_field_restores_its_default", reset_default)
        finally:
            await dashboard.aclose()
            await chat.aclose()

    async with runtime(args.workspace, extensions=(extensions,)) as core:
        dashboard, chat, read, _, _ = await connect(core)
        try:

            async def restart():
                for plugin, values in expected.items():
                    assert (await read(plugin))["values"] == values, plugin
                assert logical_state_sha256(path) == graph
                return {
                    "persisted_configurations": len(expected),
                    "learned_graph_unchanged": True,
                }

            await case("restart_preserves_all_adopted_settings_and_memory", restart)
        finally:
            await dashboard.aclose()
            await chat.aclose()
    return all(row["passed"] for row in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "workspace",
        "key-file",
        "model-config",
        "embedding-manifest",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["AKASHIC_PLUGIN_HOME"] = str(
        args.workspace.parent / (args.workspace.name + "-home")
    )

    async def run():
        async with provider_slot(
            EvaluationModel.read(args.model_config), args.key_file
        ):
            return await evaluate(args)

    raise SystemExit(0 if asyncio.run(run()) else 1)


if __name__ == "__main__":
    main()
