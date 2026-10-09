"""Internet Archive (Wayback Machine) preservation of item sources.

An evidence screenshot proves what *we* saw; a Wayback Machine capture is an
independent, third-party record that a page existed at a given moment — useful
when a source is later edited or deleted. This module offers two operations:

  * ``lookup(url)`` — the newest existing snapshot, via the public availability
    API (``https://archive.org/wayback/available``). No key needed.
  * ``save(url)``   — request a fresh capture via Save Page Now (SPN).
      - Anonymous: ``GET https://web.archive.org/save/<url>``; the snapshot path
        comes back in the ``Content-Location`` / ``Location`` header (or body).
      - Authenticated (SPN2): when ``ARCHIVE_ORG_ACCESS_KEY`` and
        ``ARCHIVE_ORG_SECRET_KEY`` are set in ``.env``, ``POST
        https://web.archive.org/save`` with ``Authorization: LOW key:secret``,
        then poll ``/save/status/<job_id>`` until the capture finishes.

Captures can take 10-60 s, so the web layer never calls ``save`` inline: it
enqueues the item on :class:`ArchiveQueue`, a single background worker that
spaces requests politely (anonymous SPN allows roughly a dozen captures a
minute) and records every outcome in the ``item_archives`` table.

OpSec: archiving tells the Internet Archive which URL you care about and
creates a PUBLIC capture. The UI shows a one-time warning before first use.

Every public function here is fail-soft: it returns a plain-English result and
never raises into a request. Every URL passes the shared SSRF guard first.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from nexus.netguard import safe_http_url

logger = logging.getLogger("nexus.wayback")

USER_AGENT = "Nexus-OSINT (+https://github.com/dorrachmani-dotcom/nexus-osint)"
AVAILABILITY_API = "https://archive.org/wayback/available"
WAYBACK_BASE = "https://web.archive.org"
SPN_ANON_PREFIX = f"{WAYBACK_BASE}/save/"
SPN2_ENDPOINT = f"{WAYBACK_BASE}/save"
SPN2_STATUS = f"{WAYBACK_BASE}/save/status/"

# Timeouts: the availability API is quick; an anonymous SPN request is held open
# while the page is captured, so it gets a long read timeout.
LOOKUP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
SAVE_TIMEOUT = httpx.Timeout(90.0, connect=15.0)

# Spacing between capture requests. Anonymous SPN tolerates about 12/minute;
# authenticated SPN2 is more generous but we stay polite either way.
MIN_INTERVAL_ANON = 6.0
MIN_INTERVAL_AUTH = 3.0
# Extra pause after the archive tells us to slow down (HTTP 429).
RATE_LIMIT_COOLDOWN = 90.0

# SPN2 polling.
SPN2_POLL_INTERVAL = 4.0
SPN2_POLL_TIMEOUT = 120.0

# Settings stored in the DB meta table (not secrets).
META_ENABLED = "archive_enabled"          # "1" (default) / "0"
META_OPSEC_ACK = "archive_opsec_ack"      # "1" once the warning was acknowledged

_SNAPSHOT_RE = re.compile(r"/web/(\d{14})(?:[a-z_]{0,4})/(\S+)")


def _result(ok: bool, *, archive_url: str = "", archived_at: str = "",
            error: str = "", rate_limited: bool = False) -> dict:
    return {"ok": ok, "archive_url": archive_url, "archived_at": archived_at,
            "error": error, "rate_limited": rate_limited}


def _http_fail(status: int) -> dict:
    return _result(False, error=_friendly_http_error(status), rate_limited=status == 429)


def _headers(extra: dict | None = None) -> dict:
    h = {"User-Agent": USER_AGENT}
    if extra:
        h.update(extra)
    return h


def timestamp_to_iso(ts: str | None) -> str:
    """``20260102030405`` -> ``2026-01-02T03:04:05Z`` (empty on bad input)."""
    try:
        dt = datetime.strptime(str(ts or "")[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return ""
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_snapshot_path(value: str | None) -> tuple[str, str] | None:
    """Extract ``(archive_url, archived_at_iso)`` from a Wayback snapshot path.

    Accepts a bare path (``/web/20260102030405/https://example.org/``), a full
    URL, or a chunk of HTML/text that contains one.
    """
    if not value:
        return None
    m = _SNAPSHOT_RE.search(str(value))
    if not m:
        return None
    ts, original = m.group(1), m.group(2).rstrip("\"'<>;,)")
    return f"{WAYBACK_BASE}/web/{ts}/{original}", timestamp_to_iso(ts)


def _guard(url: str) -> str:
    """Return an error string if the URL must not be archived, else ''."""
    ok, reason = safe_http_url(url)
    if ok:
        return ""
    return f"This link can't be archived ({reason}). Only public web pages are supported."


def _credentials(settings=None) -> tuple[str, str] | None:
    """The SPN2 key pair from settings (.env), or None for anonymous use."""
    try:
        if settings is None:
            from nexus.config import get_settings

            settings = get_settings()
        key = (getattr(settings, "archive_org_access_key", None) or "").strip()
        secret = (getattr(settings, "archive_org_secret_key", None) or "").strip()
    except Exception:
        return None
    return (key, secret) if key and secret else None


def check_url(url: str) -> str:
    """Public SSRF pre-check: an error message if ``url`` can't be archived, else ''."""
    try:
        return _guard(url)
    except Exception:
        return "This link can't be archived."


def keys_configured(settings=None) -> bool:
    """True when both archive.org keys are set (never exposes the values)."""
    return _credentials(settings) is not None


def _friendly_http_error(status: int) -> str:
    if status == 429:
        return ("The Internet Archive is rate-limiting requests right now. "
                "Wait a few minutes and retry.")
    if status in (401, 403):
        return ("The Internet Archive refused the request (check the archive.org "
                "keys in Settings, or the site may block archiving).")
    if status == 404:
        return "The Internet Archive could not reach this page."
    if status >= 500:
        return ("The Internet Archive is busy or unavailable (server error "
                f"{status}). Try again later.")
    return f"The Internet Archive returned an unexpected response ({status})."


# --------------------------------------------------------------------- lookup


def lookup(url: str, *, client: httpx.Client | None = None) -> dict:
    """Newest existing snapshot of ``url``. Never raises.

    Returns ``{"ok", "archive_url", "archived_at", "error"}``; ``ok`` is False
    with a friendly ``error`` when nothing is archived or the lookup failed.
    """
    try:
        blocked = _guard(url)
        if blocked:
            return _result(False, error=blocked)
        own = client is None
        c = client or httpx.Client(timeout=LOOKUP_TIMEOUT, follow_redirects=True)
        try:
            resp = c.get(AVAILABILITY_API, params={"url": url}, headers=_headers())
        finally:
            if own:
                c.close()
        if resp.status_code != 200:
            return _http_fail(resp.status_code)
        data = resp.json() or {}
        closest = ((data.get("archived_snapshots") or {}).get("closest") or {})
        snap_url = str(closest.get("url") or "")
        if not closest or not snap_url or str(closest.get("available")).lower() == "false":
            return _result(False, error="No snapshot of this page exists on the Wayback Machine yet.")
        if snap_url.startswith("http://"):
            snap_url = "https://" + snap_url[len("http://"):]
        return _result(True, archive_url=snap_url,
                       archived_at=timestamp_to_iso(closest.get("timestamp")))
    except httpx.TimeoutException:
        return _result(False, error="The Internet Archive did not answer in time. Try again later.")
    except httpx.HTTPError:
        return _result(False, error="Could not reach the Internet Archive (are you online?).")
    except Exception:
        logger.exception("Wayback lookup failed")
        return _result(False, error="Looking up the snapshot failed unexpectedly.")


# ----------------------------------------------------------------------- save


def _save_anonymous(url: str, client: httpx.Client) -> dict:
    resp = client.get(SPN_ANON_PREFIX + url, headers=_headers())
    # The snapshot path arrives in a header on success (200 or a redirect).
    for header in ("content-location", "location"):
        found = parse_snapshot_path(resp.headers.get(header))
        if found:
            return _result(True, archive_url=found[0], archived_at=found[1])
    if resp.status_code >= 400:
        return _http_fail(resp.status_code)
    found = parse_snapshot_path(resp.headers.get("link")) or parse_snapshot_path(
        resp.text[:200_000] if resp.text else ""
    )
    if found:
        return _result(True, archive_url=found[0], archived_at=found[1])
    return _result(False, error="The Internet Archive accepted the request but did not "
                                "report a snapshot. Try 'Find existing snapshot' in a minute.")


def _save_spn2(url: str, creds: tuple[str, str], client: httpx.Client,
               sleep: Callable[[float], None], poll_timeout: float) -> dict:
    auth = {"Authorization": f"LOW {creds[0]}:{creds[1]}", "Accept": "application/json"}
    resp = client.post(SPN2_ENDPOINT, data={"url": url}, headers=_headers(auth))
    if resp.status_code != 200:
        return _http_fail(resp.status_code)
    try:
        data = resp.json() or {}
    except ValueError:
        return _result(False, error="The Internet Archive sent an unreadable reply.")
    job_id = str(data.get("job_id") or "")
    if not job_id:
        msg = str(data.get("message") or "").strip()
        return _result(False, error=("The Internet Archive declined the capture"
                                     + (f": {msg[:200]}" if msg else ".")))
    waited = 0.0
    while True:
        st = client.get(SPN2_STATUS + quote(job_id, safe=""), headers=_headers(auth))
        if st.status_code != 200:
            return _http_fail(st.status_code)
        try:
            info = st.json() or {}
        except ValueError:
            return _result(False, error="The Internet Archive sent an unreadable status reply.")
        status = str(info.get("status") or "")
        if status == "success":
            ts = str(info.get("timestamp") or "")
            original = str(info.get("original_url") or url)
            if not ts:
                return _result(False, error="Capture finished without a timestamp.")
            return _result(True, archive_url=f"{WAYBACK_BASE}/web/{ts}/{original}",
                           archived_at=timestamp_to_iso(ts))
        if status == "error":
            msg = str(info.get("message") or info.get("status_ext") or "unknown reason")
            return _result(False, error=f"The capture failed: {msg[:200]}")
        if waited >= poll_timeout:
            return _result(False, error="The capture is taking too long; check again later "
                                        "with 'Find existing snapshot'.")
        sleep(SPN2_POLL_INTERVAL)
        waited += SPN2_POLL_INTERVAL


def save(url: str, *, settings=None, client: httpx.Client | None = None,
         sleep: Callable[[float], None] = time.sleep,
         poll_timeout: float = SPN2_POLL_TIMEOUT) -> dict:
    """Request a fresh Wayback capture of ``url``. Never raises.

    Uses authenticated SPN2 when archive.org keys are configured, otherwise the
    anonymous Save Page Now endpoint. Returns the same dict shape as ``lookup``
    plus ``rate_limited`` (True after an HTTP 429, so the queue can back off).
    """
    try:
        blocked = _guard(url)
        if blocked:
            return _result(False, error=blocked)
        creds = _credentials(settings)
        own = client is None
        c = client or httpx.Client(timeout=SAVE_TIMEOUT, follow_redirects=False)
        try:
            if creds:
                res = _save_spn2(url, creds, c, sleep, poll_timeout)
            else:
                res = _save_anonymous(url, c)
        finally:
            if own:
                c.close()
        return res
    except httpx.TimeoutException:
        return _result(False, error="The Internet Archive did not finish in time. "
                                    "Try 'Find existing snapshot' later.")
    except httpx.HTTPError:
        return _result(False, error="Could not reach the Internet Archive (are you online?).")
    except Exception:
        logger.exception("Wayback save failed")
        return _result(False, error="Archiving failed unexpectedly.")


# -------------------------------------------------------------- rate limiting


class RateLimiter:
    """Minimum spacing between requests, with an optional cooldown penalty.

    ``clock`` and ``sleep`` are injectable so tests run instantly.
    """

    def __init__(self, min_interval: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = float(min_interval)
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Block until the next slot is free; return how long we waited."""
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            self._sleep(delay)
        return delay

    def penalize(self, seconds: float) -> None:
        """Push the next slot out (e.g. after the archive returned HTTP 429)."""
        with self._lock:
            self._next_at = max(self._next_at, self._clock() + float(seconds))


# ---------------------------------------------------------------------- queue


class ArchiveQueue:
    """A single background worker that captures queued items one at a time.

    ``enqueue`` records a ``pending`` row and hands the job to the worker; the
    worker waits for a rate-limit slot, calls ``save`` and stores ``done`` or
    ``failed``. Duplicate requests for an item already queued are ignored.
    """

    def __init__(self, *, saver: Callable[[str], dict] | None = None,
                 limiter: RateLimiter | None = None, autostart: bool = True) -> None:
        self._saver = saver
        self._limiter = limiter
        self._autostart = autostart
        self._q: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- state -------------------------------------------------------------
    def is_queued(self, item_id: int) -> bool:
        with self._lock:
            return int(item_id) in self._queued

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queued)

    def _get_limiter(self) -> RateLimiter:
        if self._limiter is None:
            interval = MIN_INTERVAL_AUTH if _credentials() else MIN_INTERVAL_ANON
            self._limiter = RateLimiter(interval)
        return self._limiter

    # -- producer ----------------------------------------------------------
    def enqueue(self, item_id: int, url: str, conn=None) -> bool:
        """Queue one capture. Returns False if it was already queued.

        ``conn`` lets a caller that is inside a write transaction record the
        pending row on its own connection (avoids a lock wait on itself).
        """
        from nexus import storage

        item_id = int(item_id)
        with self._lock:
            if item_id in self._queued:
                return False
            self._queued.add(item_id)
        try:
            if conn is not None:
                storage.set_item_archive(conn, item_id, url, "pending")
            else:
                from nexus.db import get_connection

                with get_connection() as c:
                    storage.set_item_archive(c, item_id, url, "pending")
        except Exception:
            logger.exception("Could not record pending archive job")
            with self._lock:
                self._queued.discard(item_id)
            return False
        self._q.put((item_id, url))
        if self._autostart:
            self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="wayback-archiver"
            )
            self._thread.start()

    # -- consumer ----------------------------------------------------------
    def process_next(self, block: bool = False, timeout: float | None = None) -> bool:
        """Capture one queued item. Returns False when the queue was empty."""
        try:
            item_id, url = self._q.get(block=block, timeout=timeout)
        except queue.Empty:
            return False
        try:
            self._process(item_id, url)
        finally:
            with self._lock:
                self._queued.discard(item_id)
            self._q.task_done()
        return True

    def _process(self, item_id: int, url: str) -> None:
        from nexus import storage
        from nexus.db import get_connection

        limiter = self._get_limiter()
        limiter.wait()
        saver = self._saver or save
        try:
            res = saver(url)
        except Exception:  # a custom saver misbehaving must not kill the worker
            logger.exception("Archive saver raised")
            res = _result(False, error="Archiving failed unexpectedly.")
        if res.get("rate_limited"):
            limiter.penalize(RATE_LIMIT_COOLDOWN)
        try:
            with get_connection() as conn:
                if res.get("ok"):
                    storage.set_item_archive(
                        conn, item_id, url, "done",
                        archive_url=res.get("archive_url"),
                        archived_at=res.get("archived_at") or _now_iso(),
                    )
                else:
                    storage.set_item_archive(
                        conn, item_id, url, "failed",
                        error=res.get("error") or "Archiving failed.",
                    )
        except Exception:
            logger.exception("Could not store archive result for item %s", item_id)

    def _run(self) -> None:
        while True:
            try:
                if not self.process_next(block=True, timeout=30):
                    # Idle: let the thread end; the next enqueue restarts it.
                    with self._lock:
                        if self._q.empty():
                            self._thread = None
                            return
            except Exception:
                logger.exception("Archive worker loop error")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_QUEUE: ArchiveQueue | None = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> ArchiveQueue:
    """The process-wide archive queue (created lazily)."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = ArchiveQueue()
        return _QUEUE


# ----------------------------------------------------------- settings helpers


def is_enabled(conn) -> bool:
    """Global on/off switch from Settings (default ON)."""
    from nexus.storage import get_meta

    return (get_meta(conn, META_ENABLED, "1") or "1") != "0"


def opsec_acknowledged(conn) -> bool:
    from nexus.storage import get_meta

    return get_meta(conn, META_OPSEC_ACK) == "1"


def acknowledge_opsec(conn) -> None:
    from nexus.storage import set_meta

    set_meta(conn, META_OPSEC_ACK, "1")


def auto_archive_on_pin(conn, item_id: int, case_id: int | None,
                        q: ArchiveQueue | None = None) -> bool:
    """Queue a capture for a freshly pinned item when the case opted in.

    Only fires when archiving is enabled globally, the OpSec warning has been
    acknowledged, the case has ``auto_archive`` on, the item has a URL, and the
    item has no capture yet (or only a failed one). Never raises.
    """
    if not case_id:
        return False
    try:
        from nexus import storage

        if not is_enabled(conn) or not opsec_acknowledged(conn):
            return False
        if not storage.case_auto_archive(conn, case_id):
            return False
        row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
        url = (row["url"] if row else "") or ""
        if not url:
            return False
        existing = storage.get_item_archive(conn, item_id)
        if existing and existing.get("status") in ("pending", "done", "existing"):
            return False
        return (q or get_queue()).enqueue(item_id, url, conn=conn)
    except Exception:
        logger.exception("auto-archive on pin failed")
        return False
