"""Imported Obsidian vaults (a local folder each; nothing secret)."""

from __future__ import annotations

import sqlite3

from nexus.db import last_row_id

VAULT_FIELDS = ("name", "enabled", "path", "include_folders", "exclude_folders",
                "required_tag", "case_id", "in_pool", "no_ai")


def _flag(value, default: int = 0) -> int:
    if value is None:
        return default
    return 0 if value in (0, "0", False, "", "off", "false") else 1


def clean_obsidian_vault(cfg: dict) -> dict:
    out: dict = {}
    path = (cfg.get("path") or "").strip().strip('"')[:1000]
    out["path"] = path
    leaf = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if path else ""
    out["name"] = (cfg.get("name") or "").strip()[:80] or (
        f"Obsidian: {leaf}" if leaf else "Obsidian vault")
    out["enabled"] = _flag(cfg.get("enabled"), 1)
    out["include_folders"] = (cfg.get("include_folders") or "").strip()[:1000]
    out["exclude_folders"] = (cfg.get("exclude_folders") or "").strip()[:1000]
    out["required_tag"] = (cfg.get("required_tag") or "").strip().lstrip("#")[:100]
    case = str(cfg.get("case_id") or "").strip()
    out["case_id"] = int(case) if case.isdigit() else None
    out["in_pool"] = 1 if out["case_id"] is None else _flag(cfg.get("in_pool"), 0)
    out["no_ai"] = _flag(cfg.get("no_ai"))
    return out


def create_obsidian_vault(conn: sqlite3.Connection, cfg: dict) -> int:
    c = clean_obsidian_vault(cfg)
    cols = ", ".join(VAULT_FIELDS)
    marks = ", ".join("?" for _ in VAULT_FIELDS)
    cur = conn.execute(f"INSERT INTO obsidian_vaults ({cols}) VALUES ({marks})",
                       tuple(c[f] for f in VAULT_FIELDS))
    return last_row_id(cur)


def get_obsidian_vault(conn: sqlite3.Connection, vault_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM obsidian_vaults WHERE id = ?", (vault_id,)).fetchone()
    return dict(row) if row else None


def list_obsidian_vaults(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    where = "WHERE v.enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT v.*, c.name AS case_name,
               (SELECT COUNT(*) FROM connector_docs d
                WHERE d.kind = 'obsidian' AND d.source_id = v.id) AS note_count
        FROM obsidian_vaults v LEFT JOIN cases c ON c.id = v.case_id
        {where} ORDER BY v.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_obsidian_vault(conn: sqlite3.Connection, vault_id: int) -> None:
    conn.execute("UPDATE obsidian_vaults SET enabled = 1 - COALESCE(enabled, 0) WHERE id = ?",
                 (vault_id,))


def delete_obsidian_vault(conn: sqlite3.Connection, vault_id: int) -> None:
    conn.execute("DELETE FROM obsidian_vaults WHERE id = ?", (vault_id,))


def save_obsidian_scan(conn: sqlite3.Connection, vault_id: int, status: str) -> None:
    conn.execute(
        "UPDATE obsidian_vaults SET last_scan_at = datetime('now'), last_status = ? WHERE id = ?",
        (status[:500], vault_id),
    )
