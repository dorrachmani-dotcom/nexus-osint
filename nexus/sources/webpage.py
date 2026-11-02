"""Watched web pages and documentation sites as a normal collector Source.

An analyst adds a URL (a public page, an internal wiki, a docs portal). On each
scan this source:

  1. fetches the start page through the SSRF guard (every redirect hop is
     re-checked; internal hosts only when allow-listed in Settings),
  2. optionally crawls the same site — same scheme and host, optionally only
     below the start path — within depth and page limits,
  3. respects ``robots.txt`` of the **target host only** (no other host is
     ever contacted) and waits ``delay_seconds`` between requests,
  4. extracts the main readable text (HTML via the stdlib parser; a linked
     PDF/DOCX/… via the upload extractors),
  5. compares each page's text hash with the last scan and emits an item for a
     **new** page (its text) or a **changed** page (a short diff summary).

Page state is staged during ``fetch`` and only committed by the collector after
the items were stored (``commit_state``), so a failed persist never loses a
change. Secrets for internal sites (Basic credentials, a token, a header value
or a Cookie string) are read from ``.env`` (``WEB_SOURCE_<id>_SECRET``) only.
Never raises out of ``fetch``.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import logging
import posixpath
import time
import urllib.robotparser
from collections import deque
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from nexus.config import Settings, get_settings
from nexus.envstore import read_env_value, web_source_env_name
from nexus.ingest.extract import decode_text, detect_kind, extract
from nexus.ingest.html_text import extract_html
from nexus.ingest.upload import split_text
from nexus.models import RawItem
from nexus.netguard import ResponseTooLarge, safe_request, tls_verify_for
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.webpage")

USER_AGENT = "Nexus-OSINT page watcher (+local analyst workstation)"
_TIMEOUT = 25.0
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_ITEM_CHARS = 6000
_DIFF_LINES = 40
_SKIP_EXT = {"css", "js", "mjs", "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
             "woff", "woff2", "ttf", "otf", "eot", "mp4", "mp3", "webm", "avi", "mov",
             "zip", "gz", "tar", "7z", "rar", "exe", "msi", "dmg", "iso", "bin"}


def _norm_url(url: str) -> str:
    return urldefrag(url)[0]


def _host_key(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    return p.scheme.lower(), (p.netloc or "").lower()


def diff_summary(old: str, new: str, max_lines: int = _DIFF_LINES) -> str:
    """A short human-readable summary of what changed between two texts."""
    old_lines = [ln.strip() for ln in (old or "").splitlines() if ln.strip()]
    new_lines = [ln.strip() for ln in (new or "").splitlines() if ln.strip()]
    added: list[str] = []
    removed: list[str] = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())
        elif line.startswith("-"):
            removed.append(line[1:].strip())
    parts = [f"{len(added)} line(s) added, {len(removed)} line(s) removed."]
    if added:
        parts.append("Added:\n" + "\n".join(f"+ {a[:300]}" for a in added[:max_lines]))
        if len(added) > max_lines:
            parts.append(f"(+{len(added) - max_lines} more added lines)")
    if removed:
        parts.append("Removed:\n" + "\n".join(f"- {r[:300]}" for r in removed[:max_lines]))
        if len(removed) > max_lines:
            parts.append(f"(+{len(removed) - max_lines} more removed lines)")
    return "\n\n".join(parts)


class _Robots:
    """robots.txt rules for one site, fetched from that site only."""

    def __init__(self, get: Callable[[str], httpx.Response | None]) -> None:
        self._get = get
        self._cache: dict[tuple[str, str], urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        key = _host_key(url)
        if key not in self._cache:
            parser: urllib.robotparser.RobotFileParser | None = None
            resp = self._get(f"{key[0]}://{key[1]}/robots.txt")
            if resp is not None and resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(decode_text(resp.content).splitlines())
            # 404 / auth wall / error: no rules published -> allowed.
            self._cache[key] = parser
        parser = self._cache[key]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)


class WebPageSource(Source):
    """One watched URL (optionally a crawled site) from a ``web_sources`` row."""

    def __init__(self, cfg: dict, settings: Settings | None = None, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg
        self.id = int(cfg["id"])
        self.name = (cfg.get("name") or f"web{self.id}").strip() or f"web{self.id}"
        self._sleep = sleep
        self.responded = False
        self.status = ""
        self._staged: dict[str, dict] = {}

    # ------------------------------------------------------------ plumbing
    def is_available(self) -> bool:
        if not self.cfg.get("enabled", 1):
            return False
        url = (self.cfg.get("url") or "").strip().lower()
        if not url.startswith(("http://", "https://")):
            return False
        auth = (self.cfg.get("auth_type") or "none").lower()
        return not (auth != "none" and not self._secret())

    def _secret(self) -> str | None:
        return read_env_value(web_source_env_name(self.id))

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,"
                   "text/plain;q=0.9,*/*;q=0.5"}
        auth = (self.cfg.get("auth_type") or "none").lower()
        secret = self._secret() or ""
        if not secret or auth == "none":
            return headers
        if auth == "basic":
            token = base64.b64encode(secret.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif auth == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif auth == "header":
            headers[(self.cfg.get("auth_param") or "Authorization").strip()] = secret
        elif auth == "cookie":
            headers["Cookie"] = secret
        return headers

    def _get(self, url: str, max_bytes: int = _MAX_PAGE_BYTES) -> httpx.Response | None:
        try:
            return safe_request(
                "GET", url, timeout=_TIMEOUT, headers=self._headers(),
                verify=tls_verify_for(self.cfg.get("tls_mode")), max_bytes=max_bytes,
            )
        except ResponseTooLarge:
            logger.info("Web source '%s': %s is too large; skipped", self.name, url)
        except ValueError as exc:  # refused by the SSRF guard
            logger.warning("Web source '%s' blocked %s: %s", self.name, url, exc)
            self.status = f"blocked: {exc}"
        except httpx.HTTPError as exc:
            logger.warning("Web source '%s' could not fetch %s: %s", self.name, url, exc)
            self.status = f"fetch failed: {type(exc).__name__}"
        return None

    # -------------------------------------------------------------- reading
    def _read(self, url: str, resp: httpx.Response) -> tuple[str, str, list[str]]:
        """Return ``(title, text, links)`` for one fetched page."""
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        path = urlsplit(url).path
        filename = posixpath.basename(path) or "page"
        if ctype in ("text/html", "application/xhtml+xml") or (
            not ctype and detect_kind(filename, resp.content[:2048]) == "html"
        ):
            doc = extract_html(resp.text)
            return doc.title or filename, doc.text, [h for h, _t in doc.links]
        if ctype.startswith("text/") and ctype not in ("text/csv", "text/xml"):
            return filename, decode_text(resp.content), []
        # A linked document (PDF, DOCX, CSV, JSON, XML...): reuse the upload parsers.
        ex = extract(resp.content, filename)
        text = "\n\n".join(s.text for s in ex.sections)
        return filename, text, []

    def _links(self, base: str, hrefs: list[str], start: str) -> list[str]:
        start_scheme, start_host = _host_key(start)
        start_path = urlsplit(start).path or "/"
        prefix = start_path if start_path.endswith("/") else posixpath.dirname(start_path) + "/"
        out: list[str] = []
        for href in hrefs:
            href = (href or "").strip()
            if not href or href.startswith(("mailto:", "javascript:", "tel:", "data:")):
                continue
            absolute = _norm_url(urljoin(base, href))
            scheme, host = _host_key(absolute)
            if scheme != start_scheme or host != start_host:
                continue  # same site only
            path = urlsplit(absolute).path or "/"
            ext = posixpath.splitext(path)[1].lstrip(".").lower()
            if ext in _SKIP_EXT:
                continue
            if self.cfg.get("stay_under_path", 1) and not path.startswith(prefix):
                continue
            out.append(absolute)
        return out

    # ----------------------------------------------------------------- scan
    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        try:
            return self._crawl()
        except Exception:  # never let one site break a scan
            logger.exception("Web source '%s' failed", self.name)
            self.status = "error"
            return []

    def _crawl(self) -> list[RawItem]:
        from nexus.db import get_connection
        from nexus.storage.web_sources import page_states

        start = _norm_url((self.cfg.get("url") or "").strip())
        crawl = bool(self.cfg.get("crawl"))
        max_depth = int(self.cfg.get("max_depth") or 0) if crawl else 0
        max_pages = int(self.cfg.get("max_pages") or 1) if crawl else 1
        delay = float(self.cfg.get("delay_seconds") or 1.0)
        with get_connection() as conn:
            previous = page_states(conn, self.id)

        robots = _Robots(lambda u: self._get(u, max_bytes=512 * 1024))
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        seen = {start}
        items: list[RawItem] = []
        fetched = 0
        blocked_by_robots = 0
        self._staged = {}
        while queue and fetched < max_pages:
            url, depth = queue.popleft()
            if not robots.allowed(url):
                blocked_by_robots += 1
                continue
            if fetched:
                self._sleep(delay)
            resp = self._get(url)
            fetched += 1
            if resp is None or resp.status_code >= 400:
                if resp is not None:
                    self.status = f"HTTP {resp.status_code} for {url}"
                continue
            self.responded = True
            title, text, hrefs = self._read(url, resp)
            text = text.strip()
            digest = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
            self._staged[url] = {"title": title, "content_hash": digest, "text": text}
            items.extend(self._items_for(url, title, text, digest, previous.get(url)))
            if crawl and depth < max_depth:
                for link in self._links(str(resp.url), hrefs, start):
                    if link not in seen:
                        seen.add(link)
                        queue.append((link, depth + 1))

        changed = sum(1 for i in items if i.raw.get("change") == "changed")
        new = len({i.url for i in items if i.raw.get("change") == "new"})
        if self.responded:
            self.status = (f"{fetched} page(s) checked, {new} new, {changed} changed"
                           + (f", {blocked_by_robots} skipped by robots.txt"
                              if blocked_by_robots else ""))
        elif blocked_by_robots:
            self.status = "all pages disallowed by robots.txt"
        logger.info("Web source '%s': %s", self.name, self.status)
        return items

    def _items_for(self, url: str, title: str, text: str, digest: str,
                   old: dict | None) -> list[RawItem]:
        base_raw = {"web_source_id": self.id, "page_url": url, "content_hash": digest}
        if old is None:
            if not text:
                return []
            chunks = split_text(text, _ITEM_CHARS)
            out = []
            for n, chunk in enumerate(chunks, 1):
                label = title + (f" ({n}/{len(chunks)})" if len(chunks) > 1 else "")
                out.append(RawItem(
                    source=self.name, track="web", external_id=f"{url}#{digest[:16]}:{n}",
                    url=url, title=label[:300], content=chunk,
                    raw={**base_raw, "change": "new", "part": n},
                ))
            return out
        if old.get("content_hash") == digest:
            return []
        summary = diff_summary(old.get("text") or "", text)
        return [RawItem(
            source=self.name, track="web", external_id=f"{url}#{digest[:16]}",
            url=url, title=f"{title} — updated"[:300], content=summary[:_ITEM_CHARS],
            raw={**base_raw, "change": "changed",
                 "previous_hash": old.get("content_hash")},
        )]

    def commit_state(self) -> None:
        """Persist the page states seen this scan (called after items are stored)."""
        from nexus.db import get_connection
        from nexus.storage.web_sources import save_page_states

        with get_connection() as conn:
            save_page_states(conn, self.id, self._staged, self.status or "no response")
        self._staged = {}

    def test_fetch(self) -> dict:
        """Fetch only the start page and describe it (for the Test button)."""
        start = _norm_url((self.cfg.get("url") or "").strip())
        robots = _Robots(lambda u: self._get(u, max_bytes=512 * 1024))
        if not robots.allowed(start):
            return {"ok": False, "error": "robots.txt on that site disallows this page."}
        resp = self._get(start)
        if resp is None:
            return {"ok": False, "error": self.status or "the page could not be fetched"}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"the server answered HTTP {resp.status_code}"}
        title, text, hrefs = self._read(start, resp)
        same_site = self._links(str(resp.url), hrefs, start)
        return {"ok": True, "title": title, "chars": len(text), "excerpt": text[:400],
                "links": len(same_site)}


def load_web_sources(settings: Settings | None = None) -> list[WebPageSource]:
    """A WebPageSource for every enabled row. Never raises."""
    from nexus.db import get_connection
    from nexus.storage.web_sources import list_web_sources

    settings = settings or get_settings()
    try:
        with get_connection() as conn:
            rows = list_web_sources(conn, enabled_only=True)
    except Exception:
        logger.exception("Could not load web page sources")
        return []
    return [WebPageSource(row, settings) for row in rows]
