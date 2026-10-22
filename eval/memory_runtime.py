"""Live channel -> memory -> tool/API -> restart acceptance with a real provider."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eval.models import EvaluationModel, source_identity
from eval.native_alignment import metrics
from eval.native_runtime import text
from kirakira_agent.client import AsyncClient, RemoteError
from kirakira_agent.runtime import ROOT, initialize_workspace, rpc, runtime, submit
from kirakira_agent.setup import configure_embedding, configure_proactive
from plugins.akasha.contract import (
    MEMORY_QUERIES,
    MemoryQuery,
    MemoryReadout,
    MemorySourceQuery,
    MEMORY_SOURCE_SEARCH,
    MemorySourceSearchQuery,
)
from plugins.akasha.infrastructure.consumption import load_message_nodes
from plugins.akasha.infrastructure.persistence import logical_state_sha256
from plugins.delivery.contract import DELIVERY_READ
from plugins.ledger.contract import MESSAGE_CATALOG, Output, ToolResult
from plugins.wake.request import read_request


def verify_source_search(path, output):
    """Recompute current windows offline; never execute code embedded in evidence."""
    from plugins.akasha.contract import MemoryEvidence, MemorySourceSearchReadout
    from plugins.akasha.source_search import search_originals

    if output.exists() or output.resolve() == path.resolve():
        raise ValueError("verification requires a new output file")
    payload = path.read_bytes()
    document = json.loads(payload)
    evidence = document.get("source_navigation", document)
    source = evidence["source"]
    request = MemorySourceSearchQuery.model_validate_json(json.dumps(evidence["request"]))
    original = MemoryEvidence.model_validate_json(json.dumps(source["evidence"]))
    current_source = (ROOT / "plugins/akasha/source_search.py").read_bytes()
    assert hashlib.sha256(current_source).hexdigest() == evidence["current_source_sha256"]
    result = search_originals(request, ((original, source["text"]),))
    assert result.model_dump(mode="json") == evidence["current"]
    summaries = {}
    for name in ("baseline", "current"):
        value = MemorySourceSearchReadout.model_validate_json(json.dumps(evidence[name]))
        assert len(value.model_dump_json()) <= request.max_chars
        for window in value.windows:
            span = window.evidence.excerpt
            assert span.message_id == original.message_id and span.source_chars == len(source["text"])
            assert source["text"][span.start:span.end] == window.text
        summaries[name] = {"windows": len(value.windows), "json_chars": len(value.model_dump_json()),
                           "expected_value_present": any(evidence["expected"] in window.text for window in value.windows)}
    assert hashlib.sha256(evidence["baseline_source"].encode()).hexdigest() == evidence["baseline_sha256"]
    assert path.read_bytes() == payload
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"complete": True, "model_calls": 0, "summaries": summaries,
                                 "baseline_execution": "frozen code hash and output spans checked; embedded code was not executed"}, ensure_ascii=False, indent=2) + "\n")


async def evaluate(args):
    initialize_workspace(args.workspace)
    if args.personal_only:
        from agent.plugin_composition.config_input import save_config
        save_config(args.workspace / "plugin-data/akasha-builtin", {"inject_max_chars": 2400})
    extension = args.workspace.parent / (args.workspace.name + "-fixtures")
    shutil.copytree(ROOT / "eval/fixtures/alignment", extension / "alignment",
                    ignore=shutil.ignore_patterns("__pycache__"))
    model = EvaluationModel.read(args.model_config)
    implementation = source_identity()
    implementation.update({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in ("eval/memory_runtime.py", "plugins/wake/request.py", "core/net/http.py")})
    report = {"model_config": model.description(), "implementation": implementation, "cases": []}
    report["tool_recall_max_chars"] = 2400 if args.personal_only else 12000

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def case(name, action):
        if args.personal_only and name not in {
            "personal_history_source_navigation_sdk_and_agent",
            "personal_event_uses_deep_history_and_confirms_one_delivery",
        }:
            return
        start = time.monotonic()
        try:
            detail = await action()
            row = {"name": name, "passed": True, "detail": detail}
        except Exception as error:  # noqa: BLE001 - record independent acceptance failures
            import traceback
            traceback.print_exc()
            row = {"name": name, "passed": False, "error": type(error).__name__ + ": " + str(error)[:1500]}
        row["duration_s"] = round(time.monotonic() - start, 3)
        report["cases"].append(row)
        save()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    async with runtime(args.workspace, extensions=(extension,)) as core:
        await model.configure(core, args.key_file)
        await configure_embedding(core, args.embedding_manifest)
        ctx = core.plugin_manager.live_root.context
        catalog = ctx.require(MESSAGE_CATALOG)
        training = [
            ("blueprint", "青禾研究项目暂定在周三发布，负责人是林悦。请只回复已记下。"),
            ("decoy", "栖云研究项目定在周三发布，负责人是周明。请只回复已记下。"),
            ("amendment", "更正青禾研究项目：发布改为周五，负责人仍是林悦。周三的计划已作废。请只回复已记下。"),
            ("constraint", "青禾研究项目的演示必须用匿名样本，不能展示客户原始数据。请只回复已记下。"),
        ]
        for name, prompt in training:
            await submit(core, "akashic:history-" + name, "history-" + name, prompt)
        path = args.workspace / "memory/akasha.db"

        async def learned():
            async with asyncio.timeout(180):
                while True:
                    try:
                        nodes = load_message_nodes(path)
                        if all("history-" + name in nodes for name, _ in training):
                            return {"learned_turns": len(set(nodes.values())), "source_messages": len(nodes)}
                    except (OSError, ValueError, sqlite3.Error):
                        pass
                    await asyncio.sleep(0.05)
        await case("live_channel_learning", learned)
        query_session = "akashic:memory-reader"
        await rpc(core, "evaluation/seed", {"session_id": query_session})
        request = MemoryQuery(key="project-evidence", session_id=query_session,
            query="青禾研究项目最新发布时间、负责人和演示数据约束", limit=10, max_chars=6000)
        before = logical_state_sha256(path)

        async def api():
            result = await ctx.require(MEMORY_QUERIES)(request)
            assert isinstance(result, MemoryReadout), "service returned a generation-local private type"
            report["readout"] = result.model_dump(mode="json")
            async with await AsyncClient.for_workspace(args.workspace) as client:
                remote = await client.memory_recall(request)
                # Each installed plugin owns an isolated module namespace. The
                # public boundary is serialized data, not Python class identity.
                assert result.model_dump(mode="json") == remote.model_dump(mode="json")
                await asyncio.gather(*(client.memory_recall(request) for _ in range(4)))
                try:
                    await client.memory_recall(request.model_copy(update={"query": "different request"}))
                except RemoteError:
                    pass
                else:
                    raise AssertionError("conflicting query key was accepted")
            assert len(result.material) <= request.max_chars
            assert set(result.record.presented_message_ids) == {ref.message_id for ref in result.evidence}
            assert result.evidence and "周五" in result.material and "匿名样本" in result.material
            assert logical_state_sha256(path) == before
            return {"presented_sources": len(result.evidence), "graph_version": result.record.graph_version,
                    "material_chars": len(result.material), "retry_identity": result.retrieval_ref, "graph_read_only": True}
        await case("service_socket_and_concurrent_idempotency", api)

        async def dated():
            stamp = catalog.reader("akashic:history-blueprint").get("history-blueprint").recorded_at
            window = request.model_copy(update={"key": "dated", "query": "青禾研究项目发布时间",
                                                "time_start": stamp, "time_end": stamp})
            async with await AsyncClient.for_workspace(args.workspace) as client:
                result = await client.memory_recall(window)
            assert result.record.hits and all(hit.session_id == "akashic:history-blueprint" for hit in result.record.hits)
            assert "周三" in result.material and "周五" not in result.material
            return {"time_start": stamp.isoformat(), "hit_sessions": sorted({h.session_id for h in result.record.hits})}
        await case("dated_source_filter_over_socket", dated)

        excerpt_request = request.model_copy(update={"key": "project-excerpts", "selection": "fused",
            "presentation": "excerpts", "limit": 4, "max_chars": 800})

        async def excerpt_sources():
            async with await AsyncClient.for_workspace(args.workspace) as client:
                result = await client.memory_recall(excerpt_request)
                repeated = await client.memory_recall(excerpt_request)
            assert result == repeated and len(result.material) <= 800
            assert result.evidence and all(e.excerpt is not None for e in result.evidence)
            for evidence in result.evidence:
                source = text(catalog.reader(evidence.session_id).get(evidence.message_id))
                span = evidence.excerpt
                assert source[span.start:span.end] in result.material and span.source_chars == len(source)
            report["excerpt_readout"] = result.model_dump(mode="json")
            return {"characters": len(result.material), "sources": len(result.evidence),
                    "all_offsets_match_original_text": True, "same_retry": True}
        await case("bounded_excerpts_preserve_original_ranges", excerpt_sources)

        source_request = None

        async def original_sources():
            nonlocal source_request
            source_request = MemorySourceQuery(session_id=query_session,
                retrieval_ref=report["readout"]["retrieval_ref"], message_id="history-constraint", length=2000)
            async with await AsyncClient.for_workspace(args.workspace) as client:
                source = await client.memory_source(source_request)
                assert source == await client.memory_source(source_request)
                assert "匿名样本" in source.text and source.next_offset is None
                try:
                    await client.memory_source(source_request.model_copy(update={"message_id": "unknown-message"}))
                except RemoteError:
                    pass
                else:
                    raise AssertionError("source read accepted a message outside its receipt")
            answer = await submit(core, query_session, "expand-source",
                "请实际调用 read_memory_source，retrieval_ref='" + source_request.retrieval_ref
                + "'，message_id='history-constraint'，offset=0，length=2000。只根据返回原文回答演示数据限制。")
            marks = [part for message in catalog.reader(query_session).snapshot()
                     if isinstance(message.body, ToolResult) and message.body.outcome == "success"
                     for part in message.body.parts if part.kind == "akasha.source"]
            assert marks and "匿名" in text(answer)
            assert logical_state_sha256(path) == before
            report["source_readout"] = source.model_dump(mode="json")
            return {"answer": text(answer), "source_chars": len(source.text),
                    "source_read_proof_in_ledger": True, "unknown_source_rejected": True,
                    "retrieval_and_learning_unchanged": True}
        await case("agent_expands_receipt_bound_source_without_retrieval", original_sources)

        async def end_to_end():
            answer = await submit(core, query_session, "query-update",
                "请实际调用 recall_memory 查询青禾研究项目，再回答目前哪天发布、谁负责、演示用什么数据。注意历史更正，不要与栖云项目混淆。")
            value = text(answer)
            assert "周五" in value and "林悦" in value and "匿名" in value, value
            assert logical_state_sha256(path) == before, "excluded query sessions changed learned graph"
            return {"answer": value, "query_session_learning": catalog.attributes(query_session).learning}
        await case("agent_tool_update_and_constraint_answer", end_to_end)

        async def event_uses_memory():
            target = "akashic:project-follow-up"
            # Keep reference decisions out of this target's recent conversation.
            # A live acknowledgment may itself recall and restate those decisions,
            # allowing the later Alert to answer without exercising its memory tool.
            await rpc(core, "evaluation/seed", {"session_id": target, "records": [
                {"kind": "input", "source": "conversation", "text":
                 "我只关注青禾研究项目的发布安排。之后有发布检查事件时，请结合我以前确认的项目记录提醒我。"},
                {"kind": "output", "source": "conversation", "text": "已记下这个关注条件。"},
            ]})
            await configure_proactive(core, args.workspace, target)
            now = datetime.now(UTC)
            event = {"event_id": "project-release-review", "observed_at": now.isoformat(),
                     "expires_at": (now + timedelta(minutes=10)).isoformat(),
                     "payload": {"title": "青禾研究项目发布检查", "content": "项目发布前的例行检查已到达，请提醒主人核对已确认的发布时间、负责人和演示数据约束。本事件未携带这些历史决定。"}}
            receipt = await rpc(core, "event-ingress/alert", event)
            async with asyncio.timeout(120):
                while True:
                    outputs = [m for m in catalog.reader(target).snapshot()
                               if m.source == "wake" and isinstance(m.body, Output) and m.body.finish == "complete"]
                    if outputs:
                        answer = outputs[-1]
                        status = ctx.require(DELIVERY_READ).status(answer.message_id, "akashic")
                        if status and (status.get("receipt") or {}).get("status") == "delivered":
                            break
                    await asyncio.sleep(0.05)
            value = text(answer)
            flows = [catalog.reader(session).snapshot()
                     for session in catalog.snapshot_heads(prefix="wake:")]
            matching = [rows for rows in flows
                        if read_request(rows).notification_id == answer.message_id]
            assert len(matching) == 1, "delivered alert has no unique archived Wake request"
            flow = read_request(matching[0])
            assert flow.alert_ref["mail_id"] == receipt["mail_id"]
            assert flow.context_session_id == target
            assert not any(token in flow.history for token in ("周五", "林悦", "匿名")), "reference decisions leaked into recent context"
            reads = [part.value for message in matching[0]
                     if isinstance(message.body, ToolResult) and message.body.outcome == "success"
                     for part in message.body.parts if part.kind == "akasha.recall"]
            assert reads, "alert did not actually retrieve historical evidence"
            assert "周五" in value and "林悦" in value and "匿名" in value, value
            duplicate = await rpc(core, "event-ingress/alert", event)
            assert duplicate["mail_id"] == receipt["mail_id"] and not duplicate["accepted"]
            assert logical_state_sha256(path) == before
            report["follow_up_message_id"] = answer.message_id
            return {"answer": value, "delivered": True, "duplicate_event_reused": True,
                    "event_contains_reference_answers": False, "query_and_delivery_sessions_excluded": True,
                    "recent_context_contains_reference_answers": False,
                    "successful_memory_reads": len(reads), "memory_context_session_id": target,
                    "wake_session_id": flow.session_id, "same_event_identity": True}
        await case("incoming_event_recovers_project_decisions_and_delivers", event_uses_memory)

        async def long_source_follow_up():
            nonlocal before
            history = ("竹影项目的结项报告很长。最终签字人写在报告结尾的署名页，本段没有给出姓名。\n"
                       + "Background context without final signatures. " * 450
                       + "\n署名页：顾予安。\n请只回复已记下，不转述原文，无需写档案文件。")
            await submit(core, "akashic:long-source", "history-long-source", history)
            async with asyncio.timeout(180):
                while "history-long-source" not in load_message_nodes(path):
                    await asyncio.sleep(0.05)
            before = logical_state_sha256(path)
            head = catalog.reader(query_session).head()
            answer = await submit(core, query_session, "find-report-signature",
                "先以 presentation='excerpts'、selection='fused' 调 recall_memory 检索竹影项目的结项报告，"
                "再根据原文告诉我最后签字人是谁。片段若没包含署名页，就沿引用用 read_memory_source 读取报告结尾。"
                "只用这两个记忆工具，不要猜测姓名。")
            rows = catalog.reader(query_session).snapshot(after_seq=head)
            proofs = [part.value for message in rows if isinstance(message.body, ToolResult)
                      and message.body.outcome == "success" for part in message.body.parts
                      if part.kind == "akasha.source" and part.value["span"]["message_id"] == "history-long-source"]
            assert "顾予安" in text(answer), text(answer)
            assert proofs and any(proof["span"]["start"] > 0 for proof in proofs)
            assert logical_state_sha256(path) == before
            return {"answer": text(answer), "original_chars": len(history), "source_reads": len(proofs),
                    "read_original_tail": True, "query_contains_reference_answer": False,
                    "models_received_history_as_evidence": True}
        await case("agent_follows_excerpt_to_long_original_and_recovers_signature", long_source_follow_up)

        async def source_navigation():
            nonlocal before
            history = ("海棠登山背包采购的完整讨论记录，过程条目不代表最终决定。\n"
                + "例行核对海棠背包预算与出行条件；此处只讨论检查步骤，没有最终采购决定。\n" * 240
                + "最终决定：海棠登山背包的预算已经从500元更正到760元，必须防水，只在周末出行使用。\n"
                + "后续核对清单不改变上面的最终决定。\n" * 120
                + "请只回复已记下，不转述原文，不写其他档案。")
            await submit(core, "akashic:personal-backpack", "history-personal-backpack", history)
            async with asyncio.timeout(180):
                while "history-personal-backpack" not in load_message_nodes(path):
                    await asyncio.sleep(0.05)
            before = logical_state_sha256(path)
            async with await AsyncClient.for_workspace(args.workspace) as client:
                recall = await client.memory_recall(MemoryQuery(session_id=query_session, key="personal-source-navigation",
                    query="海棠登山背包采购完整讨论", selection="fused", presentation="excerpts", limit=5, max_chars=2400))
                assert "history-personal-backpack" in recall.record.presented_message_ids
                search_request = MemorySourceSearchQuery(session_id=query_session, retrieval_ref=recall.retrieval_ref,
                    message_id="history-personal-backpack", query="最终决定", max_chars=2400)
                found = await client.memory_source_search(search_request)
                assert found == await ctx.require(MEMORY_SOURCE_SEARCH)(search_request)
                assert any("760" in window.text for window in found.windows)
                assert len(found.model_dump_json()) <= search_request.max_chars
                assert await client.memory_source_search(search_request) == found
            head = catalog.reader(query_session).head()
            answer = await submit(core, query_session, "locate-personal-backpack",
                "请实际使用search_memory_source，在retrieval_ref='" + recall.retrieval_ref + "'、message_id='history-personal-backpack'中"
                "检索海棠登山背包采购的最终决定，再告诉我当前预算和必要条件。原文没有目录位置提示，不要猜字符偏移。")
            proofs = [part.value for message in catalog.reader(query_session).snapshot(after_seq=head)
                if isinstance(message.body, ToolResult) and message.body.outcome == "success"
                for part in message.body.parts if part.kind == "akasha.source"
                and part.value["span"]["message_id"] == "history-personal-backpack"]
            value = text(answer)
            assert "760" in value and "防水" in value and "周末" in value, value
            assert proofs and any(proof["span"]["start"] > 2000 for proof in proofs)
            assert logical_state_sha256(path) == before
            report["navigation_request"] = search_request.model_dump(mode="json")
            report["navigation_readout"] = found.model_dump(mode="json")
            return {"answer": value, "sdk_service_repeat_equal": True, "original_chars": len(history),
                    "search_windows": len(found.windows), "tool_source_proofs": len(proofs),
                    "query_contains_reference_answers": False, "exact_offset_supplied": False}
        await case("personal_history_source_navigation_sdk_and_agent", source_navigation)

        async def personal_event():
            target = "akashic:personal-outing-inbox"
            await rpc(core, "evaluation/seed", {"session_id": target, "records": [
                {"kind": "input", "source": "conversation", "text": "这里接收海棠登山装备的消息，请结合先前的真实讨论判断。"},
                {"kind": "output", "source": "conversation", "text": "收到。"},
            ]})
            await configure_proactive(core, args.workspace, target)
            event = {"event_id": "personal-backpack-offer", "observed_at": datetime.now(UTC).isoformat(),
                "payload": {"title": "海棠登山背包新报价", "content":
                    "海棠背包有一条720元的防水款信息。请核对此前采购讨论的现行预算和使用安排后提醒主人。"
                    "完整讨论很长，请实际先用excerpts召回，再用search_memory_source在原文中检索最终决定；"
                    "读取失败先修正引用，核实原文成功后再分享。本事件不提供历史预算和出行安排。"}}
            receipt = await rpc(core, "event-ingress/alert", event)
            async with asyncio.timeout(180):
                while True:
                    outputs = [m for m in catalog.reader(target).snapshot() if m.source == "wake" and isinstance(m.body, Output) and m.body.finish == "complete"]
                    if outputs and ctx.require(DELIVERY_READ).confirmed(outputs[-1].message_id):
                        answer = outputs[-1]
                        break
                    await asyncio.sleep(0.05)
            flows = [catalog.reader(session).snapshot() for session in catalog.snapshot_heads(prefix="wake:")]
            rows = next(rows for rows in flows if read_request(rows).notification_id == answer.message_id)
            value = text(answer)
            assert "760" in value and "周末" in value and "防水" in value, value
            proofs = [part.value for message in rows if isinstance(message.body, ToolResult) and message.body.outcome == "success"
                for part in message.body.parts if part.kind == "akasha.source" and part.value["span"]["message_id"] == "history-personal-backpack"]
            assert proofs, "personal alert did not verify the admitted original source"
            duplicate = await rpc(core, "event-ingress/alert", event)
            assert duplicate["mail_id"] == receipt["mail_id"] and not duplicate["accepted"]
            assert logical_state_sha256(path) == before
            report["personal_notification"] = {"message_id": answer.message_id, "text": value}
            return {"delivered": True, "answer": value, "original_source_proofs": len(proofs),
                    "history_not_in_inbox_or_event": True, "duplicate_event_reused": True}
        await case("personal_event_uses_deep_history_and_confirms_one_delivery", personal_event)

        async def feedback(action):
            nonlocal before
            original = catalog.reader("akashic:history-decoy").get("history-decoy")
            node = load_message_nodes(path)["history-decoy"]
            session = "akashic:feedback-" + action
            identity = "feedback-" + action
            await submit(core, session, identity,
                f"请实际调用 {action}_memory，message_ids=['history-decoy']，reason='受控记忆反馈验证'。"
                "不要重述那条消息的内容，结束后只回复已完成。")
            async with asyncio.timeout(120):
                while identity not in load_message_nodes(path):
                    await asyncio.sleep(0.05)
            marks = [part for message in catalog.reader(session).snapshot()
                     if isinstance(message.body, ToolResult) and message.body.outcome == "success"
                     for part in message.body.parts if part.kind == "akasha.feedback"]
            assert any(part.value["action"] == action and "history-decoy" in part.value["target_message_ids"] for part in marks)
            async with await AsyncClient.for_workspace(args.workspace) as client:
                result = await client.memory_recall(request.model_copy(update={
                    "key": "after-" + action, "query": "栖云研究项目负责人和发布时间", "selection": "fused", "limit": 15}))
            assert (node in {hit.node_id for hit in result.record.hits}) == (action == "remember")
            assert catalog.reader(original.session_id).get(original.message_id) == original
            before = logical_state_sha256(path)
            return {"feedback_action": action, "target_node": node,
                    "target_retrieved": action == "remember", "original_message_unchanged": True,
                    "feedback_was_learned_from_successful_tool_result": True}
        await case("explicit_forgetting_changes_new_recall_preserves_raw_fact", lambda: feedback("forget"))
        await case("explicit_remembering_restores_addressed_experience", lambda: feedback("remember"))

    async with runtime(args.workspace, extensions=(extension,)) as core:
        async def restart():
            async with await AsyncClient.for_workspace(args.workspace) as client:
                result = await client.memory_recall(request)
                excerpts = await client.memory_recall(excerpt_request)
                source = await client.memory_source(source_request)
            assert result.model_dump(mode="json") == report["readout"]
            assert excerpts.model_dump(mode="json") == report["excerpt_readout"]
            assert source.model_dump(mode="json") == report["source_readout"]
            if "navigation_request" in report:
                async with await AsyncClient.for_workspace(args.workspace) as client:
                    located = await client.memory_source_search(MemorySourceSearchQuery.model_validate_json(json.dumps(report["navigation_request"])))
                assert located.model_dump(mode="json") == report["navigation_readout"]
            assert logical_state_sha256(path) == before
            if "follow_up_message_id" in report:
                catalog = core.plugin_manager.live_root.context.require(MESSAGE_CATALOG)
                assert catalog.reader("akashic:project-follow-up").get(report["follow_up_message_id"])
            return {"same_receipt_and_evidence": True, "same_graph_state": True}
        await case("restart_replays_evidence_without_relearning", restart)
    report["metrics"] = metrics(args.workspace, model)
    save()
    return all(row["passed"] for row in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--embedding-manifest", type=Path)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--personal-only", action="store_true", help="Run the two personal-history and incoming-event cases.")
    parser.add_argument("--verify-source-search", type=Path, help="Recompute frozen source-navigation evidence without a model or credentials.")
    args = parser.parse_args()
    if args.verify_source_search:
        verify_source_search(args.verify_source_search, args.output)
        return
    if not all((args.workspace, args.key_file, args.embedding_manifest)):
        parser.error("live evaluation requires --workspace, --key-file and --embedding-manifest")
    os.environ.setdefault("AKASHIC_PLUGIN_HOME", str(args.workspace.parent / (args.workspace.name + "-home")))
    raise SystemExit(0 if asyncio.run(evaluate(args)) else 1)


if __name__ == "__main__":
    main()
