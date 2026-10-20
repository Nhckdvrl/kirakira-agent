"""Deterministic, extractive evidence windows under one shared character budget."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .infrastructure.lexical import tokenize


@dataclass(frozen=True)
class Passage:
    message_id: str
    text: str
    prefix: str


def window(text: str, query: str, size: int) -> tuple[int, int]:
    """Return original-string offsets; never summarize or synthesize source text."""
    if len(text) <= size:
        return 0, len(text)
    terms = sorted((term for term in tokenize(query)
                    if len(term) > 2 or (len(term) > 1 and not term.isascii())),
                   key=lambda term: (-len(term), term))[:32]
    matches = []
    for term in terms:
        positions = [(m.start(), m.end()) for m in re.finditer(re.escape(term), text, re.IGNORECASE)][:64]
        for start, end in positions:
            matches.append((start, end, 1.0 / len(positions)))
    starts = {0, *(max(0, min(len(text) - size, start - size // 4)) for start, _, _ in matches)}
    start = max(sorted(starts), key=lambda offset: sum(weight for left, right, weight in matches
                                                     if left >= offset and right <= offset + size))
    return start, start + size


def render(passages: Sequence[Passage], *, query: str, header: str,
           max_chars: int, identity: str) -> dict[str, object]:
    """Share body space across candidates; small messages return unused quota."""
    selected, overhead, minimum = [], len(header), 0
    for passage in passages:
        # Reserve the longest possible offset header, including the separator.
        reserve = len(passage.prefix) + len(f"片段：[{len(passage.text)}:{len(passage.text)}] / {len(passage.text)} 字符\n\n")
        if overhead + reserve + 2 * bool(selected) + minimum + min(48, len(passage.text)) > max_chars:
            continue
        selected.append(passage)
        overhead += reserve + 2 * (len(selected) > 1)
        minimum += min(48, len(passage.text))
    if not selected:
        return {"reminders": (), "references": ()}
    allocations = [0] * len(selected)
    remaining = max_chars - overhead
    active = list(range(len(selected)))
    while remaining and active:
        share = max(1, remaining // len(active))
        for index in active:
            take = min(share, remaining, len(selected[index].text) - allocations[index])
            allocations[index] += take
            remaining -= take
        active = [index for index in active if allocations[index] < len(selected[index].text)]
    rows, references = [], []
    for passage, size in zip(selected, allocations, strict=True):
        if size == 0:
            continue
        start, end = window(passage.text, query, size)
        rows.append(passage.prefix + f"片段：[{start}:{end}] / {len(passage.text)} 字符\n\n" + passage.text[start:end])
        references.append({"ref": passage.message_id, "resolved_ref": passage.message_id,
                           "retrieval_ref": identity, "start": start, "end": end,
                           "source_chars": len(passage.text)})
    material = header + "\n\n".join(rows)
    if len(material) > max_chars:
        raise ValueError("extractive material exceeded its declared budget")
    return {"reminders": ({"name": "recall", "text": material, "priority": 300},),
            "references": tuple(references)}
