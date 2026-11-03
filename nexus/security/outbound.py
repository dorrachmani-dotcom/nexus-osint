"""Outbound activity log: a durable list of everything that left the machine.

The egress monitor (:mod:`nexus.security.egress`) observes *connections*. This
log records *actions*, in words an analyst can audit: "3 calls to Gemini,
41 KB sent", "page updated on Confluence", "daily brief sent by SMTP to 2
recipients", "URL submitted to the Internet Archive", "hash looked up on
VirusTotal", "RSS fetched 12 items".

Never logged: content (prompts, item text, titles, page bodies, e-mail bodies,
URLs of items), secrets (keys, tokens, passwords) or full request URLs. Only
the category, a destination host or service name, the action, counts, byte
sizes and success. Callers pass exactly those fields; :func:`record` also
cuts every string short and drops anything that looks like a URL query or a
credential, as a second line of defence.

Recording never raises and never blocks the action it describes.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("nexus.security.outbound")

CATEGORIES = {
    "ai": "Cloud / local AI",
    "publish": "Publishing",
    "email": "E-mail",
    "archive": "Internet Archive",
    "virustotal": "VirusTotal",
    "source": "Source fetches",
}
_RETENTION_DAYS = 365
_MAX_ROWS = 200_000
_PRUNE_EVERY = 500
_writes = 0
_writes_lock = threading.Lock()
_SENSITIVE = re.compile(r"(?i)(key|token|secret|password|passwd|bearer|auth)[=:]\S*")


def _clip(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = _SENSITIVE.sub(r"\1=***", text)
    if "?" in text and "://" in text:
        text = text.split("?", 1)[0]
    return text[:limit]


def host_of(url: str | None) -> str:
    """Only the host of a URL (no path, query, user info or port)."""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def record(category: str, target: str, action: str, *, items: int = 0,
           bytes_out: int = 0, bytes_in: int = 0, ok: bool = True, detail: str = "",
           conn=None) -> None:
    """Append one row. Never raises; never stores content or secrets.

    Pass ``conn`` when the caller holds an open write transaction, so the row
    joins it instead of waiting for the database lock."""
    if category not in CATEGORIES:
        category = "source"
    row = (datetime.now(UTC).isoformat(timespec="seconds"), category, _clip(target),
           _clip(action, 80), max(0, int(items)), max(0, int(bytes_out)),
           max(0, int(bytes_in)), 1 if ok else 0, _clip(detail, 200))
    sql = ("INSERT INTO outbound_log (ts, category, target, action, items, bytes_out, "
           "bytes_in, ok, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    try:
        if conn is not None:
            conn.execute(sql, row)
            return
        from nexus.db import get_connection

        with get_connection() as own:
            own.execute(sql, row)
            _maybe_prune(own)
    except Exception:
        logger.debug("Outbound activity log write failed", exc_info=True)


def _maybe_prune(conn) -> None:
    global _writes
    with _writes_lock:
        _writes += 1
        due = _writes % _PRUNE_EVERY == 1
    if not due:
        return
    cutoff = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM outbound_log WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM outbound_log WHERE id <= (SELECT id FROM outbound_log "
        "ORDER BY id DESC LIMIT 1 OFFSET ?)", (_MAX_ROWS,),
    )


# ------------------------------------------------------------------- query
def _where(category: str = "", days: int = 0, status: str = "") -> tuple[str, list]:
    clauses: list[str] = []
    args: list[object] = []
    if category in CATEGORIES:
        clauses.append("category = ?")
        args.append(category)
    if days > 0:
        clauses.append("ts >= ?")
        args.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
    if status in ("ok", "failed"):
        clauses.append("ok = ?")
        args.append(1 if status == "ok" else 0)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def query(conn, *, category: str = "", days: int = 0, status: str = "",
          limit: int = 500) -> list[dict]:
    where, args = _where(category, days, status)
    rows = conn.execute(
        f"SELECT ts, category, target, action, items, bytes_out, bytes_in, ok, detail "  # noqa: S608
        f"FROM outbound_log{where} ORDER BY id DESC LIMIT ?", (*args, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def summary(conn, *, days: int = 0) -> list[dict]:
    where, args = _where("", days, "")
    rows = conn.execute(
        f"SELECT category, target, COUNT(*) AS events, SUM(items) AS items, "  # noqa: S608
        f"SUM(bytes_out) AS bytes_out, SUM(1 - ok) AS failed, MAX(ts) AS last "
        f"FROM outbound_log{where} GROUP BY category, target ORDER BY last DESC", args,
    ).fetchall()
    return [dict(r) for r in rows]


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    cols = ["ts", "category", "target", "action", "items", "bytes_out", "bytes_in", "ok", "detail"]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Neutralise spreadsheet formulas in exported text cells.
        safe = {k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@\t\r" else v)
                for k, v in row.items()}
        writer.writerow(safe)
    return buf.getvalue()
