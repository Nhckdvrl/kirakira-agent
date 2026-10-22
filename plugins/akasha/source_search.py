"""Lexical navigation over admitted originals, without new embeddings or facts."""

from __future__ import annotations

import math
import json
import re
from collections import Counter
from dataclasses import dataclass

from plugins.akasha.contract import (
    EvidenceSpan,
    MemoryEvidence,
    MemorySourceReadout,
    MemorySourceSearchQuery,
    MemorySourceSearchReadout,
)
from .infrastructure.lexical import tokenize


@dataclass(frozen=True)
class _Passage:
    source: int
    start: int
    end: int
    counts: Counter[str]
    phrase: bool


def _repeated_lines(fragment: str) -> tuple[str, ...]:
    """Collapse exact repeated lines, retaining changed values and line order."""
    lines = fragment.splitlines()
    if len(lines) < 3:
        return (fragment,)
    middle = [line for line in lines[1:-1] if line]
    if not middle:
        return (fragment,)
    if lines[0] and lines[0] not in middle[0]:
        middle.insert(0, lines[0])
    if lines[-1] and lines[-1] not in middle[-1]:
        middle.append(lines[-1])
    unique = []
    for line in middle:
        if not unique or unique[-1] != line:
            unique.append(line)
    return tuple(unique)


def search_originals(request: MemorySourceSearchQuery,
                     sources: tuple[tuple[MemoryEvidence, str], ...]) -> MemorySourceSearchReadout:
    """Rank overlapping original windows, with exact offsets and one JSON cap.

    Scores only select passages. They express lexical relevance, never whether
    an old decision is still valid. No state or derived index is written.
    """
    query = request.query.strip()
    terms = tuple(sorted(tokenize(query), key=lambda word: (-len(word), word))[:24])
    patterns = {term: re.compile(re.escape(term), re.IGNORECASE) for term in terms}
    phrase = re.compile(re.escape(query), re.IGNORECASE)
    candidates, frequency, total = [], Counter(), 0
    for index, (_, text) in enumerate(sources):
        for start in range(0, len(text), 400):
            end = min(start + 800, len(text))
            fragment = text[start:end]
            total += 1
            counts = Counter({term: len(pattern.findall(fragment)) for term, pattern in patterns.items()})
            counts = +counts
            exact = phrase.search(fragment) is not None
            if counts or exact:
                frequency.update(counts.keys())
                candidates.append(_Passage(index, start, end, counts, exact))
            if end == len(text):
                break
    weights = {term: math.log1p((total - count + 0.5) / (count + 0.5))
               for term, count in frequency.items()}

    def score(candidate):
        return 3.0 * candidate.phrase + sum(weights[term] * count / (count + 1.2)
                                           for term, count in candidate.counts.items())

    ranked = sorted(candidates, key=lambda candidate: (-score(candidate), candidate.source, candidate.start))
    selected, ranges, signatures, truncated = [], [], [], False

    def result():
        return MemorySourceSearchReadout(retrieval_ref=request.retrieval_ref,
            windows=tuple(selected), searched_messages=len(sources), truncated=truncated)

    for candidate in ranked:
        if any(source == candidate.source and min(end, candidate.end) - max(start, candidate.start)
               >= 0.5 * min(end - start, candidate.end - candidate.start) for source, start, end in ranges):
            continue
        fragment = sources[candidate.source][1][candidate.start:candidate.end]
        signature = _repeated_lines(fragment)
        if (candidate.source, signature) in signatures:
            # Only suppress repeated passages inside the same original message.
            # Identical wording in different dated messages must stay distinct.
            continue
        if len(selected) == request.limit:
            truncated = True
            break
        evidence, text = sources[candidate.source]
        strongest = max(candidate.counts, key=lambda term: (weights[term], len(term), term), default=None)
        pattern = phrase if candidate.phrase else patterns[strongest]
        match = pattern.search(text, candidate.start, candidate.end)
        anchor = match.start() if match is not None else candidate.start
        size = candidate.end - candidate.start
        while size >= 16:
            start = max(candidate.start, min(anchor - size // 3, candidate.end - size))
            end = start + size
            span = EvidenceSpan(message_id=evidence.message_id, start=start, end=end, source_chars=len(text))
            window = MemorySourceReadout.model_validate_json(json.dumps({
                "retrieval_ref": request.retrieval_ref, "text": text[start:end],
                "evidence": {**evidence.model_dump(mode="json"), "excerpt": span.model_dump(mode="json")},
                "next_offset": end if end < len(text) else None,
            }, ensure_ascii=False))
            selected.append(window)
            over = len(result().model_dump_json()) - request.max_chars
            if over <= 0:
                ranges.append((candidate.source, start, end))
                signatures.append((candidate.source, signature))
                break
            selected.pop()
            truncated = True
            size -= max(over + 8, 16)
        else:
            truncated = True
            break
    value = result()
    if len(value.model_dump_json()) > request.max_chars:
        raise ValueError("source search metadata does not fit its output budget")
    return value
