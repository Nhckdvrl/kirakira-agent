"""Large inputs must respect output budgets before accumulating whole responses."""

import asyncio
import gzip
import json
import os
import sys
import tracemalloc
from pathlib import Path

import httpx
import pytest

from core.net.http import HttpRequester, RequestBudget, RetryPolicy
from plugins.host_execution.bridge.filesystem import ReadFileOperation
from plugins.standard_web.contract import FetchRequest, SearchRequest
from plugins.web_fetch_http.fetch import FetchConfig, HttpFetch
from plugins.web_search_searxng.search import SearxngConfig, SearxngSearch


@pytest.mark.parametrize("scenario", [
    "valid", "shell-as-search", "shell-as-fetch", "missing-search", "missing-fetch",
    "error-search", "error-fetch", "wrong-url", "marker-on-other", "search-missing-url",
])
def test_search_acceptance_requires_receipts_for_the_exact_native_tools(tmp_path, scenario):
    from eval.native_runtime import _search_evidence
    from plugins.ledger.contract import (
        CallRef,
        ContentPart,
        ContentReferences,
        Output,
        ToolCall,
        ToolResult,
    )
    from plugins.ledger.log import MessageLog

    page_url, marker = "https://example.org/announcement", "EVIDENCE_FIXTURE"
    log = MessageLog(tmp_path / "evidence.db")
    try:
        for name in ("web_search", "web_fetch", "shell"):
            log.save_binding(name, {"metadata": {"tool": {"name": name}}})
        output = log.writer("fixture", author="assistant", source="conversation", body_types=(Output,),
                            content={}, check_call=lambda _: None)
        caller = output.append("calls", Output((
            ToolCall("shell" if scenario == "shell-as-search" else "web_search", {"query": "fixture"}),
            ToolCall("shell" if scenario == "shell-as-fetch" else "web_fetch",
                     {"url": page_url + "/other" if scenario == "wrong-url" else page_url}),
            ToolCall("shell", {}),
        ), "continue"))
        for index, kind in enumerate(("search", "fetch", "other")):
            if scenario == "missing-" + kind:
                continue
            ref = CallRef(caller.message_id, index)
            content = ("no source URL" if scenario == "search-missing-url" else page_url) if kind == "search" else marker
            if kind == "fetch" and scenario == "marker-on-other":
                content = "page without its source marker"
            writer = log.writer("fixture", author="tools", source="conversation", body_types=(ToolResult,),
                                call_ref=ref, content={"text": lambda _: ContentReferences()})
            writer.append(kind, ToolResult(ref, "error" if scenario == "error-" + kind else "success",
                                          (ContentPart("text", content),)))
        if scenario == "valid":
            evidence = _search_evidence(log.reader("fixture"), page_url=page_url, marker=marker)
            assert evidence == {"successful_tool_receipts": 2, "search_tool_receipts": 1, "fetch_tool_receipts": 1}
        else:
            with pytest.raises(AssertionError):
                _search_evidence(log.reader("fixture"), page_url=page_url, marker=marker)
    finally:
        log.close()


@pytest.mark.parametrize("scenario", ["valid", "other-tool", "wrong-value", "missing", "error", "token-on-other"])
def test_exhaustion_acceptance_requires_the_original_native_probe_receipt(tmp_path, scenario):
    from eval.native_runtime import _probe_evidence
    from plugins.ledger.contract import (
        CallRef,
        ContentPart,
        ContentReferences,
        Output,
        ToolCall,
        ToolResult,
    )
    from plugins.ledger.log import MessageLog

    value = "LIMIT_FIXTURE"
    log = MessageLog(tmp_path / "probe.db")
    try:
        for name in ("native_probe", "shell"):
            log.save_binding(name, {"metadata": {"tool": {"name": name}}})
        output = log.writer("fixture", author="assistant", source="conversation", body_types=(Output,),
                            content={}, check_call=lambda _: None)
        caller = output.append("calls", Output((
            ToolCall("shell" if scenario == "other-tool" else "native_probe",
                     {"value": "DIFFERENT" if scenario == "wrong-value" else value}),
            ToolCall("shell", {}),
        ), "continue"))
        for index in range(2):
            if index == 0 and scenario == "missing":
                continue
            ref = CallRef(caller.message_id, index)
            writer = log.writer("fixture", author="tools", source="conversation", body_types=(ToolResult,),
                                call_ref=ref, content={"text": lambda _: ContentReferences()})
            writer.append("receipt-" + str(index), ToolResult(ref,
                "error" if index == 0 and scenario == "error" else "success", (ContentPart("text",
                "no original token" if index == 0 and scenario == "token-on-other" else value),)))
        if scenario == "valid":
            assert _probe_evidence(log.reader("fixture"), value=value) == 1
        else:
            with pytest.raises(AssertionError):
                _probe_evidence(log.reader("fixture"), value=value)
    finally:
        log.close()


def test_large_file_retains_bounded_output_and_preserves_deep_pagination(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"line\n" * 100_000)
    tracemalloc.start()
    try:
        text = ReadFileOperation().read_from_disk(str(path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert isinstance(text, str)
    assert "共 100000 行 / 500000 字节" in text
    assert "本次返回 400 行" in text and "行数超限" in text
    assert len(text.encode()) < 10_000
    assert peak < 500_000  # Generous bound; the old whole-file lists exceed 20 MB.
    tail = ReadFileOperation().read_from_disk(str(path), offset=99_997, limit=2)
    assert " 99998→line\n 99999→line\n" in tail
    assert "第 99998–99999 行 / 共 100000 行 / 500000 字节" in tail


class Body(httpx.AsyncByteStream):
    def __init__(self, *, chunks=64, data=None, slow=False, fail=False):
        self.chunks, self.data, self.slow, self.fail = chunks, data, slow, fail
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self.chunks):
            if self.slow:
                await asyncio.sleep(0.1)
            if self.fail and self.consumed:
                raise httpx.ReadError("fixture body interrupted")
            value = b"x" * 65536 if self.data is None else self.data
            self.consumed += len(value)
            yield value

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_web_fetch_rejects_large_bodies_during_read_and_closes_streams():
    for headers, stream, maximum in (
        ({"Content-Length": str(4 * 1024 * 1024)}, Body(), 0),
        ({}, Body(), 1024 * 1024 + 65536),
        ({"Content-Length": "100"}, Body(), 1024 * 1024 + 65536),
        ({"Content-Encoding": "gzip"}, Body(chunks=1, data=gzip.compress(b"x" * (2 * 1024 * 1024))), None),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request, stream=stream, headers=headers: httpx.Response(
                200, headers={"Content-Type": "text/plain", **headers}, stream=stream,
            ),
        )) as client:
            requester = HttpRequester(client, RetryPolicy(max_attempts=1), 5, RequestBudget(5))
            result = await HttpFetch(requester, FetchConfig(max_response_mb=1)).fetch(FetchRequest("http://fixture/large", "text", 5))
        assert "超过 1MB" in result["error"] and stream.closed
        if maximum is not None:
            assert stream.consumed <= maximum

    stream = Body(chunks=1, slow=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    )) as client:
        requester = HttpRequester(client, RetryPolicy(max_attempts=1), 5, RequestBudget(5))
        result = await HttpFetch(requester).fetch(FetchRequest("http://fixture/slow", "text", 0.01))
    assert "TimeoutError" in result["error"] and stream.closed


@pytest.mark.asyncio
async def test_http_stream_does_not_retry_after_handing_a_response_to_its_consumer():
    attempts = []
    streams = []

    def handle(request):
        attempts.append(request.url)
        if len(attempts) == 1:
            raise httpx.ConnectError("fixture connect failure")
        streams.append(Body(chunks=2, fail=True))
        return httpx.Response(200, stream=streams[-1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        requester = HttpRequester(client, RetryPolicy(max_attempts=3, base_delay_s=0), 5, RequestBudget(5))
        with pytest.raises(httpx.ReadError, match="fixture body interrupted"):
            async with requester.stream("GET", "http://fixture/failure") as response:
                await response.aread()
    assert len(attempts) == 2  # Connect retry is allowed; an exposed body is not replayed.
    assert streams[0].closed


@pytest.mark.asyncio
async def test_searxng_distinguishes_empty_partial_and_invalid_results_and_bounds_io(monkeypatch):
    from pydantic import ValidationError

    for endpoint in ("https://name:password@example.org/", "https://example.org/?token=secret",
                     "https://example.org/#fragment"):
        with pytest.raises(ValidationError):
            SearxngConfig(endpoint=endpoint)
    config = SearxngConfig(endpoint="https://example.org/nested/")
    client_type = httpx.AsyncClient
    replies = [
        {"results": [{"title": "one", "url": "https://example.org/1"},
                     {"title": "two", "url": "https://example.org/2"}],
         "unresponsive_engines": [["fixture", "timeout"]]},
        {"results": []},
        {"results": [], "unresponsive_engines": [["fixture", "timeout"]]},
        {"message": "missing required results"},
        b"<html>JSON format is disabled</html>",
    ]
    streams = []
    requests = []

    def handle(request):
        requests.append(request)
        if replies:
            body = replies.pop(0)
            return httpx.Response(200, content=body if isinstance(body, bytes) else json.dumps(body).encode())
        stream = Body()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handle), **kwargs))
    provider = SearxngSearch(config.endpoint, config.timeout_seconds)
    partial = await provider.search(SearchRequest("紧急邮件与票务", 1))
    assert len(partial["results"]) == 1 and "error" not in partial
    assert partial["unresponsive_engines"] == [["fixture", "timeout"]]
    assert await provider.search(SearchRequest("empty", 1)) == {"query": "empty", "results": []}
    failed_engines = await provider.search(SearchRequest("engines", 1))
    assert failed_engines["results"] == [] and "error" in failed_engines
    for query in ("missing", "html"):
        assert "响应无效" in (await provider.search(SearchRequest(query, 1)))["error"]
    assert "超过 1MB" in (await provider.search(SearchRequest("oversize", 1)))["error"]
    assert streams[0].closed and streams[0].consumed <= 1024 * 1024 + 65536
    assert requests[0].url.path == "/nested/search"
    assert dict(requests[0].url.params) == {"q": "紧急邮件与票务", "format": "json"}
    assert requests[0].headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_searxng_total_deadline_cancels_slow_stream_and_redacts_http_errors(monkeypatch):
    client_type = httpx.AsyncClient
    stream = Body(chunks=2, slow=True)

    def handle(request):
        if request.url.params["q"] == "reject":
            return httpx.Response(403, content=b"private response must never enter diagnostics")
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handle), **kwargs))
    provider = SearxngSearch(SearxngConfig(endpoint="https://example.org").endpoint, 0.01)
    assert "TimeoutError" in (await provider.search(SearchRequest("slow", 1)))["error"]
    assert stream.closed
    failed = await provider.search(SearchRequest("reject", 1))
    assert "403" in failed["error"] and "private response" not in failed["error"]
    assert "?q=" not in failed["error"]


@pytest.mark.asyncio
async def test_native_search_provider_configuration_reload_and_unavailable_selection(tmp_path):
    from agent.plugin_composition.config_input import save_config
    from kirakira_agent.runtime import initialize_workspace, runtime
    from plugins.standard_web.contract import WEB_PROVIDERS
    from plugins.web_search_searxng.plugin import SETTINGS

    requests = []

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request.decode("ascii").splitlines()[0])
            body = json.dumps({"results": [{"title": "fixture", "url": "https://example.org/verified",
                                             "content": "SOURCE_VERIFIED"}]}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    workspace = initialize_workspace(tmp_path / "workspace")
    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with runtime(workspace) as core:
            def providers():
                return core.plugin_manager.live_root.context.require(WEB_PROVIDERS)

            assert providers().choices("search") == [{"value": "web_search_exa", "label": "Exa"}]
            assert core.plugin_manager.live_root.context.require(SETTINGS) is not None
            save_config(workspace / "plugin-data/web_search_searxng-builtin",
                        {"endpoint": f"http://127.0.0.1:{port}/nested"})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"web_search_searxng"}))
            assert len(providers().choices("search")) == 2
            assert "多个" in (await providers().search(SearchRequest("ambiguous", 1)))["error"]
            assert not requests
            save_config(workspace / "plugin-data/standard_web-builtin", {"search_provider": "web_search_searxng"})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"standard_web"}))
            found = await providers().search(SearchRequest("source", 1))
            assert found["results"][0]["content"] == "SOURCE_VERIFIED"
            assert requests == ["GET /nested/search?q=source&format=json HTTP/1.1"]
            save_config(workspace / "plugin-data/web_search_searxng-builtin", {})
            await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"web_search_searxng"}))
            unavailable = await providers().search(SearchRequest("must not fall back", 1))
            assert "没有启用" in unavailable["error"] and len(requests) == 1


@pytest.mark.asyncio
async def test_computer_control_and_jsonrpc_keep_page_text_with_isolated_surrogates(tmp_path):
    from plugins.computer.control import endpoint_name

    calls = []
    normal = "中文与完整 emoji 🌟🚀"
    page = "标题 \ud83c 中段 \udfff 结尾 " + normal

    async def driver(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            path = headers.split(" ", 2)[1]
            length = next((int(row.split(":", 1)[1]) for row in headers.split("\r\n")
                           if row.lower().startswith("content-length:")), 0)
            body = json.loads(await reader.readexactly(length)) if length else {}
            calls.append((path, body))
            if path == "/driver/status":
                value = {"version": 3, "source": True, "ready": True}
            else:
                assert path == "/driver/run"
                content = ([{"type": "image", "data": "eA==", "mimeType": "不支持\udfff🚀"}]
                           if body["code"] == "bad-mime" else [
                               {"type": "text", "text": page, "metadata": {"notes": ["嵌套\ud800", normal]}},
                               {"type": "text", "text": normal},
                           ])
                value = {"content": content, "call_id": body["context"]["call_id"]}
            payload = json.dumps(value).encode("ascii")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(payload)).encode("ascii") + b"\r\nConnection: close\r\n\r\n" + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(driver, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    scope = "unicode-fixture"
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "plugins/computer/mcp_server.py"),
            env={**os.environ, "COMPUTER_URL": f"http://127.0.0.1:{port}",
                 "AKA_PLUGIN_DATA_DIR": str(tmp_path), "AKASHIC_MCP_SCOPE_ID": scope,
                 "PYTHONIOENCODING": "utf-8"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        async def rpc(payload):
            process.stdin.write(json.dumps(payload).encode("ascii") + b"\n")
            await process.stdin.drain()
            raw = await asyncio.wait_for(process.stdout.readline(), 5)
            return raw, json.loads(raw.decode("utf-8"))

        _, initialized = await rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert initialized["result"]["protocolVersion"] == "2025-11-25"

        async def control(code):
            reader, writer = await asyncio.open_unix_connection("\0" + endpoint_name(tmp_path, scope))
            try:
                writer.write(json.dumps({"op": "run", "code": code,
                    "context": {"session_id": "fixture", "call_id": code}}).encode("ascii") + b"\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), 5)
                expected = normal if code == "page" else "🚀"
                assert expected.encode("utf-8") in raw
                return json.loads(raw.decode("utf-8"))
            finally:
                writer.close()
                await writer.wait_closed()

        value = await control("page")
        assert value["call_id"] == "page" and "error" not in value
        assert value["content"][0]["text"] == "标题 � 中段 � 结尾 " + normal
        assert value["content"][0]["metadata"]["notes"] == ["嵌套�", normal]
        assert value["content"][1]["text"] == normal
        failed = await control("bad-mime")
        assert failed["kind"] == "control_error"
        assert failed["error"] == "Computer returned an unsupported screenshot type: 不支持�🚀"
        raw, invalid = await rpc({"jsonrpc": "2.0", "id": "请求🚀\ud83c", "method": "未知\udfff🌟"})
        assert "请求🚀�".encode("utf-8") in raw
        assert invalid["id"] == "请求🚀�" and invalid["error"]["code"] == -32601
        assert invalid["error"]["message"] == "method not found: 未知�🌟"
        _, listed = await rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert listed == {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}
        assert [path for path, _ in calls] == ["/driver/status", "/driver/run", "/driver/run"]
    finally:
        if process is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        server.close()
        await server.wait_closed()
    assert process.returncode == 0
    assert await process.stderr.read() == b""
