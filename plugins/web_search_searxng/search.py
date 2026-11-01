"""Search a configured SearXNG instance through its JSON API."""
import asyncio

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator

from core.net.http import http_error_detail
from plugins.standard_web.contract import SearchRequest

MAX_RESPONSE_BYTES = 1024 * 1024


class SearxngConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpUrl | None = None
    timeout_seconds: float = Field(default=25.0, ge=1, le=120, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username or value.password or value.query or value.fragment):
            raise ValueError("服务地址不能包含账号、查询参数或片段")
        return value


class SearchResult(BaseModel):
    model_config = ConfigDict(strict=True)

    title: str
    url: str
    content: str = ""


class SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    results: list[SearchResult]
    unresponsive_engines: list[list[str]] = Field(default_factory=list)


class SearxngSearch:
    def __init__(self, endpoint: HttpUrl, timeout_seconds: float):
        self._url = str(endpoint).rstrip("/") + "/search"
        self._timeout = timeout_seconds

    async def search(self, request: SearchRequest) -> dict[str, object]:
        """Keep partial engine failures visible and cap results at the requested count."""
        # 1. SearXNG accepts q and format; it has no result-count parameter.
        try:
            async with asyncio.timeout(self._timeout), httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("GET", self._url, params={"q": request.query, "format": "json"},
                                         headers={"accept": "application/json"}) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            return {"query": request.query, "error": "SearXNG 响应超过 1MB，请缩小搜索范围"}
                        content.extend(chunk)
        except (TimeoutError, httpx.RequestError, httpx.HTTPStatusError) as error:
            return {"query": request.query, "error": f"搜索失败：{http_error_detail(error)}"}

        # 2. HTML, malformed JSON, and missing results are failures, not empty searches.
        try:
            data = SearchResponse.model_validate_json(content)
        except ValidationError:
            return {"query": request.query, "error": "SearXNG 响应无效；请确认服务地址正确且已启用 JSON 格式"}
        result: dict[str, object] = {
            "query": request.query,
            "results": [item.model_dump() for item in data.results[:request.num_results]],
        }
        if data.unresponsive_engines:
            result["unresponsive_engines"] = data.unresponsive_engines
            if not data.results:
                result["error"] = "搜索引擎发生故障，未获得结果"
        return result
