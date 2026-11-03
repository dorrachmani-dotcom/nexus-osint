"""AI-assisted planner for user-defined API sources.

Given an API's documentation (a URL we fetch, and/or a pasted sample response
in JSON, NDJSON, XML, CSV, RSS/Atom or HTML), the analyst's chosen AI provider works out how to pull data from it:
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
from html.parser import HTMLParser

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
    "response_format", "pagination", "page_param", "page_size_param", "page_size",
    "page_start", "cursor_path", "max_pages", "body_template", "extra_headers",
    "oauth_token_url", "oauth_scope",
}
# Never accepted from the model: TLS verification is the analyst's security call.

_SYSTEM_PROMPT = """You are an OSINT integration assistant. Your job is to read an \
API's documentation and/or a sample response and produce a JSON configuration \
that a generic collector will use to pull public content from that API.

The collector reads JSON, NDJSON, XML, CSV, RSS/Atom and HTML. A non-JSON sample \
is shown to you already CONVERTED to JSON exactly as the collector sees it: XML \
becomes {"root_tag": {"child": ..., "@attr": ...}} (repeated children become \
arrays); CSV, NDJSON and RSS become an array of records (RSS records have title, \
link, summary, author, published, id); HTML becomes [{"title", "text", "url"}]. \
Write every dotted path against that converted JSON.

Return ONLY a single JSON object (no prose, no markdown fences) with these keys:
- name: a short human label for the source (e.g. "ACLED conflict events").
- base_url: the API origin, e.g. "https://api.example.com".
- endpoint: the path appended to base_url, e.g. "/v1/posts". It MAY contain the \
literal token {query} where a search term should be inserted into the path.
- http_method: "GET" or "POST".
- body_template: for POST only, the JSON request body as a string, with the \
literal token {query} where the search term goes (inside a JSON string). null \
for GET.
- auth_type: one of "none", "header", "query", "bearer", "basic", "oauth2" \
(OAuth2 client credentials).
- auth_param: the header name (for header/bearer) or query-parameter name (for \
query auth) that carries the API key; for "oauth2" the client ID only if the \
docs show a public one, else null. Use null when auth_type is "none" or \
"basic". For "bearer" this is usually "Authorization".
- oauth_token_url, oauth_scope: the OAuth2 token endpoint and scope (oauth2 \
only), else null.
- extra_headers: a JSON object of constant NON-secret headers (e.g. \
{"Accept": "application/json"}). Use {} if none. Never put keys or tokens here.
- query_param: the query-parameter name that should carry the analyst's search \
term, or null if this endpoint takes no search term.
- extra_params: a JSON object of constant query parameters always sent (e.g. \
{"limit": 50, "lang": "en"}). Use {} if none.
- response_format: "auto" unless the docs say otherwise ("json", "ndjson", \
"xml", "csv", "rss", "html").
- items_path: the dotted path to the ARRAY of items inside the (converted) JSON \
response (e.g. "data.results"). Use "" if the response is itself the array.
- pagination: "none", "page" (page number), "offset" (offset/limit), "cursor" \
(a cursor from the response is sent back), or "next_link" (the response gives \
the next page URL, in the body or the Link header).
- page_param: the query parameter carrying the page number / offset / cursor \
(null for none and next_link). page_size_param, page_size: the limit parameter \
and a sensible value (null / 0 if none). page_start: the first page number \
(usually 1). cursor_path: the dotted path to the next cursor (cursor) or the \
next page URL (next_link; null when it is in the Link header). max_pages: pages \
to read per scan (1-5 is usually enough).
- map_title, map_content, map_url, map_author, map_published: the dotted path \
WITHIN one item object to that field. map_content (the main text) is the most \
important. Use null for any field the API does not provide (with no mapping at \
all, each record is stored as readable "key: value" text).
- notes: one or two sentences explaining how this source works and what data it \
returns, for the analyst to review.

Never invent endpoints or fields that are not supported by the provided material. \
If something is unknown, use null and explain the gap in notes. Do NOT include \
the API key, token or any secret anywhere in the output."""


def _fetch_docs(url: str) -> str:
    """Fetch documentation text from a URL, guarded against SSRF. Best-effort."""
    from nexus.netguard import safe_get

    safe, reason = _is_safe_capture_url(url)
    if not safe:
        logger.warning("Refusing to fetch docs URL: %s", reason)
        return ""
    try:
        # Every redirect hop is re-checked, so a public URL cannot bounce the
        # request to a loopback or private-network address.
        resp = safe_get(url, timeout=20.0)
        resp.raise_for_status()
        text = resp.text or ""
    except Exception as exc:
        logger.warning("Could not fetch docs URL (%s)", type(exc).__name__)
        return ""
    return _html_to_text(text)[:_DOCS_MAX_CHARS]


class _TextExtractor(HTMLParser):
    """Collects visible text, skipping <script> and <style> content entirely."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever text was collected
        logger.debug("HTML to text conversion stopped early", exc_info=True)
    return " ".join(" ".join(parser.parts).split())


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
    if "extra_headers" in out:
        headers = out["extra_headers"]
        out["extra_headers"] = json.dumps(headers) if isinstance(headers, dict) else "{}"
    if isinstance(out.get("body_template"), (dict, list)):
        out["body_template"] = json.dumps(out["body_template"])
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
        from nexus.sources.api_formats import sample_preview

        fmt, preview = sample_preview(sample, _SAMPLE_MAX_CHARS)
        if fmt == "json":
            parts.append("Sample JSON response from the API:\n" + preview)
        else:
            parts.append(
                f"Sample response from the API (detected format: {fmt}), converted "
                f"to JSON the way the collector sees it:\n{preview}"
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
