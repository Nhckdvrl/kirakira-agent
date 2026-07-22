"""三通道事件契约与归一化。

参考 akashic 的 `proactive_v2/contracts.py`，MVP 精简为渲染 prompt 所需的最小字段。

三种通道语义：
- ``alert``   高优先级告警，直接透传推送
- ``content`` 内容候选，经 LLM 兴趣判断后决定是否推送
- ``context`` 背景状态，只辅助判断，不单独触发推送
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict

VALID_CHANNELS = ("alert", "content", "context")


def _text(value: Any) -> str:
    return str(value or "").strip()


def canonical_item_id(source_id: str, event: Dict[str, Any]) -> str:
    """稳定事件身份：``<source_id>:<event_id>``，用于跨轮去重与 ACK。"""
    event_id = _text(event.get("event_id") or event.get("id")) or "?"
    return "%s:%s" % (source_id or "?", event_id)


@dataclass(slots=True)
class AlertContract:
    item_id: str
    title: str
    content: str
    severity: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_line(self) -> str:
        parts = [f"id={self.item_id}", f"title={self.title}"]
        if self.severity:
            parts.append(f"severity={self.severity}")
        if self.content:
            parts.append(f"内容：{self.content}")
        return "  " + "\n       ".join(parts)


@dataclass(slots=True)
class ContentContract:
    item_id: str
    title: str
    source: str
    url: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_line(self, index: int) -> str:
        url_part = f"\n       url={self.url}" if self.url else ""
        return (
            f"  [{index}] id={self.item_id}\n"
            f"       title={self.title}\n"
            f"       source={self.source}{url_part}"
        )


@dataclass(slots=True)
class ContextContract:
    source: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_line(self) -> str:
        payload = dict(self.raw)
        payload.pop("kind", None)
        return "  " + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def normalize_alert(event: Dict[str, Any]) -> AlertContract:
    return AlertContract(
        item_id=_text(event.get("item_id")) or canonical_item_id(_text(event.get("_source")), event),
        title=_text(event.get("title")),
        content=_text(event.get("content") or event.get("body")),
        severity=_text(event.get("severity")),
        raw=event,
    )


def normalize_content(event: Dict[str, Any]) -> ContentContract:
    return ContentContract(
        item_id=_text(event.get("item_id")) or canonical_item_id(_text(event.get("_source")), event),
        title=_text(event.get("title")),
        source=_text(event.get("source") or event.get("source_name") or event.get("_source")),
        url=_text(event.get("url")),
        raw=event,
    )


def normalize_context(event: Dict[str, Any]) -> ContextContract:
    return ContextContract(
        source=_text(event.get("_source") or event.get("source")),
        raw=event,
    )
