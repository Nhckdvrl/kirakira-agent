"""Bound tool output text without losing the truncation marker."""

OUTPUT_LIMIT = 50000


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (%d characters truncated)" % (len(text) - limit)
