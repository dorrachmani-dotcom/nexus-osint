"""Entity co-occurrence graph and single-entity profiles."""

from __future__ import annotations

import sqlite3

from nexus.storage.entities import (
    _clean_entity_display,
    _decode_entities,
    _entity_kind_map,
    _entity_norm_set,
    _norm_entity_key,
    get_entity_aliases,
)
from nexus.storage.feed import enrich_feed_rows
from nexus.storage.items import search_items


def topic_entity_graph(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    since_ts: str | None = None,
    terms: list[str] | None = None,
    extra_item_ids: list[int] | None = None,
    scan_limit: int = 600,
    max_nodes: int = 60,
) -> dict:
    """Aggregate AI-extracted entities across items matching the feed filters.

    Builds a co-occurrence network: every entity the AI pulled out of an item is
    a node; two entities sharing an item are linked. Node weight is how many
    items mention the entity ("most talked-about"); an entity's number of
    distinct co-occurring partners is its connectedness ("most connected").

    Returns a JSON-serialisable dict::

        {
          "nodes": [{"id","label","kind","mentions","connections"}...],
          "edges": [{"source","target","weight"}...],
          "most_mentioned": [{"name","kind","mentions"}...],   # ranked
          "most_connected": [{"name","kind","connections"}...],# ranked
          "item_count": int,        # items scanned
          "entity_count": int,      # distinct entities before the node cap
        }

    Entirely derived from already-stored ``analyses.entities`` — no new schema,
    no AI calls. Names are de-duplicated case-insensitively across items.
    """
    # "Explicit-items-only" mode: when the caller gives a fixed item set but no
    # terms/keyword (e.g. a case curated purely by pinning, with no tracking
    # words), graph ONLY those items — do NOT fall back to the global feed.
    if extra_item_ids is not None and not terms and not q:
        rows = []
    else:
        rows = search_items(
            conn, q=q, source=source, threat=threat, since=since, until=until,
            since_ts=since_ts, terms=terms, limit=scan_limit,
        )
    # Merge in explicitly-named items (e.g. a case's PINNED dossier) that the
    # term/keyword search didn't already cover, so a case graph reflects what the
    # analyst actually curated — not only what its tracking words happen to match.
    if extra_item_ids:
        have = {r["id"] for r in rows}
        missing = [int(i) for i in extra_item_ids if int(i) not in have]
        if missing:
            ph = ",".join("?" * len(missing))
            extra = conn.execute(
                f"SELECT i.id, i.source, i.url, a.entities "
                f"FROM items i LEFT JOIN analyses a ON a.item_id = i.id "
                f"WHERE i.id IN ({ph})",
                missing,
            ).fetchall()
            rows.extend(dict(r) for r in extra)

    # Load analyst-defined aliases once: "Messi" → "Lionel Messi".
    # alias_map: raw_low → canonical_low
    # alias_display: raw_low → canonical display string (for first-seen assignment)
    alias_map: dict[str, str] = {}
    alias_display: dict[str, str] = {}
    for alias in get_entity_aliases(conn):
        raw_low = _norm_entity_key(alias["alias"])
        can_low = _norm_entity_key(alias["canonical"])
        if raw_low and can_low:
            alias_map[raw_low] = can_low
            alias_display[raw_low] = alias["canonical"]

    mentions: dict[str, int] = {}        # low -> item count
    display: dict[str, str] = {}         # low -> first-seen display name
    kinds: dict[str, str] = {}           # low -> kind label
    partners: dict[str, set[str]] = {}   # low -> set of co-occurring lows
    edge_weight: dict[tuple[str, str], int] = {}

    items_with_entities = 0
    for row in rows:
        groups, flat = _decode_entities(row.get("entities"))
        if not flat:
            continue
        items_with_entities += 1
        kind_map = _entity_kind_map(groups)

        # De-duplicate this item's entities by their canonical key, so quote/
        # punctuation variants of the same name count as one. Aliases are
        # remapped to their canonical before counting, so "Messi" and "Lionel
        # Messi" merge into a single node.
        lows: list[str] = []
        seen_here: set[str] = set()
        for name in flat:
            raw_low = _norm_entity_key(name)
            if not raw_low:
                continue
            low = alias_map.get(raw_low, raw_low)
            if low not in display:
                display[low] = alias_display.get(raw_low) or _clean_entity_display(name)
                kinds[low] = kind_map.get(raw_low, kind_map.get(low, "entity"))
            if low not in seen_here:
                seen_here.add(low)
                mentions[low] = mentions.get(low, 0) + 1
                lows.append(low)

        # Co-occurrence edges for every unordered pair in this item.
        for i in range(len(lows)):
            for j in range(i + 1, len(lows)):
                a, b = lows[i], lows[j]
                if a == b:
                    continue
                partners.setdefault(a, set()).add(b)
                partners.setdefault(b, set()).add(a)
                key = (a, b) if a < b else (b, a)
                edge_weight[key] = edge_weight.get(key, 0) + 1

    entity_count = len(mentions)

    # Keep the busiest entities so the rendered graph stays legible.
    top_lows = sorted(mentions, key=lambda k: (-mentions[k], k))[:max_nodes]
    top_set = set(top_lows)

    nodes = [
        {
            "id": display[low],
            "label": display[low],
            "kind": kinds.get(low, "entity"),
            "mentions": mentions[low],
            "connections": len(partners.get(low, ())),
        }
        for low in top_lows
    ]
    edges = [
        {"source": display[a], "target": display[b], "weight": w}
        for (a, b), w in edge_weight.items()
        if a in top_set and b in top_set
    ]

    most_mentioned = [
        {"name": display[low], "kind": kinds.get(low, "entity"),
         "mentions": mentions[low]}
        for low in sorted(mentions, key=lambda k: (-mentions[k], k))[:12]
    ]
    most_connected = [
        {"name": display[low], "kind": kinds.get(low, "entity"),
         "connections": len(partners.get(low, ()))}
        for low in sorted(
            partners, key=lambda k: (-len(partners[k]), k)
        )[:12]
        if len(partners.get(low, ())) > 0
    ]

    # Strongest relationships: the entity pairs that co-occur most often. This is
    # the "who appears alongside whom" view an analyst reads first — the raw
    # mention/connection counts say who is busy, but the heaviest edges say which
    # links are real. Ranked by co-occurrence weight, then alphabetically for a
    # stable order. Derived straight from edge_weight; no extra passes.
    top_relationships = [
        {
            "source": display[a], "target": display[b],
            "source_kind": kinds.get(a, "entity"),
            "target_kind": kinds.get(b, "entity"),
            "weight": w,
        }
        for (a, b), w in sorted(
            edge_weight.items(), key=lambda kv: (-kv[1], kv[0])
        )[:12]
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "most_mentioned": most_mentioned,
        "most_connected": most_connected,
        "top_relationships": top_relationships,
        "item_count": items_with_entities,
        "entity_count": entity_count,
    }


# Page size for the entity dossier's item list.
ENTITY_PAGE_SIZE = 50


def entity_profile(
    conn: sqlite3.Connection, name: str, *, offset: int = 0, limit: int = ENTITY_PAGE_SIZE
) -> dict | None:
    """Everything known locally about one entity, for its dossier page.

    Returns a dict with identity + stats, a paginated list of the items that
    mention it (enriched feed rows), the entities it co-occurs with most, and the
    cases it appears in. Returns ``None`` only for an empty name. An entity with
    no items yields a populated dict with ``item_count == 0`` so the page can show
    a graceful empty state. Driven by the ``item_entities`` index, so it stays
    fast even on a large archive.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    canonical, norms = _entity_norm_set(conn, raw)
    if not norms:
        return None
    ph = ",".join("?" * len(norms))
    params = list(norms)

    stat = conn.execute(
        f"""
        SELECT COUNT(DISTINCT ie.item_id) AS item_count,
               COUNT(DISTINCT i.source)   AS source_count,
               MIN(COALESCE(i.published_at, i.fetched_at)) AS first_seen,
               MAX(COALESCE(i.published_at, i.fetched_at)) AS last_seen
        FROM item_entities ie JOIN items i ON i.id = ie.item_id
        WHERE ie.name_norm IN ({ph})
        """,
        params,
    ).fetchone()
    item_count = int(stat["item_count"]) if stat else 0

    # Best display form + kind (most frequent spelling wins).
    disp = conn.execute(
        f"""SELECT name, kind, COUNT(*) c FROM item_entities
            WHERE name_norm IN ({ph}) GROUP BY name_norm
            ORDER BY c DESC LIMIT 1""",
        params,
    ).fetchone()
    display = canonical or (disp["name"] if disp else raw)
    kind = disp["kind"] if disp else None

    if item_count == 0:
        return {
            "name": display, "kind": kind, "item_count": 0, "source_count": 0,
            "first_seen": None, "last_seen": None, "items": [],
            "connections": [], "cases": [], "aliases": sorted(n for n in norms),
            "offset": 0, "has_more": False, "next_offset": 0,
        }

    item_rows = conn.execute(
        f"""
        SELECT i.id, i.source, i.url, i.author, i.title, i.content, i.language,
               i.published_at, i.fetched_at, i.cluster_id,
               COALESCE(i.dismissed, 0) AS dismissed,
               COALESCE(c.shared_count, 1) AS shared_count,
               a.threat_level, a.summary, a.translation, a.target_lang,
               a.entities, a.party, a.contradiction, a.confidence
        FROM item_entities ie
        JOIN items i ON i.id = ie.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE ie.name_norm IN ({ph})
        GROUP BY i.id
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    items = [dict(r) for r in item_rows]
    enrich_feed_rows(conn, items)

    connections = [
        {"name": r["name"], "kind": r["kind"], "shared": int(r["shared"])}
        for r in conn.execute(
            f"""
            SELECT other.name AS name, other.kind AS kind,
                   COUNT(DISTINCT other.item_id) AS shared
            FROM item_entities me
            JOIN item_entities other
              ON other.item_id = me.item_id AND other.name_norm NOT IN ({ph})
            WHERE me.name_norm IN ({ph})
            GROUP BY other.name_norm
            ORDER BY shared DESC, name
            LIMIT 15
            """,
            [*params, *params],
        ).fetchall()
    ]

    cases = [
        {"id": int(r["id"]), "name": r["name"]}
        for r in conn.execute(
            f"""
            SELECT DISTINCT c.id, c.name
            FROM item_entities ie
            JOIN bookmarks b ON b.item_id = ie.item_id AND b.case_id IS NOT NULL
            JOIN cases c ON c.id = b.case_id
            WHERE ie.name_norm IN ({ph})
            ORDER BY c.id DESC
            """,
            params,
        ).fetchall()
    ]

    next_offset = offset + len(items)
    return {
        "name": display,
        "kind": kind,
        "item_count": item_count,
        "source_count": int(stat["source_count"]),
        "first_seen": stat["first_seen"],
        "last_seen": stat["last_seen"],
        "items": items,
        "connections": connections,
        "cases": cases,
        "aliases": sorted(n for n in norms),
        "offset": offset,
        "has_more": next_offset < item_count,
        "next_offset": next_offset,
    }
