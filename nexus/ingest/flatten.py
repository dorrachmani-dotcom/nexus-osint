"""Turn any JSON-like record into readable ``key: value`` text.

Used when there is no field mapping to follow — an uploaded JSON/NDJSON file,
an XML record, or (later) an API response whose shape nobody described. The
goal is that *something searchable* is always stored instead of nothing.
"""

from __future__ import annotations

_MAX_LINES = 400
_MAX_VALUE = 2000


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "…"


def flatten_lines(obj, prefix: str = "", max_lines: int = _MAX_LINES) -> list[str]:
    """Depth-first ``dotted.key: value`` lines for a nested dict/list.

    Lists of scalars are joined on one line; empty values are dropped so the
    text stays readable. Output is capped at ``max_lines``.
    """
    out: list[str] = []

    def walk(node, path: str) -> None:
        if len(out) >= max_lines:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
                if len(out) >= max_lines:
                    return
        elif isinstance(node, list):
            if node and all(not isinstance(x, (dict, list)) for x in node):
                joined = ", ".join(s for s in (_scalar(x) for x in node) if s)
                if joined:
                    out.append(f"{path}: {joined}" if path else joined)
                return
            for i, v in enumerate(node):
                walk(v, f"{path}.{i}" if path else str(i))
                if len(out) >= max_lines:
                    return
        else:
            text = _scalar(node)
            if text:
                out.append(f"{path}: {text}" if path else text)

    walk(obj, prefix)
    return out


def flatten_to_text(obj, max_lines: int = _MAX_LINES) -> str:
    return "\n".join(flatten_lines(obj, max_lines=max_lines))


_TITLE_KEYS = ("title", "name", "headline", "subject", "label", "summary")


def guess_title(obj) -> str | None:
    """A short title for a record: the first title-ish scalar field, if any."""
    if not isinstance(obj, dict):
        return None
    lower = {str(k).lower(): v for k, v in obj.items()}
    for key in _TITLE_KEYS:
        v = lower.get(key)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()[:200]
    return None
