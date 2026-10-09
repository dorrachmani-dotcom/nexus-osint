"""Feed presentation: row enrichment (reliability, relative time, archive
state), single-item lookup, the attention queue and threat counters.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC

from nexus.storage.archives import item_archives_map
from nexus.storage.entities import _decode_entities

# Substrings (in a source name) that hint at a reliability tier. Matched
# case-insensitively against the logical source/feed name.
_OFFICIAL_HINTS = (
    "reuters", "associated press", "ap news", "bbc", "bloomberg", "guardian",
    "nyt", "new york times", "washington post", "wsj", "wall street journal",
    "cnn", "npr", "financial times", "ft.com", "al jazeera", "gov", "official",
)


_SOCIAL_HINTS = ("twitter", "x.com", "telegram", "reddit", "mastodon", "tiktok",
                 "instagram", "facebook", "youtube", "social")


_AGG_HINTS = ("serpapi", "google news", "search", "aggregator", "rss")


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
        host = host.split("/")[0].split("?")[0]
        return host[4:] if host.lower().startswith("www.") else host or None
    except Exception:
        return None


def _classify_reliability(source: str | None, url: str | None) -> str:
    """Coarse reliability tier from the source name / URL host (heuristic)."""
    blob = f"{source or ''} {url or ''}".lower()
    if any(h in blob for h in _OFFICIAL_HINTS):
        return "official"
    if any(h in blob for h in _SOCIAL_HINTS):
        return "social"
    if any(h in blob for h in _AGG_HINTS):
        return "aggregator"
    return "unverified"


def _relative_time(iso: str | None) -> str | None:
    """Human 'just now / 5m / 3h / 2d / 4w ago' from an ISO timestamp."""
    if not iso:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 52:
        return f"{weeks}w ago"
    return f"{days // 365}y ago"


# --- "Needs your eyes" triage: surface the few items that actually matter -----
# Ranks UNREAD items by a signal computed from signals the AI already produced —
# no extra model calls. Non-destructive: it only ranks/filters; nothing is hidden
# from the main feed.
_THREAT_SIGNAL = {"critical": 100, "high": 80, "medium": 50, "low": 20, "none": 0}


ATTENTION_MIN_SIGNAL = 40


ATTENTION_LIMIT = 15


def _attention_scored(
    conn: sqlite3.Connection, min_signal: int
) -> list[tuple[int, dict, list[str]]]:
    """Score the unread pool; return (signal, raw_row, reasons) above threshold."""
    pool = conn.execute(
        """
        SELECT i.id, i.source, i.url, i.author, i.title, i.content, i.language,
               i.published_at, i.fetched_at, i.cluster_id,
               COALESCE(i.dismissed, 0) AS dismissed,
               COALESCE(c.shared_count, 1) AS shared_count,
               a.threat_level, a.summary, a.translation, a.target_lang,
               a.entities, a.party, a.contradiction, a.confidence,
               (SELECT MAX(score) FROM requirement_hits rh WHERE rh.item_id = i.id) AS req_score,
               (SELECT rq.question FROM requirement_hits rh
                  JOIN requirements rq ON rq.id = rh.requirement_id
                  WHERE rh.item_id = i.id ORDER BY rh.score DESC LIMIT 1) AS top_question,
               (SELECT 1 FROM watchlist_hits wh WHERE wh.item_id = i.id LIMIT 1) AS wl_hit
        FROM items i
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE COALESCE(i.dismissed, 0) = 0
          AND i.id NOT IN (SELECT item_id FROM item_reads)
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT 2000
        """
    ).fetchall()

    out: list[tuple[int, dict, list[str]]] = []
    for row in pool:
        r = dict(row)
        req = int(r.get("req_score") or 0)
        threat = r.get("threat_level") or "none"
        contradiction = int(r.get("contradiction") or 0)
        wl = int(r.get("wl_hit") or 0)
        signal = (
            max(req, _THREAT_SIGNAL.get(threat, 0))
            + (40 if wl else 0)
            + (25 if contradiction else 0)
        )
        if signal < min_signal:
            continue
        reasons: list[str] = []
        if req >= 40:
            q = (r.get("top_question") or "").strip()
            reasons.append(
                f"Answers “{q[:60]}” · {req}/100" if q
                else f"Answers a standing question · {req}/100"
            )
        if threat in ("high", "critical"):
            reasons.append(f"{threat} threat")
        if contradiction:
            reasons.append("Disputed / possible disinformation")
        if wl:
            reasons.append("Matches a watchlist")
        out.append((signal, r, reasons))
    out.sort(key=lambda s: (-s[0], -(s[1]["id"] or 0)))
    return out


def attention_items(
    conn: sqlite3.Connection,
    limit: int = ATTENTION_LIMIT,
    min_signal: int = ATTENTION_MIN_SIGNAL,
) -> list[dict]:
    """The few unread items that most warrant attention, enriched, with reasons.

    Each row carries a ``signal`` and a human ``reasons`` list explaining why it
    surfaced (so the ranking is never a black box)."""
    top = _attention_scored(conn, min_signal)[:limit]
    items = [r for _, r, _ in top]
    enrich_feed_rows(conn, items)
    for (signal, _row, reasons), it in zip(top, items, strict=True):
        if it.get("source_count", 1) >= 2:
            reasons = [*reasons, f"Confirmed by {it['source_count']} sources"]
        it["signal"] = signal
        it["reasons"] = reasons
    return items


def attention_count(conn: sqlite3.Connection, min_signal: int = ATTENTION_MIN_SIGNAL) -> int:
    """How many unread items clear the attention threshold (for the nav badge)."""
    try:
        return len(_attention_scored(conn, min_signal))
    except Exception:
        return 0


def enrich_feed_rows(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Augment feed rows in-place with derived presentation fields. Batched."""
    if not rows:
        return rows

    ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    id_ph = ",".join("?" * len(ids)) if ids else ""

    # Evidence-captured flags (one query for the whole page).
    evidence_ids: set[int] = set()
    read_ids: set[int] = set()
    bookmarked_ids: set[int] = set()
    membership: dict[int, list[dict]] = {}
    case_membership: dict[int, list[dict]] = {}
    if ids:
        evidence_ids = {
            int(r[0])
            for r in conn.execute(
                f"SELECT DISTINCT item_id FROM evidence WHERE item_id IN ({id_ph})",
                ids,
            ).fetchall()
        }
        read_ids = {
            int(r[0])
            for r in conn.execute(
                f"SELECT item_id FROM item_reads WHERE item_id IN ({id_ph})", ids
            ).fetchall()
        }
        bookmarked_ids = {
            int(r[0])
            for r in conn.execute(
                f"SELECT DISTINCT item_id FROM bookmarks WHERE item_id IN ({id_ph})",
                ids,
            ).fetchall()
        }
        for r in conn.execute(
            f"""
            SELECT m.item_id, l.id, l.name, l.color
            FROM list_memberships m
            JOIN lists l ON l.id = m.list_id
            WHERE m.item_id IN ({id_ph})
            ORDER BY l.position, l.id
            """,
            ids,
        ).fetchall():
            membership.setdefault(int(r["item_id"]), []).append(
                {"id": int(r["id"]), "name": r["name"], "color": r["color"]}
            )
        # Which investigation cases each item is pinned into.
        for r in conn.execute(
            f"""
            SELECT DISTINCT b.item_id, c.id, c.name,
                   COALESCE(c.priority, 'medium') AS priority,
                   COALESCE(c.status, 'open') AS status
            FROM bookmarks b
            JOIN cases c ON c.id = b.case_id
            WHERE b.item_id IN ({id_ph}) AND b.case_id IS NOT NULL
            ORDER BY c.id DESC
            """,
            ids,
        ).fetchall():
            case_membership.setdefault(int(r["item_id"]), []).append(
                {
                    "id": int(r["id"]),
                    "name": r["name"],
                    "priority": r["priority"],
                    "status": r["status"],
                }
            )

    # Internet Archive captures (latest attempt per item).
    archives: dict[int, dict] = item_archives_map(conn, ids) if ids else {}

    # Related sources: other items sharing a cluster_id with any row on the page.
    cluster_ids = sorted({r["cluster_id"] for r in rows if r.get("cluster_id")})
    related_by_cluster: dict[str | None, list[dict]] = {}
    if cluster_ids:
        cl_ph = ",".join("?" * len(cluster_ids))
        for r in conn.execute(
            f"""
            SELECT id, cluster_id, source, url, title
            FROM items
            WHERE cluster_id IN ({cl_ph})
            ORDER BY COALESCE(published_at, fetched_at) DESC
            """,
            cluster_ids,
        ).fetchall():
            related_by_cluster.setdefault(r["cluster_id"], []).append(dict(r))

    # Best intelligence-requirement match per item (highest score).
    top_req: dict[int, dict] = {}
    if ids:
        for r in conn.execute(
            f"""
            SELECT h.item_id, h.score, h.rationale, rq.question
            FROM requirement_hits h
            JOIN requirements rq ON rq.id = h.requirement_id
            WHERE h.item_id IN ({id_ph})
            ORDER BY h.score DESC
            """,
            ids,
        ).fetchall():
            if r["item_id"] not in top_req:
                top_req[int(r["item_id"])] = {
                    "question": r["question"],
                    "score": int(r["score"]),
                    "rationale": r["rationale"],
                }

    for r in rows:
        typed, flat = _decode_entities(r.get("entities"))
        r["entities_typed"] = typed
        r["entities_flat"] = flat
        r["evidence_captured"] = int(r["id"]) in evidence_ids if r.get("id") else False
        r["is_read"] = int(r["id"]) in read_ids if r.get("id") else False
        r["is_bookmarked"] = int(r["id"]) in bookmarked_ids if r.get("id") else False
        r["lists"] = membership.get(int(r["id"]), []) if r.get("id") else []
        r["cases"] = case_membership.get(int(r["id"]), []) if r.get("id") else []
        r["reliability"] = _classify_reliability(r.get("source"), r.get("url"))
        arch = archives.get(int(r["id"])) if r.get("id") else None
        r["archive"] = arch
        if arch and arch.get("status") in ("done", "existing"):
            r["archive_url"] = arch.get("archive_url") or ""
            r["archived_at"] = arch.get("archived_at") or ""
        else:
            r["archive_url"] = r["archived_at"] = ""
        r["display_url"] = _host(r.get("url"))
        r["relative_time"] = _relative_time(r.get("published_at") or r.get("fetched_at"))
        # Related = same-cluster items other than this one (dedup by source+host).
        related: list[dict] = []
        seen_pairs: set[tuple] = set()
        for other in related_by_cluster.get(r.get("cluster_id"), []):
            if other["id"] == r.get("id"):
                continue
            key = (other.get("source"), _host(other.get("url")))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            related.append(
                {
                    "source": other.get("source"),
                    "url": other.get("url"),
                    "title": other.get("title"),
                    "host": _host(other.get("url")),
                }
            )
            if len(related) >= 5:
                break
        r["related"] = related
        # Cross-source corroboration: how many DISTINCT sources carry this story.
        # 2+ independent sources => more trustworthy; 1 => treat with more caution.
        cluster_items = related_by_cluster.get(r.get("cluster_id"), [])
        sources = {it.get("source") for it in cluster_items if it.get("source")}
        if r.get("source"):
            sources.add(r["source"])
        r["source_count"] = len(sources) or 1
        r["corroborated"] = r["source_count"] >= 2
        # Don't clobber the intel view's own requirement columns (rel_score etc.).
        if "rel_score" not in r and int(r["id"]) in top_req:
            r["top_requirement"] = top_req[int(r["id"])]
    return rows


def feed_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """One enriched feed row by id (for re-rendering a single card after an action)."""
    row = conn.execute(
        """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(i.dismissed, 0) AS dismissed,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM items i
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    rows = [dict(row)]
    enrich_feed_rows(conn, rows)
    return rows[0]


def threat_counts_since(conn: sqlite3.Connection, since_ts: str) -> dict[str, int]:
    """Items collected since ``since_ts`` grouped by AI threat level.

    Unanalysed items are counted under "unscored". Dismissed items are excluded.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(a.threat_level, 'unscored') AS level, COUNT(*) AS n
        FROM items i LEFT JOIN analyses a ON a.item_id = i.id
        WHERE i.fetched_at >= ? AND COALESCE(i.dismissed, 0) = 0
        GROUP BY level
        """,
        (since_ts,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}
