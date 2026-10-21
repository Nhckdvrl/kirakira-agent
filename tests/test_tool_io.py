"""Large inputs must respect output budgets before accumulating whole responses."""

import asyncio
import gzip
import tracemalloc

import httpx
import pytest

from core.net.http import HttpRequester, RequestBudget, RetryPolicy
from plugins.host_execution.bridge.filesystem import ReadFileOperation
from plugins.standard_web.contract import FetchRequest
from plugins.web_fetch_http.fetch import FetchConfig, HttpFetch


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
