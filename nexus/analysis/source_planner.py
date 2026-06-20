"""AI-assisted planner for user-defined API sources.

Given an API's documentation (a URL we fetch, and/or a pasted sample JSON
response), the analyst's chosen AI provider works out how to pull data from it:
the base URL, how it authenticates, which endpoint to call, where the list of
items lives in the response, and which field is the title/text/link/author/date.

The output is a config dict that drops straight into the custom_sources table and
is then run by nexus/sources/custom.py — so "describe an API once" becomes "a new
source the collector scans automatically".

Graceful by design: if no AI provider is connected, plan_source returns an error
dict (the UI then lets the analyst fill the fields in by hand). Fetching a docs
URL reuses the evidence SSRF guard so the model can't be pointed at internal
hosts, and the model's reply is parsed defensively.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from nexus.analysis.providers import get_provider
from nexus.config import Settings, get_settings
from nexus.evidence import _is_safe_capture_url

logger = logging.getLogger("nexus.analysis.source_planner")

_DOCS_MAX_CHARS = 12000
_SAMPLE_MAX_CHARS = 8000
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

# Keys we accept back from the model — anything else is ignored so a chatty reply
# can't inject unexpected fields into the stored config.
_ALLOWED_KEYS = {
    "name", "base_url", "endpoint", "http_method", "auth_type", "auth_param",
    "query_param", "extra_params", "items_path",
    "map_title", "map_content", "map_url", "map_author", "map_published", "notes",
}

_SYSTEM_PROMPT = """You are an OSINT integration assistant. Your job is to read an \
API's documentation and/or a sample JSON response and produce a JSON configuration \
that a generic collector will use to pull public content from that API.

Return ONLY a single JSON object (no prose, no markdown fences) with these keys:
- name: a short human label for the source (e.g. "ACLED conflict events").
- base_url: the API origin, e.g. "https://api.example.com".
- endpoint: the path appended to base_url, e.g. "/v1/posts". It MAY contain the \
literal token {query} where a search term should be inserted into the path.
- http_method: "GET" or "POST".
- auth_type: one of "none", "header", "query", "bearer".
- auth_param: the header name (for header/bearer) or query-parameter name (for \
query auth) that carries the API key. Use null when auth_type is "none". For \
"bearer" this is usually "Authorization".
- query_param: the query-parameter name that should carry the analyst's search \
term, or null if this endpoint takes no search term.
- extra_params: a JSON object of constant query parameters always sent (e.g. \
{"limit": 50, "lang": "en"}). Use {} if none.
- items_path: the dotted path to the ARRAY of items inside the JSON response \
(e.g. "data.results"). Use "" if the response is itself the array.
- map_title, map_content, map_url, map_author, map_published: the dotted path \
WITHIN one item object to that field. map_content (the main text) is the most \
important. Use null for any field the API does not provide.
- notes: one or two sentences explaining how this source works and what data it \
returns, for the analyst to review.

Never invent endpoints or fields that are not supported by the provided material. \
If something is unknown, use null and explain the gap in notes. Do NOT include \
the API key itself anywhere in the output."""


def _fetch_docs(url: str) -> str:
    """Fetch documentation text from a URL, guarded against SSRF. Best-effort."""
    safe, reason = _is_safe_capture_url(url)
    if not safe:
        logger.warning("Refusing to fetch docs URL: %s", reason)
        return ""
    try:
        resp = httpx.get(url, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text or ""
    except Exception as exc:
        logger.warning("Could not fetch docs URL %s: %s", url, exc)
        return ""
    # Strip tags crudely so the model sees mostly text, not markup.
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_DOCS_MAX_CHARS]


def _coerce_config(data: dict) -> dict:
    """Keep only allowed keys and normalise types for storage."""
    out: dict = {}
    for key in _ALLOWED_KEYS:
        if key in data:
            out[key] = data[key]
    extra = out.get("extra_params")
    if isinstance(extra, dict):
        out["extra_params"] = json.dumps(extra)
    elif not isinstance(extra, str):
        out["extra_params"] = "{}"
    # Null -> empty string for the simple text fields (storage handles the rest).
    for key in list(out.keys()):
        if out[key] is None:
            out[key] = ""
    return out


def plan_source(
    docs_url: str | None = None,
    sample: str | None = None,
    hint: str | None = None,
    settings: Settings | None = None,
) -> dict:
    """Ask the active AI provider to produce a custom-source config.

    Returns ``{"ok": True, "config": {...}}`` on success, or
    ``{"ok": False, "error": "..."}`` (e.g. no AI connected, no usable reply).
    Never raises.
    """
    settings = settings or get_settings()
    provider = get_provider(settings)
    if provider is None:
        return {
            "ok": False,
            "error": "No AI provider is connected. Connect one in Settings, or "
            "fill in the source fields manually.",
        }

    parts: list[str] = []
    if hint:
        parts.append(f"Analyst hint about the API:\n{hint.strip()}")
    if docs_url:
        docs = _fetch_docs(docs_url.strip())
        if docs:
            parts.append(f"Documentation fetched from {docs_url.strip()}:\n{docs}")
        else:
            parts.append(
                f"(Could not fetch {docs_url.strip()} — rely on the hint/sample.)"
            )
    if sample:
        parts.append(
            "Sample JSON response from the API:\n" + sample.strip()[:_SAMPLE_MAX_CHARS]
        )
    if not parts:
        return {"ok": False, "error": "Provide a docs URL, a sample response, or a hint."}

    user_prompt = "\n\n".join(parts)
    try:
        reply = provider.complete(_SYSTEM_PROMPT, user_prompt, max_tokens=1500)
    except Exception as exc:
        logger.exception("Source planner model call failed")
        return {"ok": False, "error": f"AI request failed: {exc}"}

    match = _JSON_OBJ_RE.search(reply or "")
    if not match:
        return {"ok": False, "error": "The AI did not return a usable configuration."}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"ok": False, "error": "The AI returned malformed JSON; try again."}
    if not isinstance(data, dict):
        return {"ok": False, "error": "The AI returned an unexpected shape."}

    return {"ok": True, "config": _coerce_config(data)}
