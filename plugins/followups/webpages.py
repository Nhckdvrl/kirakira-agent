"""Canonical public-page snapshots and bounded, factual change excerpts."""

from __future__ import annotations

import difflib

from lxml import etree, html

from .feeds import MAX_BYTES, MAX_TEXT, Entry, fingerprint

MAX_SNAPSHOT = 24_000


def snapshot(data: bytes, url: str, xpath: str | None = None) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("网页超过 1 MiB")
    parser = html.HTMLParser(no_network=True, huge_tree=False)
    try:
        tree = html.document_fromstring(data, parser=parser, base_url=url)
    except etree.ParserError as error:
        raise ValueError("网页内容无法解析；本次不推进基线") from error
    title = " ".join(tree.xpath("//title/text()")).strip()[:1000]
    for node in tree.xpath(
        "//script|//style|//template|//noscript|//head|//*[@hidden]|//*[@aria-hidden='true']"
    ):
        if node.getparent() is not None:
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
    # Nested selected elements are read once; text, never links or executable markup.
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
    if not text or len(text) > MAX_SNAPSHOT:
        raise ValueError("网页关注区域为空或超过 24,000 字符，请缩小关注区域")
    return {"text": text, "sha256": fingerprint(text), "title": title, "url": url}


def change(previous: dict | None, current: dict, identity_url: str) -> Entry:
    before = "" if previous is None else previous["text"]
    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            current["text"].splitlines(),
            fromfile="previous",
            tofile="current",
            n=2,
            lineterm="",
        )
    )
    # No publication time is inferred from the poll time or the page title.
    payload = {
        "title": current["title"] or "网页内容变化",
        "content": diff[:MAX_TEXT],
        "content_truncated": len(diff) > MAX_TEXT,
        "url": current["url"],
        "published_at": None,
        "page_change": {
            "previous_sha256": None if previous is None else previous["sha256"],
            "current_sha256": current["sha256"],
            "previous_chars": len(before),
            "current_chars": len(current["text"]),
        },
    }
    return Entry(fingerprint(identity_url), current["sha256"], payload)
