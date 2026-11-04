"""Read-only cloud connections (non-secret settings only; the token lives in .env).

One table serves every connector in ``nexus/sources/cloud.py``: ``kind`` says
which, ``account`` holds its non-secret identity (a tenant and client id, for
instance) and ``scope`` the chosen channels / folders / sites.
"""

from __future__ import annotations

import re
import sqlite3

from nexus.db import last_row_id

MAX_ITEMS = 2000
_SCOPE_ITEM = re.compile(r"^[^\s,]{1,300}$")


def _flag(value, default: int = 0) -> int:
    if value is None:
        return default
    return 0 if value in (0, "0", False, "", "off", "false") else 1


def clean_scope(raw) -> str:
    """Comma-separated, de-duplicated ids of the chosen channels / folders."""
    tokens = [str(t) for t in raw] if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if tok and _SCOPE_ITEM.match(tok) and tok not in out:
            out.append(tok)
    return ",".join(out[:200])


def clean_cloud_source(cfg: dict) -> dict:
    out: dict = {
        "kind": str(cfg.get("kind") or "").strip().lower()[:40],
        "enabled": _flag(cfg.get("enabled"), 1),
        "account": str(cfg.get("account") or "").strip()[:500],
        "scope": clean_scope(cfg.get("scope")),
    }
    out["name"] = str(cfg.get("name") or "").strip()[:80] or out["kind"].title()
    try:
        n = int(str(cfg.get("max_items") or 200).strip())
    except ValueError:
        n = 200
    out["max_items"] = max(1, min(n, MAX_ITEMS))
    case_id = str(cfg.get("case_id") or "").strip()
    out["case_id"] = int(case_id) if case_id.isdigit() else None
    out["in_pool"] = 1 if out["case_id"] is None else _flag(cfg.get("in_pool"))
    out["no_ai"] = _flag(cfg.get("no_ai"), 1)  # private data: AI stays off unless allowed
    return out


def create_cloud_source(conn: sqlite3.Connection, cfg: dict) -> int:
    row = clean_cloud_source(cfg)
    cols = list(row)
    cur = conn.execute(
        f"INSERT INTO cloud_sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [row[c] for c in cols],
    )
    return last_row_id(cur)


def get_cloud_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cloud_sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_cloud_sources(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    where = "WHERE s.enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT s.*, c.name AS case_name,
               (SELECT COUNT(*) FROM connector_docs d
                WHERE d.kind = s.kind AND d.source_id = s.id) AS doc_count
        FROM cloud_sources s LEFT JOIN cases c ON c.id = s.case_id
        {where} ORDER BY s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_cloud_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE cloud_sources SET enabled = 1 - COALESCE(enabled, 0) WHERE id = ?",
                 (source_id,))


def delete_cloud_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM cloud_sources WHERE id = ?", (source_id,))


def set_cloud_scope(conn: sqlite3.Connection, source_id: int, scope) -> None:
    """Save the chosen scope. Its watermark restarts so new choices are read in full."""
    conn.execute("UPDATE cloud_sources SET scope = ?, watermark = NULL WHERE id = ?",
                 (clean_scope(scope), source_id))


def save_cloud_scan(conn: sqlite3.Connection, source_id: int, status: str,
                    watermark: str | None) -> None:
    conn.execute(
        "UPDATE cloud_sources SET last_scan_at = datetime('now'), last_status = ?, "
        "watermark = COALESCE(?, watermark) WHERE id = ?",
        (status[:300], watermark, source_id),
    )
