"""Canonical public-page snapshots and bounded, factual change excerpts."""

from __future__ import annotations

import codecs
import difflib
import json
import re
from email.message import Message
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree, html

from .feeds import MAX_BYTES, MAX_TEXT, Entry, fingerprint

MAX_SNAPSHOT = 24_000
SNAPSHOT_VERSION = 2
MAX_INTERACTIONS = 64
MAX_INTERACTION_CHARS = 12_000
_QUERY_FIELDS = frozenset({
    "id", "event", "event_id", "show", "show_id", "product", "product_id",
    "sku", "variant", "category", "date", "day", "lang", "locale", "page",
})
_CONTROLS = frozenset({"button", "input", "select", "textarea", "option", "optgroup"})
_CREDENTIAL_ROUTES = frozenset({
    "magic-link", "magiclink", "login-link", "signin-link", "sign-in-link",
    "reset-password", "password-reset", "resetpassword", "passwordreset",
    "verify-email", "email-verification", "confirm-email", "activate-account",
    "access-token", "token", "tokens", "auth", "oauth", "oauth2", "sso",
    "signed", "signed-url", "signature",
})


def safe_url(value: str | None, base: str) -> str | None:
    """Keep a useful public destination, never bearer or tracking query data."""
    if not value or len(value) > 2048 or value.strip().startswith("#"):
        return None
    try:
        raw = unquote(value.strip())
        if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in raw):
            return None
        parts = urlsplit(urljoin(base, value.strip()))
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        _ = parts.port
        path = parts.path
        for _ in range(3):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        if any(part.split(";", 1)[0].casefold().replace("_", "-") in _CREDENTIAL_ROUTES
               for part in path.split("/")):
            return None
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=128):
            # Unknown names are dropped as well: signed links use many provider-
            # specific names, and none of those values belong in model evidence.
            if (key.casefold() in _QUERY_FIELDS and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item)
                    and not re.fullmatch(r"[a-fA-F0-9]{24,}", item)):
                query.append((key.casefold(), item))
        result = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))
        return result if len(result) <= 1024 else None
    except ValueError, UnicodeError:
        return None


def _disabled(node) -> bool:
    if node.tag not in _CONTROLS:
        return False
    if "disabled" in node.attrib:
        return True
    ancestors = tuple(node.iterancestors())
    if node.tag == "option" and any(
        parent.tag in {"select", "optgroup"} and "disabled" in parent.attrib for parent in ancestors
    ):
        return True
    for parent in ancestors:
        if parent.tag != "fieldset" or "disabled" not in parent.attrib:
            continue
        legend = next((child for child in parent if child.tag == "legend"), None)
        if legend is None or legend is not node and legend not in ancestors:
            return True
    return False


def _interactions(selected, url: str) -> list[dict]:
    result = []
    for root in selected:
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            kind = node.tag.lower()
            input_type = node.get("type", "text").casefold()
            role = node.get("role", "").casefold()
            if kind == "input" and input_type not in {"button", "submit", "reset", "image"}:
                continue  # An ARIA role must never expose hidden/password/form values.
            if not (kind in {"a", "button", "option", "select"}
                    or kind == "input" and input_type in {"button", "submit", "reset", "image"}
                    or role in {"button", "link", "option", "menuitem"}):
                continue
            if kind == "option" and not any(parent.tag == "select" for parent in node.iterancestors()):
                continue
            label = node.get("aria-label") or node.get("title") or " ".join(node.itertext())
            if kind == "input":
                label = node.get("alt", "") if input_type == "image" else node.get("value", "")
            label = " ".join(label.split())[:160]
            lineage = (node, *node.iterancestors())
            native_disabled = _disabled(node)
            aria_disabled = any(parent.get("aria-disabled", "").strip().casefold() == "true" for parent in lineage)
            inert = any("inert" in parent.attrib for parent in lineage)
            item = {"tag": kind, "label": label, "disabled": native_disabled or aria_disabled or inert,
                    "native_disabled": native_disabled, "aria_disabled": aria_disabled, "inert": inert}
            if role:
                item["role"] = role[:40]
            if kind == "a" or role == "link":
                item["href"] = safe_url(node.get("href"), url)
            if kind in {"button", "input"}:
                item["type"] = node.get("type", "submit" if kind == "button" else "text").casefold()[:40]
                form = next((parent for parent in node.iterancestors() if parent.tag == "form"), None)
                if node.get("form"):
                    forms = node.getroottree().xpath("//form[@id=$identity]", identity=node.get("form"))
                    form = next(iter(forms), None)
                if form is not None and item["type"] in {"submit", "image"}:
                    item["form_action"] = safe_url(node.get("formaction") or form.get("action") or url, url)
                    method = (node.get("formmethod") or form.get("method", "get")).casefold()
                    item["form_method"] = method if method in {"get", "post", "dialog"} else "unknown"
            result.append(item)
            if len(result) > MAX_INTERACTIONS:
                raise ValueError("网页关注区域超过 64 个交互元素，请缩小关注区域；本次不推进基线")
    if len(json.dumps(result, ensure_ascii=False)) > MAX_INTERACTION_CHARS:
        raise ValueError("网页交互状态超过 12,000 字符，请缩小关注区域；本次不推进基线")
    return result


def _document(data: bytes, url: str, encoding: str | None):
    bom = data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE,
                           codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE))
    try:
        declared = None if encoding is None else codecs.lookup(encoding).name
        parser = html.HTMLParser(no_network=True, huge_tree=False, encoding=None if bom else declared)
        tree = html.document_fromstring(data, parser=parser, base_url=url)
    except LookupError, ValueError:
        raise ValueError("网页声明的字符编码无效或不受支持；本次不推进基线") from None
    except etree.ParserError as error:
        raise ValueError("网页内容无法解析；本次不推进基线") from error
    if any(entry.type_name in {"ERR_UNSUPPORTED_ENCODING", "ERR_INVALID_ENCODING"}
           for entry in parser.error_log):
        raise ValueError("网页声明的字符编码无效或正文不符合该编码；本次不推进基线")
    if bom or declared is not None:
        return tree
    # Let the HTML parser honor real document declarations before applying a
    # UTF-8 fallback. HTTP defaults must not override a declared GBK document.
    for meta in tree.xpath("//meta"):
        if "charset" in meta.attrib:
            return tree
        if meta.get("http-equiv", "").strip().casefold() == "content-type":
            header = Message()
            header["content-type"] = meta.get("content", "")
            if header.get_content_charset() is not None:
                return tree
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("网页未声明有效字符编码且不是 UTF-8；本次不推进基线") from None
    return html.document_fromstring(data, parser=html.HTMLParser(
        no_network=True, huge_tree=False, encoding="utf-8"), base_url=url)


def snapshot(data: bytes, url: str, xpath: str | None = None, *, encoding: str | None = None) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("网页超过 1 MiB")
    tree = _document(data, url, encoding)
    title = " ".join(tree.xpath("//title/text()")).strip()[:1000]
    for node in tree.xpath(
        "//script|//style|//template|//noscript|//head|//*[@hidden]|//*[@aria-hidden='true']"
    ):
        if node.getparent() is not None:
            node.drop_tree()
    for node in tuple(tree.iter()):
        if not isinstance(node.tag, str) or node.getparent() is None:
            continue
        style = re.sub(r"\s+", "", node.get("style", "")).casefold()
        if node.get("aria-hidden", "").strip().casefold() == "true" or re.search(
            r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style
        ):
            node.drop_tree()
    if xpath:
        try:
            selected = tree.xpath(xpath, smart_strings=False)
        except etree.XPathError as error:
            raise ValueError("无法读取指定关注区域") from error
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(node, html.HtmlElement) for node in selected)
        ):
            raise ValueError("关注区域必须匹配网页元素；本次不推进基线")
    else:
        for node in tree.xpath("//nav|//footer|//aside"):
            if node.getparent() is not None:
                node.drop_tree()
        selected = tree.xpath("//main") or tree.xpath("//body")
    # Nested selected regions are read once; executable markup is never retained.
    selected_ids = set(selected)
    selected = [
        node for node in selected if not selected_ids.intersection(node.iterancestors())
    ]
    lines = [
        " ".join(value.split())
        for node in selected
        for value in node.itertext()
        if value.strip()
    ]
    text = "\n".join(lines)
    interactions = _interactions(selected, url)
    if not text and not interactions or len(text) > MAX_SNAPSHOT:
        raise ValueError("网页关注区域为空或超过 24,000 字符，请缩小关注区域")
    return {"schema_version": SNAPSHOT_VERSION, "text": text, "interactions": interactions,
            "sha256": fingerprint({"text": text, "interactions": interactions}), "title": title, "url": url}


def has_change(previous: dict | None, current: dict) -> bool:
    """Upgrade old text-only baselines without inventing past control states."""
    if previous is None:
        return True
    if previous.get("schema_version") != SNAPSHOT_VERSION:
        return previous["text"] != current["text"]
    return previous["sha256"] != current["sha256"]


def change(previous: dict | None, current: dict, identity_url: str, *, initial: bool = False) -> Entry:
    before = "" if previous is None else previous["text"]
    text_diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            current["text"].splitlines(),
            fromfile="previous",
            tofile="current",
            n=2,
            lineterm="",
        )
    )
    known_controls = previous is not None and previous.get("schema_version") == SNAPSHOT_VERSION
    current_controls = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in current["interactions"]]
    if initial:
        diff = "首次现状检查（没有历史对照，不表示刚刚发生变化）\n当前交互状态：\n" + "\n".join(current_controls) + "\n当前页面正文：\n" + current["text"]
    elif known_controls:
        old_controls = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in previous["interactions"]]
        control_diff = "\n".join(difflib.unified_diff(old_controls, current_controls,
            fromfile="previous_interactions", tofile="current_interactions", n=2, lineterm=""))
        diff = "\n".join(value for value in (control_diff, text_diff) if value)
    else:
        diff = text_diff + "\n此前未记录交互状态；以下仅为当前状态，不代表控件刚刚变化：\n" + "\n".join(current_controls)
    # No publication time is inferred from the poll time or the page title.
    payload = {
        "title": current["title"] or "网页内容变化",
        "content": diff[:MAX_TEXT],
        "content_truncated": len(diff) > MAX_TEXT,
        "url": current["url"],
        "published_at": None,
        "page_change": {
            "initial": initial,
            "kind": "initial_state" if initial else "change",
            "previous_sha256": None if previous is None else previous["sha256"],
            "current_sha256": current["sha256"],
            "previous_chars": len(before),
            "current_chars": len(current["text"]),
            "previous_interactions_known": known_controls,
            "current_interactions": len(current["interactions"]),
            "interaction_scope": "静态 HTML 的按钮和链接属性；不执行脚本，不代表已可成功下单",
        },
    }
    return Entry(fingerprint(identity_url), current["sha256"], payload)
