"""Read-only cloud connectors: the part they all share.

A connector subclasses :class:`CloudSource` and supplies its API calls: how to
list what is in scope (:meth:`CloudSource.listing`), how to read one entry into
a ``ConnectorDoc`` (``Entry.load``) and how to test a connection. Everything
else lives here, once:

  * every request goes through the SSRF-guarded ``safe_request``, only over
    https, only to the connector's own fixed hosts, and only with GET (or a
    read-only POST such as a search): nothing here writes to the remote service;
  * rate limits (429 / 503 with ``Retry-After``) are waited out, responses are
    size-limited and each scan has a request budget;
  * only new and changed entries are read, up to ``max_items`` per scan, and
    stored through the same pipeline as Confluence (case / pool / no-AI target);
  * the token stays in ``.env`` (``CLOUD_SOURCE_<id>_TOKEN``) and never reaches
    the database, the status line or the logs;
  * one connection failing never breaks a scan.

To add a connector, write the subclass and add it to ``_CONNECTORS``.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from nexus.config import Settings, get_settings
from nexus.envstore import cloud_env_name, read_env_value
from nexus.ingest.connector import (
    ConnectorDoc,
    Target,
    known_keys,
    remove_missing,
    sync_document,
)
from nexus.netguard import ResponseTooLarge, safe_request
from nexus.sources.base import RawItem, Source
from nexus.sources.confluence import retry_after_seconds

logger = logging.getLogger("nexus.sources.cloud")

_TIMEOUT = 30.0
_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_MAX_RETRIES = 3
_MAX_REQUESTS = 20_000  # request budget per scan

# kind -> "module:class". Imported on demand.
_CONNECTORS = {
    "notion": "nexus.sources.notion:NotionSource",
    "slack": "nexus.sources.slack:SlackSource",
}


class CloudError(Exception):
    """A readable problem; the message is shown to the analyst (never a secret)."""


class CloudAuthError(CloudError):
    """The token was refused: stop this connection's scan."""


@dataclass
class Entry:
    """One thing in scope, as listed. ``load`` does the expensive read."""

    key: str                                 # stable id within this connection
    version: str | None                      # changes when the content changes
    modified: datetime | None
    load: Callable[[], ConnectorDoc]


def parse_when(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


class CloudSource(Source):
    """Shared behaviour; see the module docstring."""

    KIND = ""
    LABEL = ""
    HOSTS: tuple[str, ...] = ()
    INTRO = ""               # one line on the add form: what it reads
    KEY_LABEL = "Token"
    KEY_HINT = ""
    ACCOUNT_LABEL = ""       # empty: this connector has no account field
    ACCOUNT_HINT = ""
    SCOPE_NOUN = ""          # e.g. "channels"; empty: no scope to choose
    NEEDS_SCOPE = False      # a scan needs at least one chosen item
    FULL_LISTING = False     # listing() shows everything in scope, so what is
                             # missing from it was deleted at the source

    def __init__(self, cfg: dict, settings: Settings | None = None, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg
        self.id = int(cfg["id"])
        self.name = (cfg.get("name") or f"{self.KIND}{self.id}").strip() or f"{self.KIND}{self.id}"
        self._sleep = sleep
        self.status = ""
        self._requests = 0

    # ---------------------------------------------------------- config
    def token(self) -> str | None:
        return read_env_value(cloud_env_name(self.id)) or None

    def scope(self) -> list[str]:
        return [s for s in str(self.cfg.get("scope") or "").split(",") if s]

    def is_available(self) -> bool:
        return bool(self.token()) and (bool(self.scope()) or not self.NEEDS_SCOPE)

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        return []  # documents are stored by ingest(), which the collector calls

    # ------------------------------------------------------- to implement
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token() or ''}"}

    def listing(self, since: datetime | None) -> Iterator[Entry]:
        raise NotImplementedError

    def test_connection(self) -> dict:
        """``{"ok": True, "account": str, "items": [{"key","name"}], "note": str}``."""
        raise NotImplementedError

    # ------------------------------------------------------------- HTTP
    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme.lower() == "https" and (parts.hostname or "").lower() in self.HOSTS

    def request(self, method: str, url: str, *, params: dict | None = None,
                json: object | None = None, data: dict | None = None,
                headers: dict | None = None, max_bytes: int = _MAX_RESPONSE_BYTES,
                auth: bool = True) -> httpx.Response:
        if not self._allowed(url):
            raise CloudError("refused a link to another host")
        self._requests += 1
        if self._requests > _MAX_REQUESTS:
            raise CloudError("stopped at the request limit for one scan")
        sent = {"Accept": "application/json",
                "User-Agent": f"Nexus-OSINT {self.LABEL} connector",
                **(self.headers() if auth else {}), **(headers or {})}
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = safe_request(method, url, timeout=_TIMEOUT, headers=sent, params=params,
                                    json=json, data=data, max_bytes=max_bytes)
            except ResponseTooLarge as exc:
                raise CloudError("a response was too large") from exc
            except ValueError as exc:  # refused by the SSRF guard
                raise CloudError(f"blocked: {exc}") from exc
            except httpx.HTTPError as exc:
                raise CloudError(f"could not connect ({type(exc).__name__})") from exc
            if resp.status_code in (429, 503) and attempt < _MAX_RETRIES:
                wait = retry_after_seconds(resp.headers.get("retry-after"), attempt)
                logger.info("%s '%s' is rate-limited; waiting %.0fs", self.LABEL, self.name, wait)
                self._sleep(wait)
                continue
            if resp.status_code == 401:
                raise CloudAuthError("HTTP 401: the token was refused")
            if resp.status_code == 403:
                raise CloudError("HTTP 403: this account may not read that")
            if resp.status_code >= 400:
                raise CloudError(f"HTTP {resp.status_code}")
            return resp
        raise CloudError("still rate-limited after several retries")

    def json(self, method: str, url: str, **kwargs: Any) -> dict:
        resp = self.request(method, url, **kwargs)
        try:
            data = resp.json()
        except ValueError as exc:
            raise CloudError("the service did not answer with JSON") from exc
        if not isinstance(data, dict):
            raise CloudError("unexpected response shape")
        return data

    # ------------------------------------------------------------- ingest
    def ingest(self) -> dict:
        """Read new and changed entries and store them. Never raises."""
        from nexus.db import get_connection
        from nexus.storage.cloud_sources import save_cloud_scan

        since = parse_when(self.cfg.get("watermark"))
        target = Target.from_row(self.cfg)
        max_items = int(self.cfg.get("max_items") or 200)
        checked = stored = changed = new_items = skipped = removed = 0
        newest: datetime | None = None
        listed_all = True
        present: set[str] = set()
        try:
            with get_connection() as conn:
                known = known_keys(conn, self.KIND, self.id)
            for entry in self.listing(since):
                checked += 1
                present.add(entry.key)
                prev = known.get(entry.key)
                if (prev and entry.version and prev.get("version") == entry.version
                        and prev.get("document_id") is not None):
                    newest = max(filter(None, [newest, entry.modified]), default=None)
                    continue  # same version as stored: nothing to read
                if stored >= max_items:
                    listed_all = False
                    break
                try:
                    doc = entry.load()
                except CloudAuthError:
                    raise
                except CloudError as exc:
                    logger.info("%s '%s': skipped one entry: %s", self.LABEL, self.name, exc)
                    skipped += 1
                    continue
                with get_connection() as conn:
                    res = sync_document(conn, kind=self.KIND, source_id=self.id,
                                        source_name=self.name, track=self.KIND,
                                        doc=doc, target=target)
                    if res["change"] == "same":
                        conn.execute(
                            "UPDATE connector_docs SET version = ? WHERE kind = ? AND "
                            "source_id = ? AND doc_key = ?",
                            (doc.version, self.KIND, self.id, doc.key))
                stored += res["change"] != "same"
                changed += res["change"] == "changed"
                new_items += int(res.get("new") or 0)
                newest = max(filter(None, [newest, entry.modified]), default=None)
            # An empty listing is more likely lost access than everything deleted.
            if self.FULL_LISTING and listed_all and present:
                with get_connection() as conn:
                    removed = remove_missing(conn, self.KIND, self.id, present, self.name)
        except CloudError as exc:
            self.status = f"error: {exc}"
            listed_all = False
        except Exception as exc:  # never let one connection break a scan
            logger.exception("%s '%s' failed", self.LABEL, self.name)
            self.status = f"error: {type(exc).__name__}"
            listed_all = False
        if not self.status:
            parts = [f"{checked} checked, {stored - changed} new, {changed} updated"]
            if removed:
                parts.append(f"{removed} removed")
            if skipped:
                parts.append(f"{skipped} could not be read")
            self.status = ", ".join(parts) + ("" if listed_all else f" (stopped at {max_items}; more next scan)")
        # Listings may be newest-first: only a complete run may move the watermark.
        complete = listed_all and not skipped
        try:
            with get_connection() as conn:
                save_cloud_scan(conn, self.id, self.status,
                                newest.isoformat() if newest and complete else None)
        except Exception:
            logger.exception("Could not save the scan state for '%s'", self.name)
        logger.info("%s '%s': %s", self.LABEL, self.name, self.status)
        return {"fetched": checked, "new": new_items, "changed": changed, "removed": removed,
                "status": self.status}


OVERLAP = timedelta(hours=1)  # services round their edit times; versions drop repeats


def connector_class(kind: str) -> type[CloudSource] | None:
    target = _CONNECTORS.get(kind)
    if not target:
        return None
    module, name = target.split(":")
    return getattr(importlib.import_module(module), name)


def connector_classes() -> list[type[CloudSource]]:
    return [cls for cls in map(connector_class, _CONNECTORS) if cls is not None]


def make_source(row: dict, settings: Settings | None = None) -> CloudSource | None:
    cls = connector_class(str(row.get("kind") or ""))
    return cls(row, settings) if cls else None


def load_cloud_sources(settings: Settings | None = None) -> list[CloudSource]:
    """A source for every enabled connection. Never raises."""
    from nexus.db import get_connection
    from nexus.storage.cloud_sources import list_cloud_sources

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_cloud_sources(conn, enabled_only=True)
        return [s for s in (make_source(r, settings) for r in rows) if s is not None]
    except Exception:
        logger.exception("Could not load cloud connections")
        return []
