"""Web retrieval: URL validation, decoding and evidence formatting."""

import gzip
import html
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from core.schema import ToolSpec
from plugins.tools.registry import ToolRegistry, object_schema
from plugins.tools.text import truncate

PRIVATE_FETCH_ENV = "KIRAKIRA_ALLOW_PRIVATE_WEB_FETCH"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "enabled")


def _html_to_markdown(raw_html: str) -> str:
    """HTML → Markdown（参考 akashic-agent web_fetch，html2text 实现）。"""
    import html2text

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0  # 禁止自动折行
    converter.unicode_snob = True  # 保留 Unicode 字符
    converter.protect_links = True  # 防止链接被转义
    return converter.handle(raw_html).strip()


def _html_to_plain_text(raw_html: str) -> str:
    """HTML → 纯文本（参考 akashic-agent web_fetch，lxml 抽正文并合并空白）。"""
    from lxml import html as lxml_html
    from lxml.etree import ParserError

    try:
        doc = lxml_html.fromstring(raw_html)
    except ParserError, ValueError:
        return raw_html
    for tag in ("script", "style", "noscript", "iframe", "object", "embed"):
        for element in doc.xpath("//%s" % tag):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    return " ".join(doc.text_content().split())


class WebTools:
    _WEB_SEARCH_MCP_URL = "https://mcp.exa.ai/mcp"

    def web_fetch(
        self, url: str, max_chars: int = 12000, format: str = "markdown"
    ) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "Error: URL must start with http:// or https://"
        validation_error = self._validate_fetch_target(parsed)
        if validation_error:
            return validation_error
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "kirakira-agent/0.1 (+https://github.com/Nhckdvrl/kirakira-agent)",
                "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            method="GET",
        )
        owner = self

        class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, redirect_req, fp, code, msg, headers, newurl):
                redirect = urllib.parse.urlparse(newurl)
                if redirect.scheme not in ("http", "https") or not redirect.netloc:
                    raise urllib.error.URLError("unsafe redirect URL")
                error = owner._validate_fetch_target(redirect)
                if error:
                    raise urllib.error.URLError(error)
                return super().redirect_request(
                    redirect_req, fp, code, msg, headers, newurl
                )

        opener = urllib.request.build_opener(SafeRedirectHandler())
        try:
            with opener.open(req, timeout=30) as resp:
                content_type = resp.headers.get("content-type", "")
                content_encoding = resp.headers.get("content-encoding", "")
                final_url = resp.geturl()
                declared = int(resp.headers.get("content-length") or "0")
                if declared > 5 * 1024 * 1024:
                    return "Error: Response exceeds 5 MB"
                raw = resp.read(5 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            return "Error: HTTP %s while fetching %s" % (exc.code, url)
        except urllib.error.URLError as exc:
            return "Error: Fetch failed for %s: %s" % (url, exc.reason)
        if len(raw) > 5 * 1024 * 1024:
            return "Error: Response exceeds 5 MB"
        try:
            raw = self._decompress_web_body(raw, content_encoding)
        except (OSError, EOFError, zlib.error, ValueError) as exc:
            return "Error: Could not decode %s response from %s: %s" % (
                content_encoding or "compressed",
                url,
                exc,
            )
        if len(raw) > 5 * 1024 * 1024:
            return "Error: Decompressed response exceeds 5 MB"
        lowered_type = content_type.lower()
        if lowered_type and not any(
            item in lowered_type
            for item in ("text/", "json", "xml", "javascript", "x-www-form-urlencoded")
        ):
            return "Error: Unsupported response content type: %s" % content_type
        if self._looks_like_binary(raw):
            return "Error: Response appears to be binary data"
        text, charset = self._decode_web_body(raw, content_type)
        if self._looks_severely_garbled(text):
            return "Error: Response text is severely garbled or uses an unsupported encoding"
        title = self._extract_html_title(text)
        published_at = self._extract_published_at(text)
        # 参考 akashic-agent：format 决定输出形态。markdown（默认）用 html2text，
        # text 用 lxml 抽正文，html 原样返回解码后的 HTML。
        fmt = (format or "markdown").strip().lower()
        if fmt not in ("text", "markdown", "html"):
            fmt = "markdown"
        is_html = "html" in content_type.lower() or "<html" in text[:500].lower()
        if is_html and fmt == "markdown":
            text = _html_to_markdown(text)
        elif is_html and fmt == "text":
            text = _html_to_plain_text(text)
        # fmt == "html" 或非 HTML 内容：保留原始解码文本。
        source = {
            "url": final_url,
            "title": title or self._fallback_web_title(final_url),
            "format": fmt,
        }
        if published_at:
            source["published_at"] = published_at
        if content_type:
            source["content_type"] = content_type
        if charset:
            source["charset"] = charset
        return json.dumps(
            {
                "source": source,
                "content": truncate(text.strip(), max(1000, int(max_chars))),
            },
            ensure_ascii=False,
            indent=2,
        )

    def web_search(self, query: str, limit: int = 5) -> str:
        # 参考 akashic-agent：走 Exa 公开 MCP 端点（无需 API key），返回带标题/URL/摘要
        # 的结构化文本。旧的 DuckDuckGo HTML 抓取已被搜索引擎反爬全面封锁，永久失效。
        import httpx

        q = query.strip()
        if not q:
            return "Error: Web search query must not be empty"
        num_results = min(max(1, int(limit)), 20)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": q,
                    "numResults": num_results,
                    "livecrawl": "fallback",
                    "type": "auto",
                },
            },
        }
        try:
            with httpx.Client(timeout=25.0) as client:
                response = client.post(
                    self._WEB_SEARCH_MCP_URL,
                    json=payload,
                    headers={
                        "accept": "application/json, text/event-stream",
                        "content-type": "application/json",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return "Error: Web search failed for %r: %s" % (q, exc)

        # 端点以 SSE 帧返回；逐行取第一帧带 content 的 result。
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            content = (data.get("result") or {}).get("content") or []
            if content:
                text = content[0].get("text", "")
                if text.strip():
                    return json.dumps({"query": q, "result": text}, ensure_ascii=False)
        return (
            "Error: Web search returned no results for %r. "
            "Try a different query or use web_fetch with a verified URL."
        ) % q

    @staticmethod
    def _decompress_web_body(raw: bytes, content_encoding: str) -> bytes:
        encodings = [
            value.strip().lower()
            for value in content_encoding.split(",")
            if value.strip() and value.strip().lower() != "identity"
        ]
        for encoding in reversed(encodings):
            if encoding in ("gzip", "x-gzip"):
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            else:
                raise ValueError("unsupported content encoding %r" % encoding)
        return raw

    @staticmethod
    def _looks_like_binary(raw: bytes) -> bool:
        if not raw:
            return False
        sample = raw[:8192]
        if b"\x00" in sample:
            return True
        binary_controls = sum(
            1 for value in sample if value < 32 and value not in (9, 10, 12, 13)
        )
        return binary_controls / len(sample) > 0.08

    def _decode_web_body(self, raw: bytes, content_type: str) -> tuple[str, str]:
        if not raw:
            return "", "utf-8"
        declared_match = re.search(
            r"(?i)charset\s*=\s*['\"]?\s*([a-z0-9._:-]+)", content_type
        )
        html_head = raw[:4096].decode("ascii", errors="ignore")
        meta_match = re.search(
            r"(?i)(?:charset\s*=\s*['\"]?\s*|charset\s*['\"]?\s+content\s*=\s*['\"][^'\"]*charset=)([a-z0-9._:-]+)",
            html_head,
        )
        candidates = []
        if raw.startswith(b"\xef\xbb\xbf"):
            candidates.append("utf-8-sig")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            candidates.append("utf-16")
        for candidate in (
            declared_match.group(1) if declared_match else "",
            meta_match.group(1) if meta_match else "",
            "utf-8",
            "gb18030",
            "shift_jis",
        ):
            normalized = candidate.strip().lower()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        decoded = []
        for encoding in candidates:
            try:
                value = raw.decode(encoding)
            except LookupError, UnicodeDecodeError:
                continue
            decoded.append((self._text_quality_score(value), value, encoding))
            if encoding in (
                declared_match.group(1).lower() if declared_match else "",
                meta_match.group(1).lower() if meta_match else "",
            ):
                return value, encoding
        if decoded:
            _score, value, encoding = max(decoded, key=lambda item: item[0])
            return value, encoding
        return raw.decode("utf-8", errors="replace"), "utf-8-replacement"

    @staticmethod
    def _text_quality_score(value: str) -> float:
        if not value:
            return 0.0
        replacements = value.count("\ufffd")
        controls = sum(1 for char in value if ord(char) < 32 and char not in "\t\n\r\f")
        c1_controls = sum(1 for char in value if 0x80 <= ord(char) <= 0x9F)
        mojibake = sum(value.count(marker) for marker in ("Ã", "Â", "â€", "ï¿½"))
        return 1.0 - (
            replacements * 8 + controls * 8 + c1_controls * 4 + mojibake * 2
        ) / len(value)

    @staticmethod
    def _looks_severely_garbled(value: str) -> bool:
        if not value:
            return False
        replacements = value.count("\ufffd")
        controls = sum(
            1
            for char in value
            if (ord(char) < 32 and char not in "\t\n\r\f") or 0x80 <= ord(char) <= 0x9F
        )
        return replacements / len(value) > 0.02 or controls / len(value) > 0.03

    @staticmethod
    def _extract_html_title(value: str) -> str:
        for pattern in (
            r"(?is)<meta[^>]+(?:property|name)=['\"](?:og:title|twitter:title)['\"][^>]+content=['\"]([^'\"]+)",
            r"(?is)<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name)=['\"](?:og:title|twitter:title)['\"]",
            r"(?is)<title[^>]*>(.*?)</title>",
        ):
            match = re.search(pattern, value)
            if match:
                title = html.unescape(re.sub(r"(?is)<[^>]+>", " ", match.group(1)))
                title = re.sub(r"\s+", " ", title).strip()
                if title:
                    return title[:500]
        return ""

    @staticmethod
    def _extract_published_at(value: str) -> str:
        patterns = (
            r"(?is)<meta[^>]+(?:property|name|itemprop)=['\"](?:article:published_time|datePublished|date|pubdate|publishdate|publish-date)['\"][^>]+content=['\"]([^'\"]+)",
            r"(?is)<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name|itemprop)=['\"](?:article:published_time|datePublished|date|pubdate|publishdate|publish-date)['\"]",
            r'(?is)["\']datePublished["\']\s*:\s*["\']([^"\']+)',
        )
        for pattern in patterns:
            match = re.search(pattern, value)
            if match:
                published_at = html.unescape(match.group(1)).strip()
                if published_at:
                    return published_at[:100]
        return ""

    @staticmethod
    def _fallback_web_title(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).rstrip("/")
        return (path.rsplit("/", 1)[-1] if path else parsed.netloc) or url

    def _html_to_text(self, value: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _validate_fetch_target(self, parsed: urllib.parse.ParseResult) -> str:
        if _env_bool(PRIVATE_FETCH_ENV):
            return ""
        host = parsed.hostname
        if not host:
            return "Error: URL host is required"
        try:
            addresses = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            return "Error: Could not resolve host %s: %s" % (host, exc)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return (
                    "Error: Refusing to fetch private/local address %s. "
                    "Set %s=true only for trusted local tests."
                ) % (ip, PRIVATE_FETCH_ENV)
        return ""


def register_web_tools(registry: ToolRegistry) -> None:
    handlers = WebTools()
    registry.register(
        ToolSpec(
            "web_fetch",
            "Fetch a verified web URL and return readable content with structured source metadata (URL, title, and publication date when available). format selects the output shape: markdown (default), text, or html. Use it to verify important claims found by search; HTTP, binary, encoding, and decoding failures are returned as errors and are not evidence.",
            object_schema(
                {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "format": {
                        "type": "string",
                        "enum": ["text", "markdown", "html"],
                    },
                },
                ["url"],
            ),
        ),
        handlers.web_fetch,
    )

    registry.register(
        ToolSpec(
            "web_search",
            "Search the web and return structured result titles and URLs for source discovery. Empty/unparseable results are errors. For time-sensitive news, prices, or status claims, fetch reliable sources, note their dates, cross-check important facts, and disclose when evidence is insufficient.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                ["query"],
            ),
        ),
        handlers.web_search,
    )
