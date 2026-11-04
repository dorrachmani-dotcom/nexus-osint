"""Confluence connections (non-secret settings only; the token lives in .env)."""

from __future__ import annotations

import re
import sqlite3

from nexus.db import last_row_id

CONFLUENCE_FIELDS = (
    "name", "enabled", "deployment", "base_url", "email", "spaces", "cql",
    "include_blogposts", "include_comments", "include_attachments", "max_items",
    "tls_mode", "case_id", "in_pool", "no_ai",
)
DEPLOYMENTS = {"cloud", "dc"}
TLS_MODES = {"verify", "ca_bundle", "insecure"}
MAX_ITEMS = 2000
_SPACE_KEY = re.compile(r"^[A-Za-z0-9_~.-]{1,255}$")


def _flag(value, default: int = 0) -> int:
    if value is None:
        return default
    return 0 if value in (0, "0", False, "", "off", "false") else 1


def clean_space_keys(raw) -> str:
    """Comma-separated, de-duplicated, valid Confluence space keys."""
    if isinstance(raw, (list, tuple)):
        tokens = [str(t) for t in raw]
    else:
        tokens = str(raw or "").replace(";", ",").replace(" ", ",").split(",")
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if tok and _SPACE_KEY.match(tok) and tok not in out:
            out.append(tok)
    return ",".join(out[:100])


def clean_confluence_source(cfg: dict) -> dict:
    out: dict = {}
    base = (cfg.get("base_url") or "").strip().rstrip("/")[:500]
    dep = (cfg.get("deployment") or "cloud").strip().lower()
    out["deployment"] = dep if dep in DEPLOYMENTS else "cloud"
    if out["deployment"] == "cloud" and base.lower().endswith("/wiki"):
        base = base[:-5]  # the site URL; /wiki is added by the client
    out["base_url"] = base
    host = base.split("://", 1)[-1].split("/", 1)[0] if base else ""
    out["name"] = (cfg.get("name") or "").strip()[:80] or (
        f"Confluence: {host}" if host else "Confluence")
    out["enabled"] = _flag(cfg.get("enabled"), 1)
    out["email"] = (cfg.get("email") or "").strip()[:200] or None
    out["spaces"] = clean_space_keys(cfg.get("spaces"))
    out["cql"] = (cfg.get("cql") or "").strip()[:2000]
    out["include_blogposts"] = _flag(cfg.get("include_blogposts"))
    out["include_comments"] = _flag(cfg.get("include_comments"))
    out["include_attachments"] = _flag(cfg.get("include_attachments"))
    try:
        n = int(str(cfg.get("max_items") or 200).strip())
    except ValueError:
        n = 200
    out["max_items"] = max(1, min(MAX_ITEMS, n))
    tls = (cfg.get("tls_mode") or "verify").strip().lower()
    out["tls_mode"] = tls if tls in TLS_MODES else "verify"
    case = str(cfg.get("case_id") or "").strip()
    out["case_id"] = int(case) if case.isdigit() else None
    out["in_pool"] = 1 if out["case_id"] is None else _flag(cfg.get("in_pool"), 0)
    out["no_ai"] = _flag(cfg.get("no_ai"))
    return out


def create_confluence_source(conn: sqlite3.Connection, cfg: dict) -> int:
    c = clean_confluence_source(cfg)
    cols = ", ".join(CONFLUENCE_FIELDS)
    marks = ", ".join("?" for _ in CONFLUENCE_FIELDS)
    cur = conn.execute(
        f"INSERT INTO confluence_sources ({cols}) VALUES ({marks})",
        tuple(c[f] for f in CONFLUENCE_FIELDS),
    )
    return last_row_id(cur)


def update_confluence_source(conn: sqlite3.Connection, source_id: int, cfg: dict) -> None:
    c = clean_confluence_source(cfg)
    sets = ", ".join(f"{f} = ?" for f in CONFLUENCE_FIELDS)
    conn.execute(f"UPDATE confluence_sources SET {sets} WHERE id = ?",
                 (*[c[f] for f in CONFLUENCE_FIELDS], source_id))


def set_confluence_spaces(conn: sqlite3.Connection, source_id: int, spaces) -> None:
    conn.execute("UPDATE confluence_sources SET spaces = ? WHERE id = ?",
                 (clean_space_keys(spaces), source_id))


def get_confluence_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM confluence_sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_confluence_sources(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    where = "WHERE s.enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT s.*, c.name AS case_name,
               (SELECT COUNT(*) FROM connector_docs d
                WHERE d.kind = 'confluence' AND d.source_id = s.id) AS page_count
        FROM confluence_sources s LEFT JOIN cases c ON c.id = s.case_id
        {where} ORDER BY s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_confluence_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE confluence_sources SET enabled = 1 - COALESCE(enabled, 0) WHERE id = ?",
                 (source_id,))


def delete_confluence_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM confluence_sources WHERE id = ?", (source_id,))


def cached_user_names(conn: sqlite3.Connection, source_id: int,
                      max_age_days: int) -> dict[str, tuple[str, bool]]:
    """``{account_id: (name, is_fresh)}`` for one connection."""
    rows = conn.execute(
        "SELECT account_id, name, fetched_at >= datetime('now', ?) AS fresh "
        "FROM confluence_users WHERE source_id = ?",
        (f"-{int(max_age_days)} days", source_id),
    ).fetchall()
    return {r["account_id"]: (r["name"], bool(r["fresh"])) for r in rows}


def save_user_name(conn: sqlite3.Connection, source_id: int, account_id: str,
                   name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO confluence_users (source_id, account_id, name) "
        "VALUES (?, ?, ?)",
        (source_id, account_id, name),
    )


def save_confluence_scan(conn: sqlite3.Connection, source_id: int, status: str,
                         watermark: str | None) -> None:
    conn.execute(
        "UPDATE confluence_sources SET last_scan_at = datetime('now'), last_status = ?, "
        "watermark = COALESCE(?, watermark) WHERE id = ?",
        (status[:300], watermark, source_id),
    )
