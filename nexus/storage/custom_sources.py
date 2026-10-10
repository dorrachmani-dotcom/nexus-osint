"""User-defined custom API sources and their health records."""

from __future__ import annotations

import json
import sqlite3

from nexus.db import last_row_id

# --------------------------------------------------------------- custom sources
# User-defined API sources (see db.py custom_sources). Secrets never live here —
# only the non-secret connection config. The fields mirror the table columns.
_CUSTOM_FIELDS = (
    "name", "enabled", "base_url", "endpoint", "http_method", "auth_type",
    "auth_param", "query_param", "extra_params", "items_path",
    "map_title", "map_content", "map_url", "map_author", "map_published", "notes",
)


_CUSTOM_AUTH_TYPES = {"none", "header", "query", "bearer"}


_CUSTOM_METHODS = {"GET", "POST"}


def _clean_custom(cfg: dict) -> dict:
    """Normalize an incoming config dict to safe, storable values."""
    out: dict = {}
    out["name"] = (cfg.get("name") or "").strip()[:80] or "Custom source"
    out["enabled"] = 1 if cfg.get("enabled", 1) else 0
    out["base_url"] = (cfg.get("base_url") or "").strip()[:500]
    out["endpoint"] = (cfg.get("endpoint") or "").strip()[:500]
    method = (cfg.get("http_method") or "GET").strip().upper()
    out["http_method"] = method if method in _CUSTOM_METHODS else "GET"
    auth = (cfg.get("auth_type") or "none").strip().lower()
    out["auth_type"] = auth if auth in _CUSTOM_AUTH_TYPES else "none"
    out["auth_param"] = (cfg.get("auth_param") or "").strip()[:120] or None
    out["query_param"] = (cfg.get("query_param") or "").strip()[:120] or None
    # extra_params may arrive as a dict or a JSON string; store canonical JSON.
    extra = cfg.get("extra_params")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except (ValueError, TypeError):
            extra = {}
    if not isinstance(extra, dict):
        extra = {}
    out["extra_params"] = json.dumps(extra)
    out["items_path"] = (cfg.get("items_path") or "").strip()[:200]
    for key in ("map_title", "map_content", "map_url", "map_author", "map_published"):
        out[key] = (cfg.get(key) or "").strip()[:200] or None
    out["notes"] = (cfg.get("notes") or "").strip()[:4000] or None
    return out


def create_custom_source(conn: sqlite3.Connection, cfg: dict) -> int:
    c = _clean_custom(cfg)
    cols = ", ".join(_CUSTOM_FIELDS)
    placeholders = ", ".join("?" for _ in _CUSTOM_FIELDS)
    cur = conn.execute(
        f"INSERT INTO custom_sources ({cols}) VALUES ({placeholders})",
        tuple(c[f] for f in _CUSTOM_FIELDS),
    )
    return last_row_id(cur)


def update_custom_source(conn: sqlite3.Connection, source_id: int, cfg: dict) -> None:
    c = _clean_custom(cfg)
    assignments = ", ".join(f"{f} = ?" for f in _CUSTOM_FIELDS)
    # A manual edit means the analyst has (re)defined the mapping, so clear any
    # accumulated Auto-Adapt drift — start its health fresh.
    conn.execute(
        f"UPDATE custom_sources SET {assignments}, consecutive_empty = 0 WHERE id = ?",
        (*[c[f] for f in _CUSTOM_FIELDS], source_id),
    )


def toggle_custom_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE custom_sources SET enabled = 1 - COALESCE(enabled, 0) WHERE id = ?",
        (source_id,),
    )


def delete_custom_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM custom_sources WHERE id = ?", (source_id,))


def get_custom_source(conn: sqlite3.Connection, source_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM custom_sources WHERE id = ?", (source_id,)
    ).fetchone()
    return dict(row) if row else None


def list_custom_sources(
    conn: sqlite3.Connection, enabled_only: bool = False
) -> list[dict]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"SELECT * FROM custom_sources {where} ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


# --- Auto-Adapt health (see nexus/sources/auto_adapt.py) --------------------
# A custom API can keep responding (HTTP 200, valid JSON) while its field
# mapping silently drifts — yielding zero items. We track consecutive
# "responsive-but-empty" scans; once they cross a threshold the AI planner
# re-maps the source from the latest raw sample. A successful (non-empty) scan,
# or any manual edit, resets the counter.
_SAMPLE_MAX_CHARS = 8000


def record_custom_source_health(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    responded: bool,
    mapped: int,
    sample: str | None = None,
) -> int:
    """Update a custom source's drift counter after a scan; return the new count.

    Increments ``consecutive_empty`` only when the API *responded* but mapped
    zero items (the drift signal). A network/HTTP failure (``responded=False``)
    leaves the counter untouched — transient outages must not trigger adapts.
    Any successful (mapped > 0) scan resets it to 0. ``sample`` (the latest raw
    response JSON) is stored so the planner can re-map offline.
    """
    if mapped > 0:
        conn.execute(
            "UPDATE custom_sources SET consecutive_empty = 0 WHERE id = ?",
            (source_id,),
        )
        return 0
    if not responded:
        row = conn.execute(
            "SELECT consecutive_empty FROM custom_sources WHERE id = ?", (source_id,)
        ).fetchone()
        return int(row["consecutive_empty"]) if row else 0
    # Responded but mapped nothing: bump the counter and stash the sample.
    clipped = (sample or "")[:_SAMPLE_MAX_CHARS] or None
    conn.execute(
        "UPDATE custom_sources SET consecutive_empty = consecutive_empty + 1, "
        "last_sample = COALESCE(?, last_sample) WHERE id = ?",
        (clipped, source_id),
    )
    row = conn.execute(
        "SELECT consecutive_empty FROM custom_sources WHERE id = ?", (source_id,)
    ).fetchone()
    return int(row["consecutive_empty"]) if row else 0


def apply_custom_source_remap(
    conn: sqlite3.Connection, source_id: int, mapping: dict
) -> None:
    """Apply ONLY the response-mapping fields from an Auto-Adapt re-plan.

    Auth, base_url, endpoint and the analyst's other connection settings are
    preserved — Auto-Adapt fixes *where the data lives in the response*, never
    how we reach or authenticate to the API. Also resets the drift counter and
    stamps ``last_adapt_at``.
    """
    fields = ("items_path", "map_title", "map_content", "map_url",
              "map_author", "map_published")
    sets = []
    params: list = []
    for f in fields:
        if f in mapping:
            val = mapping[f]
            val = (str(val).strip()[:200] or None) if val not in (None, "") else None
            sets.append(f"{f} = ?")
            params.append(val)
    sets.append("consecutive_empty = 0")
    sets.append("last_adapt_at = datetime('now')")
    conn.execute(
        f"UPDATE custom_sources SET {', '.join(sets)} WHERE id = ?",
        (*params, source_id),
    )
