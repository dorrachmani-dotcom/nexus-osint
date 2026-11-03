"""Generic, config-driven source for user-defined APIs.

This is the engine behind the dashboard's "add a new API source" feature. An
analyst (optionally helped by the AI planner) describes any REST API once — its
base URL, how it authenticates, how it pages, which field is the post text —
and this class turns that config into a normal `Source` the collector scans
like any built-in one.

What it handles:
  * Response formats: JSON, NDJSON, XML, CSV, RSS/Atom and HTML, auto-detected
    from the Content-Type and a sniff of the body (``nexus/sources/api_formats``).
  * Pagination: page number, offset/limit, a cursor read from the response
    (dotted path), or a next link (dotted path in the body, or the ``Link``
    header) — always capped by ``max_pages``.
  * Auth: none, a header, a query parameter, bearer, Basic, OAuth2 client
    credentials (token cached in memory only), plus any number of secret
    headers. Every secret lives in ``.env`` (``CUSTOM_SOURCE_<id>_KEY`` and
    ``CUSTOM_SOURCE_<id>_HEADERS``) — never the database.
  * POST bodies from a JSON template with a ``{query}`` placeholder.
  * No field mapping: each record is flattened into readable text so items
    are still stored.

Design rules it inherits from the rest of the project:
  * Graceful degradation: a missing key, an unreachable host, or an unexpected
    shape logs a warning and yields fewer/no items — it never crashes a scan.
  * Every request goes through ``nexus.netguard.safe_request``: the SSRF policy
    (including the opt-in internal allow-list) is applied to the first URL and
    re-checked on every redirect hop; per-source TLS mode (internal CA / skip
    verification) is honoured. A next-page link may never leave the API host.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from nexus.config import Settings, get_settings
from nexus.envstore import (
    custom_source_env_name,
    custom_source_headers_env_name,
    read_env_value,
)
from nexus.ingest.flatten import flatten_to_text, guess_title
from nexus.models import RawItem
from nexus.netguard import ResponseTooLarge, safe_request, tls_verify_for
from nexus.sources import api_formats
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.custom")

_TIMEOUT = 30.0
_MAX_ITEMS = 100           # per page
_MAX_ITEMS_PER_QUERY = 500  # across all pages of one query
_MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES_CAP = 20
KEYED_AUTH = ("header", "query", "bearer", "basic", "oauth2")


def _parse_date(value) -> datetime | None:
    """Best-effort date parse: unix epoch, ISO 8601, RFC 822, or give up (None)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            # Milliseconds since the epoch are common in APIs.
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    # Numeric string -> epoch seconds.
    if text.isdigit():
        try:
            n = int(text)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=UTC)
        except (OverflowError, OSError, ValueError):
            logger.debug("Numeric date %r is not a valid epoch; trying ISO format", text, exc_info=True)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def _dig(obj, path: str | None):
    """Walk a dotted path (e.g. "data.items.0.text") into nested JSON.

    Supports dict keys and integer list indices. Returns None on any miss so a
    bad mapping degrades to an empty field rather than an exception.
    """
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        part = part.strip()
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    # Lists/dicts: serialise compactly so nothing is silently lost.
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _json_obj(raw: str | None) -> dict:
    try:
        val = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _link_next(header: str | None) -> str | None:
    """The ``rel="next"`` target of an RFC 8288 ``Link`` header, if any."""
    for part in (header or "").split(","):
        if 'rel="next"' in part.replace("'", '"') or "rel=next" in part:
            start, end = part.find("<"), part.find(">")
            if 0 <= start < end:
                return part[start + 1:end].strip()
    return None


_URL_KEYS = ("url", "link", "href", "permalink", "web_url", "html_url")
_DATE_KEYS = ("published", "published_at", "date", "created_at", "created",
              "updated", "updated_at", "pubdate", "timestamp", "time")
_AUTHOR_KEYS = ("author", "user", "username", "by", "creator", "owner")


def _first_scalar(raw: dict, keys: tuple[str, ...]):
    lower = {str(k).lower(): v for k, v in raw.items()}
    for key in keys:
        v = lower.get(key)
        if isinstance(v, dict):
            v = v.get("name") or v.get("login") or v.get("username")
        if isinstance(v, (str, int, float)) and str(v).strip():
            return v
    return None


# ------------------------------------------------------------- OAuth2 tokens
# Client-credentials access tokens are cached in memory only (never written to
# disk), keyed by source id + token URL + client id, until shortly before expiry.
_TOKENS: dict[tuple[int, str, str], tuple[str, float]] = {}
_TOKENS_LOCK = threading.Lock()


def clear_token_cache() -> None:
    with _TOKENS_LOCK:
        _TOKENS.clear()


@dataclass
class _Page:
    """Where the next request goes (pagination state)."""

    n: int = 0                       # pages fetched so far
    params: dict = field(default_factory=dict)
    url: str | None = None           # absolute next-link URL, when following links


class CustomApiSource(Source):
    """A Source built from a stored custom_sources config row."""

    def __init__(self, cfg: dict, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg
        self.id = int(cfg["id"])
        # The label is the source tag stamped on every item it collects.
        self.name = (cfg.get("name") or f"custom{self.id}").strip() or f"custom{self.id}"
        # Auto-Adapt health signals, set by fetch() and read by the collector:
        #   responded   — did the API return a usable response this scan?
        #   last_sample — the latest parsed response as JSON (for AI re-mapping).
        self.responded = False
        self.last_sample: str | None = None
        self.last_error: str = ""
        self.detected_format: str | None = None
        self._last_headers: httpx.Headers = httpx.Headers()

    # ------------------------------------------------------------- availability
    def _api_key(self) -> str | None:
        return read_env_value(custom_source_env_name(self.id))

    def _secret_headers(self) -> dict[str, str]:
        raw = read_env_value(custom_source_headers_env_name(self.id))
        return {str(k).strip(): str(v) for k, v in _json_obj(raw).items()
                if str(k).strip() and "\n" not in str(v)}

    @property
    def auth_type(self) -> str:
        return (self.cfg.get("auth_type") or "none").lower()

    def is_available(self) -> bool:
        if not self.cfg.get("enabled", 1):
            return False
        if not (self.cfg.get("base_url") or "").strip():
            return False
        if self.auth_type in KEYED_AUTH and not self._api_key():
            return False
        if self.auth_type == "oauth2" and not (self.cfg.get("oauth_token_url") or "").strip():
            return False
        return True

    # ------------------------------------------------------------------ request
    def _auth(self) -> tuple[dict, dict]:
        """Return (extra_headers, extra_params) implementing the auth scheme."""
        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        auth = self.auth_type
        key = self._api_key() or ""
        if not key:
            return headers, params
        if auth == "bearer":
            headers[self.cfg.get("auth_param") or "Authorization"] = f"Bearer {key}"
        elif auth == "header":
            headers[self.cfg.get("auth_param") or "Authorization"] = key
        elif auth == "query":
            params[self.cfg.get("auth_param") or "api_key"] = key
        elif auth == "basic":
            basic = base64.b64encode(key.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        elif auth == "oauth2":
            bearer = self._oauth_token()
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
        return headers, params

    def _oauth_token(self, refresh: bool = False) -> str | None:
        """An OAuth2 client-credentials access token (cached in memory)."""
        token_url = (self.cfg.get("oauth_token_url") or "").strip()
        client_id = (self.cfg.get("auth_param") or "").strip()
        secret = self._api_key() or ""
        if not token_url or not secret:
            return None
        cache_key = (self.id, token_url, client_id)
        now = time.monotonic()
        with _TOKENS_LOCK:
            cached = _TOKENS.get(cache_key)
            if cached and not refresh and cached[1] > now:
                return cached[0]
        form = {"grant_type": "client_credentials"}
        if client_id:
            form["client_id"] = client_id
        scope = (self.cfg.get("oauth_scope") or "").strip()
        if scope:
            form["scope"] = scope
        try:
            resp = safe_request(
                "POST", token_url, timeout=_TIMEOUT, data=form,
                auth=(client_id, secret) if client_id else None,
                headers={"Accept": "application/json"},
                verify=tls_verify_for(self.cfg.get("tls_mode")), max_bytes=256 * 1024,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            logger.warning("Custom source '%s': OAuth2 token request failed (%s)",
                           self.name, type(exc).__name__)
            self.last_error = "OAuth2 token request failed"
            return None
        token = str(body.get("access_token") or "") if isinstance(body, dict) else ""
        if not token:
            self.last_error = "OAuth2 token response had no access_token"
            return None
        ttl = _int(body.get("expires_in"), 3600)
        with _TOKENS_LOCK:
            _TOKENS[cache_key] = (token, now + max(30, ttl - 60))
        return token

    def _build_url(self, query: str | None) -> str:
        base = (self.cfg.get("base_url") or "").rstrip("/")
        endpoint = (self.cfg.get("endpoint") or "").strip()
        if "{query}" in endpoint and query is not None:
            endpoint = endpoint.replace("{query}", httpx.QueryParams({"q": query})["q"])
        if endpoint and not endpoint.startswith("/") and not endpoint.startswith("http"):
            endpoint = "/" + endpoint
        if endpoint.startswith("http"):
            return endpoint
        return f"{base}{endpoint}"

    def _body(self, query: str | None, params: dict) -> object | None:
        """The JSON body for a POST: the template (``{query}`` filled) or params."""
        template = (self.cfg.get("body_template") or "").strip()
        if not template:
            return params
        filled = template.replace("{query}", json.dumps(query or "")[1:-1])
        try:
            return json.loads(filled)
        except ValueError:
            logger.warning("Custom source '%s': the POST body template is not valid JSON",
                           self.name)
            return None

    def _first_page(self) -> _Page:
        page = _Page()
        mode = (self.cfg.get("pagination") or "none").lower()
        param = (self.cfg.get("page_param") or "").strip()
        size_param = (self.cfg.get("page_size_param") or "").strip()
        size = _int(self.cfg.get("page_size"), 0)
        if mode == "page" and param:
            page.params[param] = _int(self.cfg.get("page_start"), 1)
        elif mode == "offset" and param:
            page.params[param] = 0
        if mode in ("page", "offset", "cursor") and size_param and size > 0:
            page.params[size_param] = size
        return page

    def _request(self, query: str | None, page: _Page | None = None) -> object | None:
        """Fetch and parse one page. Returns the parsed structure or None."""
        page = page or self._first_page()
        url = page.url or self._build_url(query)
        if not url.lower().startswith(("http://", "https://")):
            logger.warning("Custom source '%s' has non-http URL; skipped.", self.name)
            return None
        headers = {"Accept": "application/json, application/xml;q=0.9, text/csv;q=0.8, "
                             "application/rss+xml;q=0.8, text/html;q=0.5, */*;q=0.3"}
        headers.update({str(k): str(v) for k, v in _json_obj(self.cfg.get("extra_headers")).items()})
        headers.update(self._secret_headers())
        auth_headers, auth_params = self._auth()
        headers.update(auth_headers)
        params: dict = {} if page.url else _json_obj(self.cfg.get("extra_params"))
        params.update(auth_params)
        qp = self.cfg.get("query_param")
        if qp and query is not None and not page.url:
            params[qp] = query
        params.update(page.params)
        method = (self.cfg.get("http_method") or "GET").upper()
        body = None
        if method == "POST":
            body = self._body(query, params)
            if body is None:
                return None
            if not (self.cfg.get("body_template") or "").strip():
                params = dict(auth_params)  # the params travel in the body
        try:
            resp = self._send(method, url, headers, params, body)
            if resp.status_code == 401 and self.auth_type == "oauth2":
                # The cached token may have been revoked early: refresh once.
                token = self._oauth_token(refresh=True)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    resp = self._send(method, url, headers, params, body)
            resp.raise_for_status()
        except ResponseTooLarge:
            self.last_error = "response too large"
            logger.warning("Custom source '%s': response too large", self.name)
            return None
        except ValueError as exc:  # refused by the SSRF guard
            self.last_error = f"blocked: {exc}"
            logger.warning("Custom source '%s' blocked unsafe URL: %s", self.name, exc)
            return None
        except Exception as exc:
            self.last_error = f"request failed: {type(exc).__name__}"
            logger.warning("Custom source '%s' request failed: %s", self.name,
                           type(exc).__name__)
            return None
        self._last_headers = resp.headers
        fmt = (self.cfg.get("response_format") or "auto").lower()
        if fmt not in api_formats.FORMATS or fmt == "auto":
            fmt = api_formats.detect_format(resp.headers.get("content-type"), resp.content)
        self.detected_format = fmt
        data = api_formats.parse_body(fmt, resp.content, str(resp.url))
        if data is None:
            self.last_error = f"could not read the {fmt} response"
        return data

    def _send(self, method: str, url: str, headers: dict, params: dict,
              body: object | None) -> httpx.Response:
        return safe_request(
            method, url, timeout=_TIMEOUT, headers=headers, params=params or None,
            json=body if method == "POST" else None,
            verify=tls_verify_for(self.cfg.get("tls_mode")), max_bytes=_MAX_BYTES,
        )

    def _next_page(self, page: _Page, data: object, current_url: str, got: int) -> _Page | None:
        """The next page to request, or None when pagination is done."""
        mode = (self.cfg.get("pagination") or "none").lower()
        max_pages = max(1, min(MAX_PAGES_CAP, _int(self.cfg.get("max_pages"), 1)))
        if mode == "none" or page.n + 1 >= max_pages or got == 0:
            return None
        param = (self.cfg.get("page_param") or "").strip()
        nxt = _Page(n=page.n + 1, params=dict(page.params))
        if mode == "page" and param:
            nxt.params[param] = _int(page.params.get(param), 1) + 1
            return nxt
        if mode == "offset" and param:
            nxt.params[param] = _int(page.params.get(param), 0) + got
            return nxt
        if mode == "cursor" and param:
            cursor = _dig(data, self.cfg.get("cursor_path"))
            if cursor in (None, "", False) or cursor == page.params.get(param):
                return None
            nxt.params[param] = _as_text(cursor)
            return nxt
        if mode == "next_link":
            path = (self.cfg.get("cursor_path") or "").strip()
            link = _dig(data, path) if path else _link_next(self._last_headers.get("link"))
            if not isinstance(link, str) or not link.strip():
                return None
            target = urljoin(current_url, link.strip())
            # Never follow a next link off the API host (credentials ride along).
            a, b = urlsplit(target), urlsplit(current_url)
            if (a.scheme.lower(), (a.netloc or "").lower()) != (b.scheme.lower(),
                                                                 (b.netloc or "").lower()):
                logger.warning("Custom source '%s': next link leaves the API host; stopped",
                               self.name)
                return None
            if target == current_url:
                return None
            return _Page(n=page.n + 1, url=target)
        return None

    # -------------------------------------------------------------------- parse
    def _field(self, raw: dict, path: str | None):
        """Dig a *mapped* field. An empty/unset mapping yields None — never the
        whole item (that is only meaningful for items_path, not field mapping)."""
        if not path:
            return None
        return _dig(raw, path)

    def _has_mapping(self) -> bool:
        return any((self.cfg.get(k) or "").strip() for k in ("map_content", "map_title"))

    def _map_item(self, raw, query: str | None) -> RawItem | None:
        if not self._has_mapping():
            return self._fallback_item(raw, query)
        if not isinstance(raw, dict):
            return None
        content = _as_text(self._field(raw, self.cfg.get("map_content")))
        title = _as_text(self._field(raw, self.cfg.get("map_title"))) or None
        # An item needs *some* text to be worth storing.
        if not content and not title:
            return None
        url = _as_text(self._field(raw, self.cfg.get("map_url"))) or None
        author = _as_text(self._field(raw, self.cfg.get("map_author"))) or None
        published = _parse_date(self._field(raw, self.cfg.get("map_published")))
        # Stable-ish external id: prefer the url, else a hash of the content.
        external_id = url or None
        return RawItem(
            source=self.name,
            track="api",
            external_id=external_id,
            url=url,
            author=author,
            title=title,
            content=content or (title or ""),
            published_at=published,
            raw={"query": query, "item": raw, "custom_source_id": self.id},
        )

    def _fallback_item(self, raw, query: str | None) -> RawItem | None:
        """No field mapping: turn the whole record into readable text."""
        if isinstance(raw, dict):
            content = flatten_to_text(raw)
            title = guess_title(raw)
            url_val = _first_scalar(raw, _URL_KEYS)
            url = str(url_val) if isinstance(url_val, str) and url_val.startswith(
                ("http://", "https://")) else None
            author_val = _first_scalar(raw, _AUTHOR_KEYS)
            author = str(author_val)[:200] if author_val is not None else None
            published = _parse_date(_first_scalar(raw, _DATE_KEYS))
        else:
            content = _as_text(raw)
            title, url, author, published = None, None, None, None
        if not content.strip():
            return None
        return RawItem(
            source=self.name, track="api", external_id=url, url=url, author=author,
            title=title, content=content, published_at=published,
            raw={"query": query, "item": raw, "custom_source_id": self.id,
                 "flattened": True},
        )

    def _extract_list(self, data) -> list:
        if self.detected_format in api_formats.RECORD_FORMATS and isinstance(data, list):
            return data
        path = self.cfg.get("items_path")
        if self.detected_format == "xml" and not (path or "").strip():
            found = api_formats.xml_records(data)
            if found is not None:
                return found
        items = _dig(data, path)
        if isinstance(items, list):
            return items
        # Tolerate an API that returns a single object instead of a list.
        if isinstance(items, dict):
            return [items]
        return []

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        qp = self.cfg.get("query_param")
        template = self.cfg.get("body_template") or ""
        has_query_template = qp or ("{query}" in (self.cfg.get("endpoint") or "")) or (
            "{query}" in template)
        queries: list[str | None]
        if has_query_template:
            queries = list(self.settings.investigation_query_targets()) or []
        else:
            queries = [None]

        for query in queries:
            page: _Page | None = None
            data = self._request(query)
            per_query = 0
            while data is not None:
                # The API answered: remember that, and keep the most recent
                # parsed response so Auto-Adapt can re-map fields on drift.
                self.responded = True
                self.last_sample = api_formats.to_sample(data)
                all_records = self._extract_list(data)
                for raw in all_records[:_MAX_ITEMS]:
                    item = self._map_item(raw, query)
                    if item is None:
                        continue
                    if since and item.published_at and item.published_at <= since:
                        continue
                    items.append(item)
                    per_query += 1
                if per_query >= _MAX_ITEMS_PER_QUERY:
                    break
                current = page or self._first_page()
                current_url = current.url or self._build_url(query)
                page = self._next_page(current, data, current_url, len(all_records))
                if page is None:
                    break
                data = self._request(query, page)

        logger.info("Custom source '%s' fetched %d items", self.name, len(items))
        return items


def load_custom_sources(settings: Settings | None = None) -> list[CustomApiSource]:
    """Build a CustomApiSource for every enabled row in custom_sources.

    Read fresh each scan so sources added from the dashboard take effect without
    a restart. Never raises — a DB hiccup yields an empty list.
    """
    from nexus.db import get_connection
    from nexus.storage import list_custom_sources

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_custom_sources(conn, enabled_only=True)
    except Exception:
        logger.exception("Could not load custom sources")
        return []
    return [CustomApiSource(row, settings) for row in rows]
