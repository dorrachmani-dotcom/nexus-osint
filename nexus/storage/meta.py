"""Key/value meta table and per-source collection watermarks."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


# --- Meta: runtime key/value settings (never secrets) -----------------------
# A tiny key/value store for non-secret runtime toggles the analyst can change
# from the dashboard (e.g. the active AI provider). API keys are NEVER stored
# here — those live only in .env. DB values override env defaults at resolution.
def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None and row[0] is not None else default


def get_meta_value(key: str, default: str | None = None) -> str | None:
    """Read a meta value on a short-lived connection. Never raises.

    Used by config to resolve DB overrides without holding a connection; on any
    storage error it returns the default so behaviour falls back to env.
    """
    try:
        from nexus.db import get_connection

        with get_connection() as conn:
            return get_meta(conn, key, default)
    except Exception:
        return default


# --- Per-source watermarks (incremental "Update") --------------------------
# Each source carries a watermark in ``sources.last_synced``: the start time of
# its last *successful* fetch. The collector reads it as ``since`` so a scan
# surfaces only what is new, and advances it only when a source persisted
# without error — a failed source keeps its old watermark so nothing is skipped.
def _parse_utc(value: str | None) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime. Never raises.

    Handles both ISO-8601 (with or without offset/``Z``) and SQLite's
    ``datetime('now')`` format (``YYYY-MM-DD HH:MM:SS``, implicitly UTC).
    Returns None on anything unparseable so callers degrade to a full pull.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                # Naive on purpose: made UTC-aware just below.
                parsed = datetime.strptime(text, fmt)  # noqa: DTZ007
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # SQLite datetime('now') is UTC but carries no offset.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_source_watermark(conn: sqlite3.Connection, name: str) -> datetime | None:
    """Return a source's last-successful-sync time as aware UTC, or None.

    None means "no successful sync recorded yet" -> the collector does a full
    (window-bounded) pull. Never raises on a malformed stored value.
    """
    row = conn.execute(
        "SELECT last_synced FROM sources WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return _parse_utc(row["last_synced"])


def set_source_watermark(
    conn: sqlite3.Connection,
    name: str,
    when: datetime,
    track: str = "api",
) -> None:
    """Advance a source's watermark to ``when`` (stored as aware UTC ISO-8601).

    Upserts the ``sources`` row so a never-seen source is registered. Call this
    only after a successful fetch+persist, passing the scan-start time so the
    next scan picks up everything published during this run.
    """
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    conn.execute(
        """
        INSERT INTO sources (name, track, last_synced)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET last_synced = excluded.last_synced
        """,
        (name, track, when.isoformat()),
    )
