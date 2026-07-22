"""兼容 shim：akashic `core.net.http` 的最小替身。

memory2 的 embedder 在模块顶层 import 这些名字；只有真正启用 embedding 时才会用到
HttpRequester。这里用 httpx 提供一个够用的实现，未配置 embedding 时不会被调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RequestBudget:
    """请求预算占位：只携带总超时，供 embedder 传参。"""

    total_timeout_s: float = 40.0


class _HttpxResponse:
    def __init__(self, response: Any) -> None:
        self._response = response

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def json(self) -> Any:
        return self._response.json()

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def headers(self) -> Any:
        return self._response.headers

    @property
    def url(self) -> Any:
        return self._response.url

    @property
    def encoding(self) -> Any:
        return self._response.encoding


class HttpRequester:
    """基于 httpx 的最小异步请求器，满足 embedder 的 get/post 接口。"""

    def __init__(self, name: str = "external_default") -> None:
        self._name = name

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        timeout_s: float = 30.0,
        budget: RequestBudget | None = None,
        **_ignore: Any,
    ) -> _HttpxResponse:
        import httpx

        timeout = budget.total_timeout_s if budget else timeout_s
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=json)
        return _HttpxResponse(resp)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        timeout_s: float = 30.0,
        budget: RequestBudget | None = None,
        **_ignore: Any,
    ) -> _HttpxResponse:
        import httpx

        timeout = budget.total_timeout_s if budget else timeout_s
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects
        ) as client:
            resp = await client.get(url, headers=headers)
        return _HttpxResponse(resp)


class SharedHttpResources:
    """Reference-compatible HTTP resource owner."""

    def __init__(self) -> None:
        self.external_default = get_default_http_requester("external_default")

    async def aclose(self) -> None:
        return None


_DEFAULT_REQUESTER: HttpRequester | None = None


def get_default_http_requester(name: str = "external_default") -> HttpRequester:
    global _DEFAULT_REQUESTER
    if _DEFAULT_REQUESTER is None:
        _DEFAULT_REQUESTER = HttpRequester(name)
    return _DEFAULT_REQUESTER
