"""Topic subscriptions, query capsules and the scan-term value providers."""

from __future__ import annotations

import sqlite3

from nexus.db import last_row_id
from nexus.storage.cases import all_case_terms


# --- Subscriptions: user-chosen collection topics ---------------------------
# These let the analyst pick *what* to collect from the dashboard instead of
# editing .env. At scan time each source merges its env-configured targets with
# the enabled subscriptions for that source (see config.subscription helpers).
def add_subscription(
    conn: sqlite3.Connection,
    source: str,
    value: str,
    label: str | None = None,
) -> int:
    """Add a collection target. Idempotent on (source, value).

    Re-adding an existing pair re-enables it (a previously removed-by-disable
    target can come back) and refreshes its label.
    """
    cur = conn.execute(
        """
        INSERT INTO subscriptions (source, value, label, enabled)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(source, value) DO UPDATE SET
            enabled = 1,
            label   = COALESCE(excluded.label, subscriptions.label)
        """,
        (source, value, label),
    )
    return last_row_id(cur)


def list_subscriptions(
    conn: sqlite3.Connection, source: str | None = None
) -> list[dict]:
    """All subscriptions (optionally filtered by source), newest first."""
    sql = "SELECT id, source, value, label, enabled, created_at FROM subscriptions"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY source, id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def delete_subscription(conn: sqlite3.Connection, sub_id: int) -> None:
    conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))


def list_query_capsules(conn: sqlite3.Connection) -> list[dict]:
    """Investigation queries grouped into named 'capsules'.

    A capsule is a named bundle of search terms (e.g. "Tesla" -> Tesla, Elon
    Musk). Terms are stored as source='query' subscriptions whose ``label``
    carries the capsule name; terms without a label fall under "General".
    Every term is broadcast to all keyword-capable sources at scan time, so a
    capsule needs no extra collection plumbing. Returned newest-capsule-first.
    """
    rows = conn.execute(
        "SELECT id, value, label, created_at FROM subscriptions "
        "WHERE source = 'query' AND enabled = 1 ORDER BY id DESC"
    ).fetchall()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        name = ((r["label"] or "").strip()) or "General"
        groups.setdefault(name, []).append({"id": r["id"], "value": r["value"]})
    return [{"name": k, "terms": v} for k, v in groups.items()]


def delete_capsule(conn: sqlite3.Connection, name: str) -> None:
    """Remove an entire capsule (all query terms sharing its name)."""
    name = (name or "").strip()
    if name in ("", "General"):
        conn.execute(
            "DELETE FROM subscriptions WHERE source = 'query' "
            "AND (label IS NULL OR TRIM(label) = '' OR label = 'General')"
        )
    else:
        conn.execute(
            "DELETE FROM subscriptions WHERE source = 'query' AND label = ?",
            (name,),
        )


def subscription_values(conn: sqlite3.Connection, source: str) -> list[str]:
    """Enabled target values for a source, on an existing connection."""
    rows = conn.execute(
        "SELECT value FROM subscriptions WHERE source = ? AND enabled = 1 ORDER BY id",
        (source,),
    ).fetchall()
    return [r[0] for r in rows]


def get_subscription_values(source: str) -> list[str]:
    """Enabled target values for a source, opening a short-lived connection.

    Convenience for callers (sources, config) that don't already hold one.
    Never raises: on any storage error it returns an empty list so collection
    falls back to env-only targets (graceful degradation).
    """
    try:
        from nexus.db import get_connection

        with get_connection() as conn:
            return subscription_values(conn, source)
    except Exception:
        return []


def get_case_term_values() -> list[str]:
    """Every case's tracking words, opening a short-lived connection.

    Lets config broadcast case words to keyword-capable sources at scan time, so
    a case's live feed actually fills. Never raises (graceful degradation)."""
    try:
        from nexus.db import get_connection

        with get_connection() as conn:
            return all_case_terms(conn)
    except Exception:
        return []
