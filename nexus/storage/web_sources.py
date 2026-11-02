"""Watched web pages / documentation sites and their last-seen page state."""

from __future__ import annotations

import sqlite3

from nexus.db import last_row_id

WEB_FIELDS = (
    "name", "url", "enabled", "crawl", "max_depth", "max_pages", "stay_under_path",
    "delay_seconds", "tls_mode", "auth_type", "auth_param",
)
WEB_TLS_MODES = {"verify", "ca_bundle", "insecure"}
WEB_AUTH_TYPES = {"none", "basic", "bearer", "header", "cookie"}
MAX_DEPTH = 3
MAX_PAGES = 200
PAGE_TEXT_MAX = 200_000  # chars of last-seen text kept per page (for diffs)


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clean_web_source(cfg: dict) -> dict:
    """Normalise an incoming config dict to safe, storable values."""
    out: dict = {}
    url = (cfg.get("url") or "").strip()[:1000]
    out["url"] = url
    host = url.split("://", 1)[-1].split("/", 1)[0] if url else ""
    out["name"] = (cfg.get("name") or "").strip()[:80] or (f"web: {host}" if host else "Web page")
    out["enabled"] = 1 if cfg.get("enabled", 1) not in (0, "0", False, "") else 0
    out["crawl"] = 1 if cfg.get("crawl") not in (None, 0, "0", False, "") else 0
    out["max_depth"] = _int(cfg.get("max_depth"), 1, 0, MAX_DEPTH)
    out["max_pages"] = _int(cfg.get("max_pages"), 20, 1, MAX_PAGES)
    out["stay_under_path"] = 1 if cfg.get("stay_under_path", 1) not in (0, "0", False, "") else 0
    try:
        delay = float(cfg.get("delay_seconds") or 1.0)
    except (TypeError, ValueError):
        delay = 1.0
    out["delay_seconds"] = max(0.5, min(30.0, delay))
    tls = (cfg.get("tls_mode") or "verify").strip().lower()
    out["tls_mode"] = tls if tls in WEB_TLS_MODES else "verify"
    auth = (cfg.get("auth_type") or "none").strip().lower()
    out["auth_type"] = auth if auth in WEB_AUTH_TYPES else "none"
    out["auth_param"] = (cfg.get("auth_param") or "").strip()[:120] or None
    return out


def create_web_source(conn: sqlite3.Connection, cfg: dict) -> int:
    c = clean_web_source(cfg)
    cols = ", ".join(WEB_FIELDS)
    marks = ", ".join("?" for _ in WEB_FIELDS)
    cur = conn.execute(
        f"INSERT INTO web_sources ({cols}) VALUES ({marks})",
        tuple(c[f] for f in WEB_FIELDS),
    )
    return last_row_id(cur)


def update_web_source(conn: sqlite3.Connection, source_id: int, cfg: dict) -> None:
    c = clean_web_source(cfg)
    sets = ", ".join(f"{f} = ?" for f in WEB_FIELDS)
    conn.execute(
        f"UPDATE web_sources SET {sets} WHERE id = ?",
        (*[c[f] for f in WEB_FIELDS], source_id),
    )


def get_web_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM web_sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_web_sources(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    where = "WHERE w.enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT w.*, (SELECT COUNT(*) FROM web_pages p WHERE p.source_id = w.id) AS page_count
        FROM web_sources w {where} ORDER BY w.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_web_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE web_sources SET enabled = 1 - COALESCE(enabled, 0) WHERE id = ?", (source_id,)
    )


def delete_web_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM web_sources WHERE id = ?", (source_id,))


def page_states(conn: sqlite3.Connection, source_id: int) -> dict[str, dict]:
    """``{url: {title, content_hash, text}}`` as of the last committed scan."""
    rows = conn.execute(
        "SELECT url, title, content_hash, text FROM web_pages WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    return {r["url"]: dict(r) for r in rows}


def save_page_states(
    conn: sqlite3.Connection, source_id: int, pages: dict[str, dict], status: str
) -> None:
    """Upsert the pages seen in a scan and stamp the source's scan status."""
    for url, p in pages.items():
        conn.execute(
            """
            INSERT INTO web_pages (source_id, url, title, content_hash, text)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, url) DO UPDATE SET
                title = excluded.title,
                last_seen = datetime('now'),
                last_changed = CASE WHEN web_pages.content_hash != excluded.content_hash
                                    THEN datetime('now') ELSE web_pages.last_changed END,
                content_hash = excluded.content_hash,
                text = excluded.text
            """,
            (source_id, url, (p.get("title") or "")[:300], p["content_hash"],
             (p.get("text") or "")[:PAGE_TEXT_MAX]),
        )
    conn.execute(
        "UPDATE web_sources SET last_scan_at = datetime('now'), last_status = ? WHERE id = ?",
        (status[:300], source_id),
    )
