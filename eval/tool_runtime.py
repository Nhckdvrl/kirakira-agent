"""Tool I/O measurements and real-agent acceptance through the native plugin graph."""

import argparse
import asyncio
import gc
import hashlib
import json
import logging
import os
import shlex
import statistics
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path
from uuid import uuid4

import httpx

from core.net.http import HttpRequester, RequestBudget, RetryPolicy
from eval.models import EvaluationModel, provider_slot, source_identity
from eval.native_alignment import metrics
from kirakira_agent.runtime import ROOT, initialize_workspace, runtime, submit
from plugins.host_execution.bridge.filesystem import ReadFileOperation
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    CallRef,
    Output,
    ToolCall,
    ToolResult,
)
from plugins.standard_web.contract import FetchRequest
from plugins.tools.contract import ALL_TOOLS, TOOLS, durable_call_key
from plugins.web_fetch_http.fetch import FetchConfig, HttpFetch


def digest(data):
    return hashlib.sha256(data).hexdigest()


def previous_module(name, revision):
    data = subprocess.check_output(["git", "-C", str(ROOT), "show", revision + ":" + name])
    identity = "_tool_eval_before_" + digest(name.encode())[:12]
    module = types.ModuleType(identity)
    module.__package__ = name.removesuffix(".py").replace("/", ".").rsplit(".", 1)[0]
    module.__file__ = str(ROOT / name)
    sys.modules[identity] = module
    exec(compile(data, str(ROOT / name), "exec"), module.__dict__)  # noqa: S102 - Selected local Git source, only for the component comparison.
    return module, digest(data)


class Body(httpx.AsyncByteStream):
    def __init__(self):
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(64):
            self.consumed += 65536
            yield b"x" * 65536

    async def aclose(self):
        self.closed = True


async def components(folder, baseline, samples):
    path = folder / "large.txt"
    with path.open("w") as handle:
        for i in range(100_000):
            handle.write(f"{i:06d} " + "message data " * 5 + "\n")
    readers, fetchers, hashes = {"candidate": ReadFileOperation}, {"candidate": HttpFetch}, {}
    if baseline:
        for name, field, variants in (("plugins/host_execution/bridge/filesystem.py", "ReadFileOperation", readers),
                                      ("plugins/web_fetch_http/fetch.py", "HttpFetch", fetchers)):
            module, sha = previous_module(name, baseline)
            variants["baseline"] = getattr(module, field)
            hashes[name] = sha
    rounds = []
    for number in range(samples):
        order = list(readers) if number % 2 == 0 else list(reversed(readers))
        for side in order:
            gc.collect()
            tracemalloc.start()
            started = time.perf_counter()
            try:
                value = readers[side]().read_from_disk(str(path))
                elapsed = time.perf_counter() - started
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert isinstance(value, str)
            rounds.append({"pair": number, "side": side, "seconds": elapsed,
                           "peak_python_allocated_bytes": peak, "output_sha256": digest(value.encode()),
                           "output_bytes": len(value.encode())})
    assert len({r["output_sha256"] for r in rounds}) == 1
    # Boundary fixtures compare exact old/new presentation, not just output size.
    parity = []
    variants = {"empty": b"", "utf8": "中文\r\n第二行\n".encode(), "long_line": b"a" * 10_001,
                "invalid_tail": b"valid\n" * 500 + b"\xff\n", "bom": b"\xef\xbb\xbfhello\r\n"}
    for label, content in variants.items():
        fixture = folder / (label + ".txt")
        fixture.write_bytes(content)
        for offset, limit in ((0, None), (0, 2), (1, 1), (999, 5)):
            outputs = [reader().read_from_disk(str(fixture), offset=offset, limit=limit) for reader in readers.values()]
            assert all(value == outputs[0] for value in outputs), (label, offset, limit)
            parity.append({"fixture": label, "offset": offset, "limit": limit, "output_equal": True})
    web = []
    for side, fetcher in fetchers.items():
        for declared in (True, False):
            body = Body()
            headers = {"Content-Type": "text/plain", **({"Content-Length": str(4 * 1024 * 1024)} if declared else {})}
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request, body=body, headers=headers: httpx.Response(200, headers=headers, stream=body),
            )) as client:
                requester = HttpRequester(client, RetryPolicy(max_attempts=1), 5, RequestBudget(5))
                result = await fetcher(requester, FetchConfig(max_response_mb=1)).fetch(FetchRequest("http://fixture/large", "text", 5))
            assert "超过 1MB" in result["error"] and body.closed
            if side == "candidate":
                assert body.consumed <= (0 if declared else 1024 * 1024 + 65536)
            web.append({"side": side, "declared_length": declared, "body_bytes": 4 * 1024 * 1024,
                        "configured_max_bytes": 1024 * 1024, "consumed_bytes": body.consumed,
                        "stream_closed": body.closed, "result": result})
    return {"baseline_revision": baseline, "baseline_source_sha256": hashes,
            "scope": "same-process local file backend; tracemalloc Python allocations, not process RSS; controlled HTTP transport, not internet throughput",
            "file_bytes": path.stat().st_size, "file_rounds": rounds, "file_parity": parity,
            "file_summary": {side: {"median_seconds": statistics.median(r["seconds"] for r in rounds if r["side"] == side),
                                     "median_peak_python_allocated_bytes": statistics.median(r["peak_python_allocated_bytes"] for r in rounds if r["side"] == side)}
                             for side in readers}, "web": web}


class Timings(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        fields = getattr(record, "akashic_fields", {})
        if fields.get("event") == "runtime.timing" and fields.get("phase", "").startswith("tool."):
            self.rows.append(dict(fields))


async def evaluate(args):
    initialize_workspace(args.workspace)
    report = {"implementation": source_identity(), "cases": [], "component_only": args.component_only}
    for name in ("eval/tool_runtime.py", "eval/native_runtime.py", "core/net/http.py"):
        report["implementation"][name] = digest((ROOT / name).read_bytes())
    for area in ("tools", "tool_search", "files", "shell", "host_execution", "standard_web", "web_fetch_http", "mcp"):
        report["implementation"].update({str(p.relative_to(ROOT)): digest(p.read_bytes())
                                         for p in (ROOT / "plugins" / area).rglob("*.py")})
    folder = args.workspace / "tool-fixtures"
    folder.mkdir()

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def case(name, operation):
        started = time.perf_counter()
        try:
            row = {"name": name, "passed": True, "detail": await operation()}
        except Exception as error:  # noqa: BLE001 - retain failed cases, including model errors
            row = {"name": name, "passed": False, "error": type(error).__name__ + ": " + str(error)[:1600]}
        row["seconds"] = round(time.perf_counter() - started, 3)
        report["cases"].append(row)
        save()
        print(json.dumps({k: v for k, v in row.items() if k != "detail"}), flush=True)

    await case("bounded_io_component_comparison", lambda: components(folder, args.baseline_ref, args.samples))
    if args.component_only:
        return all(c["passed"] for c in report["cases"])
    model = EvaluationModel.read(args.model_config)
    report["model_config"] = model.description()
    nonce = uuid4().hex[:12]
    requests = []

    async def page(reader, writer):
        try:
            first = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode()
            requests.append(first)
            route = first.split(" ")[1]
            body = ("<html><body><script>noise</script><p>核对结果：WEB_" + nonce + "</p></body></html>").encode()
            if route == "/redirect":
                response = b"HTTP/1.1 302 Found\r\nLocation: /page\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            elif route == "/error":
                response = b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            else:
                response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(page, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    timings = Timings()
    logger = logging.getLogger("akashic.timing")
    logger.setLevel(logging.INFO)
    logger.addHandler(timings)
    try:
        async with provider_slot(model, args.key_file), runtime(args.workspace) as core:
            await model.configure(core, args.key_file)
            context = core.plugin_manager.live_root.context
            catalog = context.require(MESSAGE_CATALOG)
            report["tools"] = [{"name": ref.name, "parallel": context.require(TOOLS).allows_parallel(ref.name)}
                               for ref in context.require(ALL_TOOLS)().refs]

            def calls(session):
                reader = catalog.reader(session)
                bindings = reader.read_page(limit=100).bindings
                return [(message, index, part, bindings[part.binding_id]["metadata"]["tool"]["name"])
                        for message in reader.snapshot() if isinstance(message.body, Output)
                        for index, part in enumerate(message.body.parts) if isinstance(part, ToolCall)]

            async def ask(label, prompt):
                session = "akashic:tools-" + label
                answer = await submit(core, session, label, prompt)
                return session, "\n".join(str(p.value) for p in answer.body.parts if p.kind == "text")

            async def paginated_read():
                path = folder / "deep.txt"
                path.write_text("ordinary row\n" * 99_999 + "DEEP_" + nonce + "\n")
                session, answer = await ask("deep", f"使用 read_file 读取 {path}，offset=99998、limit=2；报告末行标记，不用其他工具也不要猜测。")
                assert "DEEP_" + nonce in answer
                assert any(name == "read_file" and p.arguments.get("offset") == 99998 and p.arguments.get("limit") == 2 for _, _, p, name in calls(session))
                return {"answer": answer, "file_lines": 100_000}
            await case("agent_reads_deep_file_page", paginated_read)

            async def edit():
                path = folder / "unicode.txt"
                path.write_bytes(b"\xef\xbb\xbf" + "项目\r\n预算=120\r\n尾行\r\n".encode())
                session, answer = await ask("edit", f"先用 read_file 读取 {path}，再用 edit_file 将预算=120改为预算=80，然后再次读取核对。不要完整覆盖文件。")
                assert path.read_bytes() == b"\xef\xbb\xbf" + "项目\r\n预算=80\r\n尾行\r\n".encode()
                assert [n for _, _, _, n in calls(session)].count("edit_file") == 1
                return {"bom_and_crlf_preserved": True, "answer": answer}
            await case("agent_edits_utf8_bom_crlf_without_rewriting", edit)

            async def listing():
                directory = folder / "entries"
                directory.mkdir()
                for i in range(8):
                    (directory / f"file-{i:03d}.txt").touch()
                session, answer = await ask("listing", f"用 list_dir 列出 {directory}，limit=3；随后按 after=file-002.txt 再读取 limit=3 的一页，报告两页的文件名。")
                assert all(f"file-{i:03d}.txt" in answer for i in range(6))
                assert any(name == "list_dir" and p.arguments.get("after") == "file-002.txt" for _, _, p, name in calls(session))
                return {"answer": answer, "pages_verified": 2}
            await case("agent_uses_directory_cursor", listing)

            async def parallel():
                paths = [folder / ("parallel-" + side + ".txt") for side in ("a", "b")]
                for i, path in enumerate(paths):
                    path.write_text(f"PAIR_{i}_{nonce}")
                session, answer = await ask("parallel", f"在同一次工具调用批次中并行用 read_file 读取 {paths[0]} 和 {paths[1]}，读完后报告两个标记。")
                assert all(f"PAIR_{i}_{nonce}" in answer for i in range(2))
                selected = [(m, i) for m, i, _, name in calls(session) if name == "read_file"]
                assert len(selected) == 2 and selected[0][0].message_id == selected[1][0].message_id
                results = [r.body.call_ref.part_index for r in catalog.reader(session).snapshot() if isinstance(r.body, ToolResult) and r.body.call_ref.message_id == selected[0][0].message_id]
                assert results == [selected[0][1], selected[1][1]]
                return {"answer": answer, "same_model_batch": True, "result_order_preserved": True}
            await case("agent_parallel_file_batch_preserves_result_order", parallel)

            async def web():
                session, answer = await ask("web", f"实际使用 web_fetch：先读取 {url}/error，遇到失败后再读取 {url}/redirect，报告最终页面的核对标记。不要编造失败页面的内容。")
                assert "WEB_" + nonce in answer
                rows = catalog.reader(session).snapshot()
                assert any(isinstance(r.body, ToolResult) and r.body.outcome == "error" for r in rows)
                assert any("/redirect " in r for r in requests) and any("/page " in r for r in requests)
                assert sum(n == "web_fetch" for _, _, _, n in calls(session)) >= 2
                return {"answer": answer, "error_observed": True, "redirect_followed": True}
            await case("agent_loads_web_tools_recovers_from_http_failure_and_follows_redirect", web)

            async def interactive():
                script = "print('READY', flush=True); value=input(); print('ACK:'+value, flush=True)"
                command = "python -u -c " + shlex.quote(script)
                session, answer = await ask("interactive", f"实际使用 shell 执行 {command!r}，shell=bash、login=false、tty=true、yield_time_ms=250；等出现READY，用 write_stdin 向同一个执行发送 {nonce!r} 加换行，报告实际ACK输出。")
                assert "ACK:" + nonce in answer
                names = [n for _, _, _, n in calls(session)]
                assert "shell" in names and "write_stdin" in names
                return {"answer": answer, "actual_pty_and_stdin": True}
            await case("agent_continues_interactive_shell", interactive)

            async def output_limit():
                command = "python -c " + shlex.quote("print('output-row\\n'*20000, end='')")
                session, answer = await ask("output", f"用 shell 执行 {command!r}，shell=bash、login=false、max_output_tokens=100、yield_time_ms=1000。报告输出是否被截断及完整输出文件路径；不要把完整输出再读进对话。")
                results = [json.loads(str(p.value)) for r in catalog.reader(session).snapshot() if isinstance(r.body, ToolResult)
                           for p in r.body.parts if p.kind == "text" and str(p.value).startswith('{"chunk_id"')]
                result = next(r for r in results if r.get("output_omitted_bytes", 0))
                full = Path(result["output_path"]).read_bytes()
                assert full == b"output-row\n" * 20000
                assert len(result["output"].encode()) < len(full)
                return {"answer": answer, "full_bytes": len(full), "presented_bytes": len(result["output"].encode()), "omitted_bytes": result["output_omitted_bytes"]}
            await case("agent_shell_keeps_full_output_after_bounded_presentation", output_limit)
            report["model_usage"] = metrics(args.workspace, model)
            measured = []
            for label in ("deep", "edit", "listing", "parallel", "web", "interactive", "output"):
                session = "akashic:tools-" + label
                for message, index, _, name in calls(session):
                    key = durable_call_key(CallRef(message.message_id, index))
                    phases = {r["phase"]: r["measurement_value"] for r in timings.rows if r.get("operation_id") == key}
                    durations = {}
                    for field, start, end in (("prepare_ms", "tool.prepare.begin", "tool.prepare.end"),
                                              ("invoke_ms", "tool.invoke.begin", "tool.invoke.end"),
                                              ("commit_ms", "tool.commit.begin", "tool.committed")):
                        durations[field] = (phases[end] - phases[start]) / 1e6 if start in phases and end in phases else None
                    measured.append({"name": name, "call_key": key, **durations})
            report["native_tool_timings"] = measured
            report["http_fixture_requests"] = requests
    finally:
        logger.removeHandler(timings)
        server.close()
        await server.wait_closed()
        save()
    return all(c["passed"] for c in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("key-file", "model-config"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--baseline-ref", help="Git revision for a source-only before/after component comparison")
    parser.add_argument("--samples", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--component-only", action="store_true")
    args = parser.parse_args()
    if not args.component_only and (args.key_file is None or args.model_config is None):
        parser.error("real-agent mode requires --key-file and --model-config")
    os.environ["AKASHIC_PLUGIN_HOME"] = str(args.workspace.parent / (args.workspace.name + "-home"))
    os.environ["AKASHIC_TIMING"] = "1"
    raise SystemExit(0 if asyncio.run(evaluate(args)) else 1)


if __name__ == "__main__":
    main()
