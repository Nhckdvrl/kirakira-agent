"""Bounded RSS/Atom decoding. External content supplies facts, never permissions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from lxml import etree, html

MAX_BYTES = 1_048_576
MAX_ENTRIES = 100
MAX_TEXT = 6000


def feed_url(value: str, *, github_releases: bool = True) -> str:
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ValueError("请填写不含账号、密码或片段的 HTTP(S) 信息源地址")
    # Local/private feeds are intentional: this is an authenticated owner's workspace.
    if github_releases and parts.hostname.lower() == "github.com":
        path = parts.path.strip("/").removesuffix(".git").split("/")
        if len(path) == 2 and all(path):
            parts = parts._replace(
                path="/" + "/".join(path) + "/releases.atom", query=""
            )
    try:
        return str(httpx.URL(urlunsplit(parts)))
    except httpx.InvalidURL as error:
        raise ValueError("信息源地址无效") from error


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _plain(value: str) -> str:
    if not value:
        return ""
    tree = html.fragment_fromstring(value, create_parent="div")
    for node in tree.xpath(".//script|.//style"):
        node.drop_tree()
    return " ".join(tree.text_content().split())


def _text(node, name: str) -> str:
    child = next((item for item in node if etree.QName(item).localname == name), None)
    if child is None:
        return ""
    # Atom XHTML and RSS escaped HTML both become inert text.
    return _plain(" ".join(child.itertext()))


def _date(value: str) -> str | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except ValueError, TypeError, OverflowError:
            return None
    return stamp.astimezone(UTC).isoformat() if stamp.tzinfo is not None else None


@dataclass(frozen=True)
class Entry:
    item_id: str
    revision: str
    payload: dict


def decode(data: bytes, url: str) -> tuple[Entry, ...]:
    if len(data) > MAX_BYTES:
        raise ValueError("信息源超过 1 MiB")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    root = etree.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("信息源不能包含 DTD")
    kind = etree.QName(root).localname
    if kind == "feed" and etree.QName(root).namespace == "http://www.w3.org/2005/Atom":
        nodes = [node for node in root if etree.QName(node).localname == "entry"]
    elif kind == "rss":
        nodes = root.findall("./channel/item")
    else:
        raise ValueError("仅支持 RSS 2.0 或 Atom 信息源")
    result: dict[tuple[str, str], Entry] = {}
    identities: set[str] = set()
    for node in nodes[:MAX_ENTRIES]:
        link = _text(node, "link")
        if kind == "feed":
            link = next(
                (
                    child.get("href", "")
                    for child in node
                    if etree.QName(child).localname == "link"
                    and child.get("rel", "alternate") == "alternate"
                ),
                "",
            )
        link = urljoin(url, link) if link else url
        if urlsplit(link).scheme not in {"http", "https"}:
            link = url
        identity = _text(node, "id") or _text(node, "guid") or link
        if identity in identities:
            raise ValueError("同一批信息源包含重复条目身份")
        identities.add(identity)
        title = _text(node, "title")
        body = (
            _text(node, "content")
            or _text(node, "encoded")
            or _text(node, "description")
            or _text(node, "summary")
        )
        published = _date(
            _text(node, "published") or _text(node, "pubDate") or _text(node, "updated")
        )
        raw = {"title": title, "content": body, "url": link, "published_at": published}
        payload = {
            **raw,
            "title": title[:1000],
            "content": body[:MAX_TEXT],
            "content_truncated": len(body) > MAX_TEXT,
        }
        entry = Entry(fingerprint(identity), fingerprint(raw), payload)
        result[(entry.item_id, entry.revision)] = entry
    return tuple(result.values())
