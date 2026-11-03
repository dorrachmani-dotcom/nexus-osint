"""Confluence connector: pull pages (and optionally blog posts, comments and
attachments) from Confluence Cloud or Data Center / Server into a case or the
knowledge pool.

Authentication
  * Cloud: site URL + account e-mail + API token → HTTP Basic.
  * Data Center / Server: base URL (with any context path) + Personal Access
    Token → ``Authorization: Bearer``.
  The token is read from ``.env`` (``CONFLUENCE_SOURCE_<id>_TOKEN``) only; it is
  never stored in the database, rendered in the UI or written to a log.

APIs
  * Cloud with chosen spaces: REST v2 (``/wiki/api/v2``) — spaces by key, then
    each space's pages / blog posts sorted by last modification, cursor-paged
    through ``_links.next``; labels, ancestors (breadcrumb), footer comments
    and attachments from their v2 endpoints.
  * Data Center / Server, or a CQL query on either: REST v1
    ``/rest/api/content/search`` with ``expand=body.storage,version,space,
    ancestors,metadata.labels,history``, paged with ``_links.next`` (or
    ``start``/``limit``).
  HTTP 429 / 503 are retried after ``Retry-After`` (capped).

Network
  Every request goes through :func:`nexus.netguard.safe_request`: an internal
  host is refused unless it is on the operator's internal allow-list, every
  redirect hop is re-checked, credentials are dropped if a redirect leaves the
  host, and the per-connection TLS mode (internal CA / opt-in no verification)
  is honoured. ``_links.next`` may never point at another host.

Incremental
  A per-connection watermark (the newest ``lastModified`` stored) narrows the
  next query; the stored version number short-cuts unchanged pages, and the
  content hash decides between "new", "changed" (fresh text + a change-summary
  item, like watched web pages) and "same".
"""

from __future__ import annotations

import base64
import html
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from nexus.config import Settings, get_settings
from nexus.envstore import confluence_env_name, read_env_value
from nexus.ingest.connector import ConnectorDoc, Target, known_keys, sync_document
from nexus.ingest.html_text import extract_html
from nexus.ingest.safety import DEFAULT_LIMITS
from nexus.models import RawItem
from nexus.netguard import ResponseTooLarge, safe_request, tls_verify_for
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.confluence")

KIND = "confluence"
_TIMEOUT = 30.0
_PAGE_BYTES = 20 * 1024 * 1024
_MAX_RETRIES = 3
_MAX_RETRY_WAIT = 60.0
_MAX_LIST_REQUESTS = 5000  # request budget per scan
_MAX_ATTACHMENTS = 20
_OVERLAP = timedelta(days=1)  # CQL dates are minute-precision in the user's zone


class ConfluenceError(Exception):
    """A request failed in a way worth showing to the analyst (no secrets)."""


# ------------------------------------------------------------- helpers
def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def retry_after_seconds(value: str | None, attempt: int) -> float:
    """Seconds to wait for a ``Retry-After`` header (delta or HTTP date)."""
    if value:
        value = value.strip()
        if value.isdigit():
            return min(float(value), _MAX_RETRY_WAIT)
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, min((when - datetime.now(UTC)).total_seconds(), _MAX_RETRY_WAIT))
        except (TypeError, ValueError, IndexError):
            pass
    return min(2.0 ** attempt, _MAX_RETRY_WAIT)


# Bounded repetitions: page bodies are untrusted, so no unbounded backtracking.
_CDATA = re.compile(r"<!\[CDATA\[(.{0,200000}?)\]\]>", re.S)
_RI_TITLE = re.compile(
    r"<ri:(?:page|blog-post)\b[^>]{0,500}?ri:content-title=\"([^\"]{0,500})\"[^>]{0,500}>", re.I)
_RI_ATTACH = re.compile(
    r"<ri:attachment\b[^>]{0,500}?ri:filename=\"([^\"]{0,500})\"[^>]{0,500}>", re.I)
_RI_USER = re.compile(r"<ri:user\b[^>]{0,500}>", re.I)
_AC_PARAM = re.compile(r"<ac:parameter\b[^>]{0,500}>[^<]{0,2000}</ac:parameter>", re.I)


def storage_to_text(storage: str) -> str:
    """Confluence storage format (XHTML + ``ac:``/``ri:`` macros) → clean text.

    Code-block bodies (CDATA) are kept as text, page/attachment links become
    their titles, user mentions become ``@user``, macro parameters are dropped;
    the rest goes through the shared HTML text extractor.
    """
    if not storage:
        return ""
    s = _CDATA.sub(lambda m: html.escape(m.group(1)), storage)
    s = _AC_PARAM.sub(" ", s)
    s = _RI_TITLE.sub(lambda m: f" {m.group(1)} ", s)
    s = _RI_ATTACH.sub(lambda m: f" [{m.group(1)}] ", s)
    s = _RI_USER.sub(" @user ", s)
    doc = extract_html(f"<html><body>{s}</body></html>")
    return doc.text.strip()


# ------------------------------------------------------------- the source
class ConfluenceSource(Source):
    """One Confluence connection (a ``confluence_sources`` row)."""

    def __init__(self, cfg: dict, settings: Settings | None = None, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg
        self.id = int(cfg["id"])
        self.name = (cfg.get("name") or f"confluence{self.id}").strip() or f"confluence{self.id}"
        self._sleep = sleep
        self.status = ""
        self._title_cache: dict[str, str] = {}
        self._user_cache: dict[str, str | None] = {}
        self._requests = 0

    # ---------------------------------------------------------- config
    @property
    def cloud(self) -> bool:
        return (self.cfg.get("deployment") or "cloud") == "cloud"

    @property
    def base(self) -> str:
        return (self.cfg.get("base_url") or "").strip().rstrip("/")

    @property
    def web_base(self) -> str:
        return f"{self.base}/wiki" if self.cloud else self.base

    @property
    def v1(self) -> str:
        return f"{self.web_base}/rest/api"

    @property
    def v2(self) -> str:
        return f"{self.base}/wiki/api/v2"

    def _token(self) -> str | None:
        return read_env_value(confluence_env_name(self.id))

    def spaces(self) -> list[str]:
        return [s for s in (self.cfg.get("spaces") or "").split(",") if s.strip()]

    def is_available(self) -> bool:
        if not self.cfg.get("enabled", 1):
            return False
        if not self.base.lower().startswith(("http://", "https://")):
            return False
        if not self._token():
            return False
        if self.cloud and not (self.cfg.get("email") or "").strip():
            return False
        return bool(self.spaces() or (self.cfg.get("cql") or "").strip())

    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        # Documents are stored by ingest() (the collector calls it instead).
        return []

    # ------------------------------------------------------------- HTTP
    def _headers(self) -> dict[str, str]:
        token = self._token() or ""
        headers = {"Accept": "application/json",
                   "User-Agent": "Nexus-OSINT Confluence connector"}
        if self.cloud:
            raw = f"{(self.cfg.get('email') or '').strip()}:{token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _same_host(self, url: str) -> bool:
        a, b = urlsplit(url), urlsplit(self.base)
        return (a.scheme.lower(), (a.netloc or "").lower()) == (b.scheme.lower(),
                                                                (b.netloc or "").lower())

    def _get(self, url: str, params: dict | None = None, *,
             max_bytes: int = _PAGE_BYTES) -> httpx.Response:
        if not self._same_host(url):
            raise ConfluenceError("refused a link to another host")
        self._requests += 1
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = safe_request(
                    "GET", url, timeout=_TIMEOUT, headers=self._headers(), params=params,
                    verify=tls_verify_for(self.cfg.get("tls_mode")), max_bytes=max_bytes,
                )
            except ResponseTooLarge as exc:
                raise ConfluenceError("a response was too large") from exc
            except ValueError as exc:  # refused by the SSRF guard
                raise ConfluenceError(f"blocked: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ConfluenceError(f"could not connect ({type(exc).__name__})") from exc
            if resp.status_code in (429, 503) and attempt < _MAX_RETRIES:
                wait = retry_after_seconds(resp.headers.get("retry-after"), attempt)
                logger.info("Confluence '%s' is rate-limited; waiting %.0fs", self.name, wait)
                self._sleep(wait)
                continue
            if resp.status_code in (401, 403):
                raise ConfluenceError(
                    f"HTTP {resp.status_code}: check the e-mail and token and that the "
                    "account can read these spaces")
            if resp.status_code == 404:
                raise ConfluenceError("HTTP 404: check the site / base URL")
            if resp.status_code >= 400:
                raise ConfluenceError(f"HTTP {resp.status_code}")
            return resp
        raise ConfluenceError("still rate-limited after several retries")

    def _json(self, url: str, params: dict | None = None) -> dict:
        resp = self._get(url, params)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ConfluenceError("the server did not answer with JSON "
                                  "(is this the right URL?)") from exc
        if not isinstance(data, dict):
            raise ConfluenceError("unexpected response shape")
        return data

    def _next_url(self, data: dict, v2: bool) -> str | None:
        links = data.get("_links") or {}
        nxt = links.get("next")
        if not isinstance(nxt, str) or not nxt:
            return None
        if nxt.startswith(("http://", "https://")):
            url = nxt
        elif v2:
            url = urljoin(self.base + "/", nxt.lstrip("/"))
        else:
            raw_base = links.get("base")
            base = raw_base if isinstance(raw_base, str) else self.web_base
            url = base.rstrip("/") + "/" + nxt.lstrip("/")
        return url if self._same_host(url) else None

    def _paged(self, url: str, params: dict | None, *, v2: bool):
        """Yield result objects across pages (cursor / next link / start)."""
        start = 0
        cur_url: str | None = url
        cur_params = dict(params or {})
        while cur_url and self._requests < _MAX_LIST_REQUESTS:
            data = self._json(cur_url, cur_params or None)
            results = data.get("results") or []
            yield from (r for r in results if isinstance(r, dict))
            nxt = self._next_url(data, v2)
            if nxt:
                cur_url, cur_params = nxt, {}
                continue
            if v2 or not results:
                return
            size, limit = int(data.get("size") or len(results)), int(data.get("limit") or 0)
            if limit and size >= limit:
                start = int(data.get("start") or start) + size
                cur_params = {**(params or {}), "start": start}
                continue
            return

    # ----------------------------------------------------------- spaces
    def list_spaces(self, limit: int = 250) -> list[dict]:
        """``[{key, name}]`` of the spaces this account can read."""
        out: list[dict] = []
        if self.cloud:
            it = self._paged(f"{self.v2}/spaces", {"limit": 250}, v2=True)
        else:
            it = self._paged(f"{self.v1}/space", {"limit": 100}, v2=False)
        for sp in it:
            key = sp.get("key")
            if key:
                out.append({"key": str(key), "name": str(sp.get("name") or key)})
            if len(out) >= limit:
                break
        return out

    def test_connection(self) -> dict:
        try:
            spaces = self.list_spaces()
        except ConfluenceError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "spaces": spaces}

    # ----------------------------------------------------------- reading
    def _user_name(self, account_id: str | None) -> str | None:
        if not account_id:
            return None
        if account_id not in self._user_cache:
            name = None
            try:
                data = self._json(f"{self.v1}/user", {"accountId": account_id})
                name = data.get("displayName") or data.get("publicName")
            except ConfluenceError:
                pass
            self._user_cache[account_id] = name
        return self._user_cache[account_id]

    def _v2_title(self, page_id: str) -> str:
        if page_id not in self._title_cache:
            try:
                self._title_cache[page_id] = str(
                    self._json(f"{self.v2}/pages/{page_id}").get("title") or page_id)
            except ConfluenceError:
                self._title_cache[page_id] = page_id
        return self._title_cache[page_id]

    def _comments_text(self, content_id: str, ctype: str) -> str:
        texts: list[str] = []
        try:
            if self.cloud and ctype in ("pages", "blogposts"):
                it = self._paged(f"{self.v2}/{ctype}/{content_id}/footer-comments",
                                 {"body-format": "storage", "limit": 50}, v2=True)
                for c in it:
                    body = ((c.get("body") or {}).get("storage") or {}).get("value") or ""
                    texts.append(storage_to_text(body))
            else:
                it = self._paged(f"{self.v1}/content/{content_id}/child/comment",
                                 {"expand": "body.storage,history", "limit": 50,
                                  "depth": "all"}, v2=False)
                for c in it:
                    body = ((c.get("body") or {}).get("storage") or {}).get("value") or ""
                    who = ((c.get("history") or {}).get("createdBy") or {}).get("displayName")
                    text = storage_to_text(body)
                    texts.append(f"{who}: {text}" if who else text)
        except ConfluenceError as exc:
            logger.info("Confluence '%s': comments unavailable (%s)", self.name, exc)
        texts = [t for t in texts if t]
        return ("Comments:\n" + "\n".join(f"- {t}" for t in texts)) if texts else ""

    def _attachments(self, content_id: str, ctype: str) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        limit = DEFAULT_LIMITS.max_file_bytes
        try:
            if self.cloud and ctype in ("pages", "blogposts"):
                it = self._paged(f"{self.v2}/{ctype}/{content_id}/attachments",
                                 {"limit": 50}, v2=True)
                rows = [(a.get("title"), a.get("downloadLink"), a.get("fileSize")) for a in it]
                prefix = self.web_base
            else:
                it = self._paged(f"{self.v1}/content/{content_id}/child/attachment",
                                 {"limit": 50}, v2=False)
                rows = [(a.get("title"), (a.get("_links") or {}).get("download"),
                         (a.get("extensions") or {}).get("fileSize")) for a in it]
                prefix = self.web_base
            for title, link, size in rows[:_MAX_ATTACHMENTS]:
                if not title or not isinstance(link, str):
                    continue
                if isinstance(size, int) and size > limit:
                    continue
                url = link if link.startswith("http") else prefix + "/" + link.lstrip("/")
                try:
                    resp = self._get(url, max_bytes=limit)
                except ConfluenceError as exc:
                    logger.info("Confluence '%s': attachment skipped (%s)", self.name, exc)
                    continue
                out.append((str(title), resp.content))
        except ConfluenceError as exc:
            logger.info("Confluence '%s': attachments unavailable (%s)", self.name, exc)
        return out

    def _extras(self, doc: ConnectorDoc, content_id: str, ctype: str) -> None:
        extra = []
        if self.cfg.get("include_comments"):
            comments = self._comments_text(content_id, ctype)
            if comments:
                extra.append(comments)
        if extra:
            doc.text = "\n\n".join([doc.text, *extra]).strip()
        if self.cfg.get("include_attachments"):
            doc.attachments = self._attachments(content_id, ctype)

    def _doc_v1(self, c: dict) -> ConnectorDoc:
        cid = str(c.get("id"))
        body = ((c.get("body") or {}).get("storage") or {}).get("value") or ""
        version = c.get("version") or {}
        space = c.get("space") or {}
        history = c.get("history") or {}
        labels = [str(lab.get("name")) for lab in
                  ((c.get("metadata") or {}).get("labels") or {}).get("results") or []
                  if lab.get("name")]
        ancestors = [str(a.get("title")) for a in c.get("ancestors") or [] if a.get("title")]
        webui = (c.get("_links") or {}).get("webui") or ""
        author = (history.get("createdBy") or {}).get("displayName") or (
            version.get("by") or {}).get("displayName")
        ctype = str(c.get("type") or "page")
        return ConnectorDoc(
            key=cid, title=str(c.get("title") or cid), text=storage_to_text(body),
            url=(self.web_base + webui) if webui else None, author=author,
            modified=_parse_when(version.get("when")),
            version=str(version.get("number") or ""),
            meta={"space": space.get("key"), "space_name": space.get("name"),
                  "labels": labels, "breadcrumb": ancestors, "content_type": ctype,
                  "last_modified_by": (version.get("by") or {}).get("displayName")},
        )

    def _doc_v2(self, c: dict, space: dict, ctype: str) -> ConnectorDoc:
        cid = str(c.get("id"))
        body = ((c.get("body") or {}).get("storage") or {}).get("value") or ""
        version = c.get("version") or {}
        webui = (c.get("_links") or {}).get("webui") or ""
        return ConnectorDoc(
            key=cid, title=str(c.get("title") or cid), text=storage_to_text(body),
            url=(self.web_base + webui) if webui else None,
            author=self._user_name(c.get("authorId") or version.get("authorId")),
            modified=_parse_when(version.get("createdAt")),
            version=str(version.get("number") or ""),
            meta={"space": space.get("key"), "space_name": space.get("name"),
                  "content_type": "page" if ctype == "pages" else "blogpost"},
        )

    def _v2_meta(self, doc: ConnectorDoc, ctype: str) -> None:
        try:
            doc.meta["labels"] = [str(lab.get("name")) for lab in self._paged(
                f"{self.v2}/{ctype}/{doc.key}/labels", {"limit": 100}, v2=True)
                if lab.get("name")]
        except ConfluenceError:
            doc.meta["labels"] = []
        crumbs: list[str] = []
        if ctype == "pages":
            try:
                ids = [str(a.get("id")) for a in self._paged(
                    f"{self.v2}/pages/{doc.key}/ancestors", {"limit": 50}, v2=True)
                    if a.get("id")]
                crumbs = [self._v2_title(i) for i in ids]
            except ConfluenceError:
                crumbs = []
        doc.meta["breadcrumb"] = crumbs

    # ------------------------------------------------------------ listing
    def _cql(self, since: datetime | None) -> str:
        parts: list[str] = []
        keys = self.spaces()
        if keys:
            parts.append("space in (" + ",".join(f'"{k}"' for k in keys) + ")")
        types = ["page"] + (["blogpost"] if self.cfg.get("include_blogposts") else [])
        parts.append("type in (" + ",".join(types) + ")")
        user = re.sub(r"\border\s+by\b.*$", "", (self.cfg.get("cql") or "").strip(),
                      flags=re.I | re.S).strip()
        if user:
            parts.append(f"({user})")
        if since is not None:
            parts.append(f'lastmodified >= "{(since - _OVERLAP):%Y-%m-%d %H:%M}"')
        return " AND ".join(parts) + " ORDER BY lastmodified ASC"

    def _listing(self, since: datetime | None):
        """Yield ``(doc, content_id, ctype, v2)`` for every candidate page."""
        use_v2 = self.cloud and not (self.cfg.get("cql") or "").strip()
        if not use_v2:
            expand = "body.storage,version,space,ancestors,metadata.labels,history"
            for c in self._paged(f"{self.v1}/content/search",
                                 {"cql": self._cql(since), "expand": expand, "limit": 25},
                                 v2=False):
                ctype = "blogposts" if c.get("type") == "blogpost" else "pages"
                yield self._doc_v1(c), str(c.get("id")), ctype, False
            return
        spaces = {}
        for sp in self._paged(f"{self.v2}/spaces", {"keys": ",".join(self.spaces()),
                                                    "limit": 250}, v2=True):
            spaces[str(sp.get("id"))] = {"key": sp.get("key"), "name": sp.get("name")}
        ctypes = ["pages"] + (["blogposts"] if self.cfg.get("include_blogposts") else [])
        floor = (since - _OVERLAP) if since else None
        for space_id, space in spaces.items():
            for ctype in ctypes:
                it = self._paged(f"{self.v2}/spaces/{space_id}/{ctype}",
                                 {"body-format": "storage", "sort": "-modified-date",
                                  "limit": 50}, v2=True)
                for c in it:
                    doc = self._doc_v2(c, space, ctype)
                    if floor and doc.modified and doc.modified < floor:
                        break  # sorted newest first: the rest is older
                    yield doc, doc.key, ctype, True

    # ------------------------------------------------------------- ingest
    def ingest(self) -> dict:
        """Read new/changed pages and store them. Never raises."""
        from nexus.db import get_connection
        from nexus.storage.confluence_sources import save_confluence_scan

        since = _parse_when(self.cfg.get("watermark"))
        target = Target.from_row(self.cfg)
        max_items = int(self.cfg.get("max_items") or 200)
        checked = stored = changed = new_items = 0
        newest: datetime | None = None
        complete = True
        try:
            with get_connection() as conn:
                known = known_keys(conn, KIND, self.id)
            for doc, cid, ctype, v2 in self._listing(since):
                checked += 1
                prev = known.get(doc.key)
                if (prev and doc.version and prev.get("version") == doc.version
                        and prev.get("document_id") is not None):
                    newest = max(filter(None, [newest, doc.modified]), default=None)
                    continue  # same version as stored: nothing to read
                if stored >= max_items:
                    complete = False
                    break
                if v2:
                    self._v2_meta(doc, ctype)
                self._extras(doc, cid, ctype)
                with get_connection() as conn:
                    res = sync_document(conn, kind=KIND, source_id=self.id,
                                        source_name=self.name, track="confluence",
                                        doc=doc, target=target)
                stored += res["change"] != "same"
                changed += res["change"] == "changed"
                new_items += int(res.get("new") or 0)
                newest = max(filter(None, [newest, doc.modified]), default=None)
        except ConfluenceError as exc:
            self.status = f"error: {exc}"
            complete = False
        except Exception as exc:  # never let one connection break a scan
            logger.exception("Confluence '%s' failed", self.name)
            self.status = f"error: {type(exc).__name__}"
            complete = False
        if not self.status:
            self.status = (f"{checked} checked, {stored - changed} new, {changed} updated"
                           + ("" if complete else f" (stopped at {max_items}; more next scan)"))
        # The v1 listing is oldest-first, so a partial run may still advance the
        # watermark; the v2 listing is newest-first, so only a complete run may.
        uses_v2 = self.cloud and not (self.cfg.get("cql") or "").strip()
        mark = newest.isoformat() if newest and (complete or not uses_v2) else None
        try:
            with get_connection() as conn:
                save_confluence_scan(conn, self.id, self.status, mark)
        except Exception:
            logger.exception("Could not save the Confluence scan state for '%s'", self.name)
        logger.info("Confluence '%s': %s", self.name, self.status)
        return {"fetched": checked, "new": new_items, "changed": changed,
                "status": self.status}


def load_confluence_sources(settings: Settings | None = None) -> list[ConfluenceSource]:
    """A ConfluenceSource for every enabled connection. Never raises."""
    from nexus.db import get_connection
    from nexus.storage.confluence_sources import list_confluence_sources

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_confluence_sources(conn, enabled_only=True)
    except Exception:
        logger.exception("Could not load Confluence connections")
        return []
    return [ConfluenceSource(row, settings) for row in rows]
