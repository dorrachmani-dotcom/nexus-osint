"""Generic, config-driven source for user-defined JSON APIs.

This is the engine behind the dashboard's "add a new API source" feature. An
analyst (optionally helped by the AI planner) describes any JSON REST API once —
its base URL, how it authenticates, which field is the post text, etc. — and this
class turns that config into a normal `Source` the collector scans like any
built-in one.

Design rules it inherits from the rest of the project:
  * Graceful degradation: a missing key, an unreachable host, or an unexpected
    JSON shape logs a warning and yields fewer/no items — it never crashes a scan.
  * OpSec: the API key is read from .env (CUSTOM_SOURCE_<id>_KEY), never the DB.
  * Untrusted config: requests only go to http(s); response parsing tolerates
    drift via dotted-path field mapping with safe fallbacks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from nexus.config import Settings, get_settings
from nexus.envstore import custom_source_env_name, read_env_value
from nexus.models import RawItem
from nexus.netguard import safe_http_url
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.custom")

_TIMEOUT = 30.0
_MAX_ITEMS = 100


def _parse_date(value) -> datetime | None:
    """Best-effort date parse: unix epoch, ISO 8601, or give up (None)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    # Numeric string -> epoch seconds.
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
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
        #   last_sample — the latest raw response JSON (for AI re-mapping on drift).
        self.responded = False
        self.last_sample: str | None = None

    # ------------------------------------------------------------- availability
    def _api_key(self) -> str | None:
        return read_env_value(custom_source_env_name(self.id))

    def is_available(self) -> bool:
        if not self.cfg.get("enabled", 1):
            return False
        if not (self.cfg.get("base_url") or "").strip():
            return False
        auth = (self.cfg.get("auth_type") or "none").lower()
        if auth in ("header", "query", "bearer") and not self._api_key():
            return False
        return True

    # ------------------------------------------------------------------ request
    def _auth(self) -> tuple[dict, dict]:
        """Return (extra_headers, extra_params) implementing the auth scheme."""
        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        auth = (self.cfg.get("auth_type") or "none").lower()
        key = self._api_key() or ""
        if not key:
            return headers, params
        if auth == "bearer":
            headers[self.cfg.get("auth_param") or "Authorization"] = f"Bearer {key}"
        elif auth == "header":
            headers[self.cfg.get("auth_param") or "Authorization"] = key
        elif auth == "query":
            params[self.cfg.get("auth_param") or "api_key"] = key
        return headers, params

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

    def _request(self, query: str | None) -> object | None:
        url = self._build_url(query)
        if not url.lower().startswith(("http://", "https://")):
            logger.warning("Custom source '%s' has non-http URL; skipped.", self.name)
            return None
        # SSRF guard: the base_url/endpoint come from a user (or the AI planner),
        # so refuse internal/loopback/link-local targets before we make the call.
        safe, reason = safe_http_url(url)
        if not safe:
            logger.warning("Custom source '%s' blocked unsafe URL: %s", self.name, reason)
            return None
        headers, auth_params = self._auth()
        try:
            params = json.loads(self.cfg.get("extra_params") or "{}")
            if not isinstance(params, dict):
                params = {}
        except (ValueError, TypeError):
            params = {}
        params.update(auth_params)
        qp = self.cfg.get("query_param")
        if qp and query is not None:
            params[qp] = query
        method = (self.cfg.get("http_method") or "GET").upper()
        try:
            # follow_redirects=False: a redirect could bounce a vetted public URL
            # to an internal address, sidestepping the SSRF guard above.
            if method == "POST":
                resp = httpx.post(
                    url, headers=headers, json=params, timeout=_TIMEOUT, follow_redirects=False
                )
            else:
                resp = httpx.get(
                    url, headers=headers, params=params, timeout=_TIMEOUT, follow_redirects=False
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Custom source '%s' request failed: %s", self.name, exc)
            return None

    # -------------------------------------------------------------------- parse
    def _field(self, raw: dict, path: str | None):
        """Dig a *mapped* field. An empty/unset mapping yields None — never the
        whole item (that is only meaningful for items_path, not field mapping)."""
        if not path:
            return None
        return _dig(raw, path)

    def _map_item(self, raw: dict, query: str | None) -> RawItem | None:
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

    def _extract_list(self, data) -> list:
        items = _dig(data, self.cfg.get("items_path"))
        if isinstance(items, list):
            return items
        # Tolerate an API that returns a single object instead of a list.
        if isinstance(items, dict):
            return [items]
        return []

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        qp = self.cfg.get("query_param")
        has_query_template = qp or ("{query}" in (self.cfg.get("endpoint") or ""))
        queries: list[str | None]
        if has_query_template:
            queries = list(self.settings.investigation_query_targets()) or []
        else:
            queries = [None]

        for query in queries:
            data = self._request(query)
            if data is None:
                continue
            # The API answered: remember that, and keep the most recent raw
            # response so Auto-Adapt can re-map the fields if mapping drifts.
            self.responded = True
            try:
                self.last_sample = json.dumps(data, ensure_ascii=False)[:8000]
            except (TypeError, ValueError):
                self.last_sample = None
            for raw in self._extract_list(data)[:_MAX_ITEMS]:
                item = self._map_item(raw, query)
                if item is None:
                    continue
                if since and item.published_at and item.published_at <= since:
                    continue
                items.append(item)

        logger.info("Custom source '%s' fetched %d items", self.name, len(items))
        return items


def load_custom_sources(settings: Settings | None = None) -> list[CustomApiSource]:
    """Build a CustomApiSource for every enabled row in custom_sources.

    Read fresh each scan so sources added from the dashboard take effect without
    a restart. Never raises — a DB hiccup yields an empty list.
    """
    from nexus.storage import list_custom_sources
    from nexus.db import get_connection

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_custom_sources(conn, enabled_only=True)
    except Exception:
        logger.exception("Could not load custom sources")
        return []
    return [CustomApiSource(row, settings) for row in rows]
