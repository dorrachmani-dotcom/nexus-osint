"""Persistence helpers between the collector and SQLite.

`upsert_item` is the single write path. It enforces idempotent re-scans (the
same post fetched twice is ignored) while still counting genuine echoes — a
*different* post carrying identical content bumps the cluster's shared_count,
which drives the "🔥 Echoed N times" badge.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from nexus.models import RawItem

logger = logging.getLogger("nexus.storage")

# Unicode word characters: \w already covers every script (Latin, Cyrillic,
# CJK, and more) under re.UNICODE, so search tokenizes any language.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# ReDoS guardrails for user-authored watchlist regexes. We can't portably
# interrupt a running regex (no SIGALRM on Windows), so we bound the two inputs
# that drive catastrophic backtracking instead: the pattern length and the
# length of the text it runs against. A few KB of haystack keeps even a
# pathological pattern's worst case fast, and no real keyword/regex needs to be
# hundreds of characters long.
_MAX_REGEX_PATTERN_LEN = 500
_MAX_REGEX_HAYSTACK = 8192


def build_fts_query(raw: str | None) -> str | None:
    """Turn free user text into a safe FTS5 MATCH expression.

    Each token is quoted and AND-ed (implicit), so punctuation or FTS operators
    in user input can never produce a syntax error.
    """
    if not raw:
        return None
    tokens = _WORD_RE.findall(raw)
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens)


def build_fts_or_query(terms: list[str] | None) -> str | None:
    """Match an item against ANY of several terms (used for capsule views).

    Each term is tokenised and AND-ed within itself (so a multi-word term like
    "Elon Musk" stays a phrase), then the terms are OR-ed together. Punctuation
    and FTS operators in user input can never produce a syntax error.
    """
    parts: list[str] = []
    for term in terms or []:
        tokens = _WORD_RE.findall(term or "")
        if tokens:
            phrase = " ".join(f'"{t}"' for t in tokens)
            parts.append(f"({phrase})")
    return " OR ".join(parts) if parts else None


def _bump_cluster(conn: sqlite3.Connection, dedup_key: str) -> int:
    """Increment a cluster's echo counter and return its representative item id."""
    conn.execute(
        "UPDATE clusters SET shared_count = shared_count + 1 WHERE id = ?", (dedup_key,)
    )
    rep = conn.execute(
        "SELECT id FROM items WHERE cluster_id = ? ORDER BY id LIMIT 1", (dedup_key,)
    ).fetchone()
    return int(rep["id"]) if rep else 0


def upsert_item(conn: sqlite3.Connection, item: RawItem) -> tuple[int, bool]:
    """Insert an item, or fold a (near-)duplicate into its cluster.

    Returns (item_id, is_new). Behaviour:
      * Re-fetch of the same post (matched on source+external_id, or identical
        text when no id) -> no-op, no echo bump.
      * Near/exact duplicate of *different* content already stored -> bump the
        cluster's shared_count ("Echoed N times") and do NOT store a new row.
      * Genuinely new content -> store it as the cluster representative.
    Resilient to a concurrent writer inserting the same content_hash.
    """
    content_hash = item.content_hash
    dedup_key = item.dedup_key

    # 1) Re-fetch detection (idempotent re-scans).
    if item.external_id:
        same = conn.execute(
            "SELECT id FROM items WHERE source = ? AND external_id = ?",
            (item.source, item.external_id),
        ).fetchone()
    else:
        same = conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (content_hash,)
        ).fetchone()
    if same is not None:
        return int(same["id"]), False

    # 2) Echo of already-clustered content.
    cluster = conn.execute("SELECT id FROM clusters WHERE id = ?", (dedup_key,)).fetchone()
    if cluster is not None:
        return _bump_cluster(conn, dedup_key), False

    # 3) New content -> store the representative item.
    conn.execute(
        "INSERT OR IGNORE INTO clusters (id, shared_count) VALUES (?, 1)", (dedup_key,)
    )
    try:
        cur = conn.execute(
            """
            INSERT INTO items (
                content_hash, dedup_key, source, track, external_id, url, author,
                title, content, language, published_at, fetched_at, media_urls, raw, cluster_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                dedup_key,
                item.source,
                item.track,
                item.external_id,
                item.url,
                item.author,
                item.title,
                item.content,
                item.language,
                item.published_at.isoformat() if item.published_at else None,
                item.fetched_at.isoformat(),
                json.dumps(item.media_urls),
                json.dumps(item.raw, default=str),
                dedup_key,
            ),
        )
        return int(cur.lastrowid), True
    except sqlite3.IntegrityError:
        # Lost a race on content_hash -> treat as an echo of the winner.
        return _bump_cluster(conn, dedup_key), False


def list_items(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    source: str | None = None,
) -> list[dict]:
    """Newest-first feed rows joined with echo count and (optional) analysis."""
    sql = [
        """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM items i
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        """
    ]
    params: list = []
    if source:
        sql.append("WHERE i.source = ?")
        params.append(source)
    sql.append("ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    rows = conn.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


def count_items(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT source FROM items ORDER BY source").fetchall()
    return [r[0] for r in rows]


def dismiss_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET dismissed = 1 WHERE id = ?", [item_id])
    conn.commit()


def undismiss_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE items SET dismissed = 0 WHERE id = ?", [item_id])
    conn.commit()


def _item_filter_clauses(
    *,
    q: str | None,
    source: str | None,
    threat: str | None,
    since: str | None,
    until: str | None,
    since_ts: str | None,
    unread_only: bool,
    read_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None,
) -> tuple[list[str], list]:
    """Build the shared WHERE clauses + params for item search and counting.

    Kept in one place so ``search_items`` and ``count_matching_items`` can never
    drift apart (pagination relies on the count matching the listing exactly).
    """
    where: list[str] = []
    params: list = []

    if terms is not None:
        # Case-scoped feed: restrict to items matching ANY tracking term.
        fts_terms = build_fts_or_query(terms)
        if fts_terms:
            where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            params.append(fts_terms)
        # Secondary text search narrows within those results (AND semantics).
        if q and q.strip():
            fts_q = build_fts_query(q.strip())
            if fts_q:
                where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
                params.append(fts_q)
    else:
        fts = build_fts_query(q)
        if fts:
            fts_clause = "i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)"
            # On the free-text path, also match OCR text from evidence screenshots.
            if q and q.strip():
                where.append(
                    "(" + fts_clause
                    + " OR i.id IN (SELECT item_id FROM evidence "
                    "WHERE ocr_text IS NOT NULL AND ocr_text LIKE ?))"
                )
                params.append(fts)
                params.append(f"%{q.strip()}%")
            else:
                where.append(fts_clause)
                params.append(fts)
    if source:
        where.append("i.source = ?")
        params.append(source)
    if threat:
        where.append("a.threat_level = ?")
        params.append(threat)
    if since:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) >= ?")
        params.append(since)
    if until:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) <= ?")
        params.append(until)
    if since_ts:
        where.append("COALESCE(i.published_at, i.fetched_at) >= ?")
        params.append(since_ts)
    if unread_only:
        where.append("i.id NOT IN (SELECT item_id FROM item_reads)")
    if read_only:
        where.append("i.id IN (SELECT item_id FROM item_reads)")
    if not show_dismissed:
        where.append("COALESCE(i.dismissed, 0) = 0")
    return where, params


def search_items(
    conn: sqlite3.Connection,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,   # 'YYYY-MM-DD'
    until: str | None = None,   # 'YYYY-MM-DD'
    since_ts: str | None = None,  # full ISO timestamp cutoff (for hour-level windows)
    unread_only: bool = False,
    read_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None = None,
    sort: str = "newest",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Full-text + faceted search over the entire local history.

    Free text hits items_fts (title/content/summary); the rest are exact-match
    facets. ``since``/``until`` filter on the date part; ``since_ts`` is a full
    ISO timestamp cutoff for hour-level "last N hours" windows. ``unread_only``
    hides items already marked read. When ``terms`` is given (a capsule's terms)
    the text match becomes an OR across those terms instead of the single ``q``
    AND-match.
    """
    select = """
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
    """
    where, params = _item_filter_clauses(
        q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, read_only=read_only,
        show_dismissed=show_dismissed, terms=terms,
    )

    order_dir = "ASC" if sort == "oldest" else "DESC"
    sql = select
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY COALESCE(i.published_at, i.fetched_at) {order_dir} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_matching_items(
    conn: sqlite3.Connection,
    q: str | None = None,
    source: str | None = None,
    threat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    since_ts: str | None = None,
    unread_only: bool = False,
    show_dismissed: bool = False,
    terms: list[str] | None = None,
) -> int:
    """Total number of items matching the same filters ``search_items`` uses.

    Drives pagination: lets the feed know whether more pages exist beyond the
    current ``limit``/``offset`` window.
    """
    where, params = _item_filter_clauses(
        q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, unread_only=unread_only, show_dismissed=show_dismissed, terms=terms,
    )
    sql = (
        "SELECT COUNT(*) FROM items i "
        "LEFT JOIN analyses a ON a.item_id = i.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()[0])


def case_timeline(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Return all pinned items for a case sorted oldest-first, each row including
    its date (YYYY-MM-DD) so the template can group them into day bands."""
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.url, i.title,
               COALESCE(i.published_at, i.fetched_at) AS ts,
               substr(COALESCE(i.published_at, i.fetched_at), 1, 10) AS day,
               a.threat_level, a.summary
        FROM bookmarks bm
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE bm.case_id = ?
        ORDER BY ts ASC NULLS LAST
        """,
        [case_id],
    ).fetchall()
    return [dict(r) for r in rows]


# --- Analysis persistence ---------------------------------------------------
# The intelligence layer is decoupled from collection: items land first, then a
# separate pass enriches them. `pending_analysis` finds rows that still need
# Claude; `save_analysis` writes the result back and mirrors the summary into
# items.summary so it becomes full-text searchable.


def pending_analysis(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Newest stored items that have no analysis row yet.

    Used by the Claude pipeline as its work queue. `limit` is the per-run cap
    (budget guard) so a large backlog can never blow the token budget at once.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE a.item_id IS NULL
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_analysis(conn: sqlite3.Connection, item_id: int, analysis) -> None:
    """Persist an Analysis for an item (idempotent on item_id).

    Also copies the summary onto items.summary; the FTS update trigger then
    folds it into items_fts so analyst summaries are searchable.
    """
    # Persist the typed entity object when the model supplied one (backward
    # compatible: older rows hold a flat JSON array; readers handle both).
    entity_groups = getattr(analysis, "entity_groups", None) or {}
    if any(entity_groups.values()):
        entities_json = json.dumps(entity_groups, ensure_ascii=False)
    else:
        entities_json = json.dumps(analysis.entities, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO analyses (
            item_id, threat_level, summary, translation, target_lang,
            entities, party, contradiction, confidence, model, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            threat_level  = excluded.threat_level,
            summary       = excluded.summary,
            translation   = excluded.translation,
            target_lang   = excluded.target_lang,
            entities      = excluded.entities,
            party         = excluded.party,
            contradiction = excluded.contradiction,
            confidence    = excluded.confidence,
            model         = excluded.model,
            analyzed_at   = excluded.analyzed_at
        """,
        (
            item_id,
            analysis.threat_level.value,
            analysis.summary,
            analysis.translation,
            analysis.target_lang,
            entities_json,
            analysis.party.value,
            1 if analysis.contradiction else 0,
            getattr(analysis, "confidence", None),
            analysis.model,
            analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
        ),
    )
    # Mirror summary into items so it is indexed by items_fts (UPDATE trigger).
    if analysis.summary:
        conn.execute(
            "UPDATE items SET summary = ? WHERE id = ?", (analysis.summary, item_id)
        )
    # Keep the entity index in sync (powers the entity dossier + faster graph).
    _reindex_item_entities(conn, item_id, entities_json)


def _reindex_item_entities(conn: sqlite3.Connection, item_id: int, entities_raw) -> None:
    """Rebuild item_entities rows for one item from its entities JSON.

    Idempotent: clears this item's rows then re-inserts the de-duplicated set, so
    a re-analysis updates the index cleanly. Fail-soft — an indexing hiccup must
    never break saving an analysis (graceful degradation)."""
    try:
        groups, flat = _decode_entities(entities_raw)
        kind_map = _entity_kind_map(groups)
        conn.execute("DELETE FROM item_entities WHERE item_id = ?", (item_id,))
        seen: set[str] = set()
        for raw_name in flat:
            norm = _norm_entity_key(raw_name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            conn.execute(
                "INSERT OR IGNORE INTO item_entities "
                "(item_id, name, name_norm, kind) VALUES (?, ?, ?, ?)",
                (item_id, _clean_entity_display(raw_name), norm,
                 kind_map.get(norm, "entity")),
            )
    except Exception:
        logger.exception("entity index update failed for item %s", item_id)


# --- Keyless translation fallback -------------------------------------------
# A separate, optional pass renders non-English items into English when no AI
# translation exists (e.g. no AI provider is configured). It reuses the same
# analyses.translation slot the feed already reads, so no template change is
# needed. It must NEVER clobber a higher-quality AI translation.


def pending_keyless_translation(
    conn: sqlite3.Connection, limit: int = 200
) -> list[dict]:
    """Items that still lack ANY English translation, newest first.

    Returns rows with no analysis row at all, OR an analysis row whose
    ``translation`` is empty. Such rows are the only candidates the keyless pass
    may write to (rows that already carry a translation are left untouched).
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE a.item_id IS NULL
           OR a.translation IS NULL
           OR TRIM(a.translation) = ''
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_keyless_translation(
    conn: sqlite3.Connection,
    item_id: int,
    translation: str,
    target_lang: str,
    model: str,
) -> bool:
    """Store a keyless English rendering into the existing translation slot.

    Idempotent and non-destructive: writes ONLY when the row has no translation
    yet, so an AI-produced (higher-quality) translation is never overwritten.
    Creates a minimal analyses row when none exists. Returns True if it wrote.
    """
    cur = conn.execute(
        "SELECT item_id, translation FROM analyses WHERE item_id = ?", (item_id,)
    ).fetchone()

    if cur is None:
        # No analysis row at all — create a minimal one carrying just the
        # translation (threat_level/party keep their schema defaults).
        conn.execute(
            """
            INSERT INTO analyses (item_id, translation, target_lang, model, analyzed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (item_id, translation, target_lang, model),
        )
        return True

    existing = (cur["translation"] or "").strip()
    if existing:
        # Guard: a translation already exists (AI or earlier keyless pass). Do
        # NOT clobber it.
        return False

    conn.execute(
        """
        UPDATE analyses
        SET translation = ?, target_lang = ?
        WHERE item_id = ? AND (translation IS NULL OR TRIM(translation) = '')
        """,
        (translation, target_lang, item_id),
    )
    return True


# --- Feed presentation enrichment -------------------------------------------
# Feed rows come from search_items / list_items / intel_items / case_items as
# raw DB columns. `enrich_feed_rows` augments them in one batched pass with the
# derived fields the card template needs (typed entities, related sources in
# the same cluster, evidence-captured flag, the best intelligence-requirement
# match, a source-reliability label, a short display URL, and a relative time)
# without N+1 queries. It is purely additive and safe to call on any feed rows.

_ENTITY_GROUP_KEYS = ("people", "organizations", "locations", "identifiers")

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
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
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


def _decode_entities(raw) -> tuple[dict[str, list[str]], list[str]]:
    """Decode the stored entities JSON into (typed groups, flat list).

    Handles both the new typed object and the legacy flat array transparently.
    """
    groups: dict[str, list[str]] = {k: [] for k in _ENTITY_GROUP_KEYS}
    flat: list[str] = []
    if not raw:
        return groups, flat
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return groups, flat
    seen: set[str] = set()

    def _push(name) -> None:
        name = str(name).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            flat.append(name)

    if isinstance(data, dict):
        for k in _ENTITY_GROUP_KEYS:
            members = data.get(k) or []
            if not isinstance(members, list):
                members = [members]
            for m in members:
                m = str(m).strip()
                if m:
                    groups[k].append(m)
                    _push(m)
    elif isinstance(data, list):
        for m in data:
            _push(m)
    return groups, flat


def decode_entities(raw) -> dict[str, list[str]]:
    """Public helper: parse raw entities JSON → typed groups dict."""
    groups, _ = _decode_entities(raw)
    return groups


# Characters the AI commonly leaves wrapped around or trailing an entity name
# (quote styles, sentence punctuation, brackets). Stripped from both ends when
# keying so "Alice", "Alice.", and '"Alice"' collapse into one graph node.
_ENTITY_EDGE_CHARS = " \t\r\n\"'`.,;:!?()[]{}“”‘’«»"


def _norm_entity_key(name: str) -> str:
    """Canonical, case-insensitive key for de-duplicating an entity name.

    Conservative on purpose: it lowercases and trims surrounding whitespace,
    quotes, and trailing sentence punctuation, but never touches the inside of a
    name — so genuine variants merge while distinct entities stay distinct.
    """
    return name.strip().strip(_ENTITY_EDGE_CHARS).lower()


def _clean_entity_display(name: str) -> str:
    """A tidy display form: drop surrounding whitespace/quotes but keep casing."""
    return name.strip().strip(_ENTITY_EDGE_CHARS) or name.strip()


# Which typed group a name belongs to (for graph node colouring). The first
# group a name appears in wins; names from the legacy flat list are "entity".
def _entity_kind_map(groups: dict[str, list[str]]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    # people/org/location/identifier -> a singular kind label used by the graph.
    label = {
        "people": "person",
        "organizations": "organization",
        "locations": "location",
        "identifiers": "identifier",
    }
    for key in _ENTITY_GROUP_KEYS:
        for name in groups.get(key, []):
            low = _norm_entity_key(name)
            if low and low not in kinds:
                kinds[low] = label[key]
    return kinds


# --- Entity alias helpers ---------------------------------------------------

def get_entity_aliases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, alias, canonical, created_at FROM entity_aliases ORDER BY alias COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def add_entity_alias(conn: sqlite3.Connection, alias: str, canonical: str) -> None:
    alias = (alias or "").strip()
    canonical = (canonical or "").strip()
    if alias and canonical and alias.lower() != canonical.lower():
        conn.execute(
            "INSERT OR REPLACE INTO entity_aliases (alias, canonical) VALUES (?, ?)",
            (alias, canonical),
        )


def delete_entity_alias(conn: sqlite3.Connection, alias_id: int) -> None:
    conn.execute("DELETE FROM entity_aliases WHERE id = ?", (alias_id,))


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
    rows = search_items(
        conn, q=q, source=source, threat=threat, since=since, until=until,
        since_ts=since_ts, terms=terms, limit=scan_limit,
    )

    # Load analyst-defined aliases once: "Messi" → "Lionel Messi".
    # alias_map: raw_low → canonical_low
    # alias_display: raw_low → canonical display string (for first-seen assignment)
    alias_map: dict[str, str] = {}
    alias_display: dict[str, str] = {}
    for a in get_entity_aliases(conn):
        raw_low = _norm_entity_key(a["alias"])
        can_low = _norm_entity_key(a["canonical"])
        if raw_low and can_low:
            alias_map[raw_low] = can_low
            alias_display[raw_low] = a["canonical"]

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


def _entity_norm_set(conn: sqlite3.Connection, name: str) -> tuple[str, set[str]]:
    """Resolve a name to (canonical_display, {all norm keys that mean the same}).

    Honours entity_aliases both ways: if ``name`` is an alias, jump to its
    canonical; then gather the canonical plus every alias that maps to it, so a
    dossier for "Messi" and "Lionel Messi" is one and the same.
    """
    raw = (name or "").strip()
    canonical = raw
    arow = conn.execute(
        "SELECT canonical FROM entity_aliases WHERE alias = ? COLLATE NOCASE", (raw,)
    ).fetchone()
    if arow:
        canonical = arow["canonical"]
    norms = {_norm_entity_key(raw), _norm_entity_key(canonical)}
    for a in conn.execute(
        "SELECT alias FROM entity_aliases WHERE canonical = ? COLLATE NOCASE", (canonical,)
    ).fetchall():
        norms.add(_norm_entity_key(a["alias"]))
    norms.discard("")
    return canonical, norms


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
    for (signal, _row, reasons), it in zip(top, items):
        if it.get("source_count", 1) >= 2:
            reasons = reasons + [f"Confirmed by {it['source_count']} sources"]
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

    # Related sources: other items sharing a cluster_id with any row on the page.
    cluster_ids = sorted({r["cluster_id"] for r in rows if r.get("cluster_id")})
    related_by_cluster: dict[str, list[dict]] = {}
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


# --- Read / unread tracking -------------------------------------------------


def mark_read(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark an item read (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO item_reads (item_id) VALUES (?)", (item_id,)
    )


def mark_unread(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM item_reads WHERE item_id = ?", (item_id,))


def is_read(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM item_reads WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row is not None


# --- Custom triage lists / lanes --------------------------------------------
# Personal organization layer: the analyst creates named lists (e.g. "Very
# interesting", "Not interesting") and files items into any number of them.

# Accent colors the UI knows how to render (keeps user input to a safe set).
_LIST_COLORS = {
    "slate", "emerald", "amber", "red", "sky", "indigo", "fuchsia", "teal", "orange",
}


def create_list(
    conn: sqlite3.Connection, name: str, color: str = "slate",
    case_id: int | None = None,
) -> int:
    """Create a triage list, optionally scoped to a case."""
    name = (name or "").strip()[:60] or "Untitled"
    color = color if color in _LIST_COLORS else "slate"
    if case_id is not None:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM lists WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lists (name, color, position, case_id) VALUES (?, ?, ?, ?)",
            (name, color, pos, case_id),
        )
    else:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM lists WHERE case_id IS NULL"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lists (name, color, position) VALUES (?, ?, ?)",
            (name, color, pos),
        )
    return int(cur.lastrowid)


def rename_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
    name = (name or "").strip()[:60]
    if name:
        conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    """Delete a list (memberships cascade; the items themselves are untouched)."""
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))


def update_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
    """Rename a triage list."""
    conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name.strip(), list_id))


def list_lists(conn: sqlite3.Connection, case_id: int | None = None) -> list[dict]:
    """Triage lists in display order, each with its item count.

    When ``case_id`` is given, returns only lists scoped to that case.
    When ``case_id`` is None, returns only global (unscoped) lists.
    """
    if case_id is not None:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.color, l.position, l.created_at,
                   (SELECT COUNT(*) FROM list_memberships m WHERE m.list_id = l.id) AS item_count,
                   (SELECT MAX(i.fetched_at) FROM list_memberships m2
                    JOIN items i ON i.id = m2.item_id WHERE m2.list_id = l.id) AS last_item_at
            FROM lists l
            WHERE l.case_id = ?
            ORDER BY l.position, l.id
            """,
            (case_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.color, l.position, l.created_at,
                   (SELECT COUNT(*) FROM list_memberships m WHERE m.list_id = l.id) AS item_count,
                   (SELECT MAX(i.fetched_at) FROM list_memberships m2
                    JOIN items i ON i.id = m2.item_id WHERE m2.list_id = l.id) AS last_item_at
            FROM lists l
            WHERE l.case_id IS NULL
            ORDER BY l.position, l.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_list(conn: sqlite3.Connection, list_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, color, position, created_at FROM lists WHERE id = ?",
        (list_id,),
    ).fetchone()
    return dict(row) if row else None


def add_to_list(conn: sqlite3.Connection, list_id: int, item_id: int) -> None:
    """File an item into a list (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO list_memberships (list_id, item_id) VALUES (?, ?)",
        (list_id, item_id),
    )


def remove_from_list(conn: sqlite3.Connection, list_id: int, item_id: int) -> None:
    conn.execute(
        "DELETE FROM list_memberships WHERE list_id = ? AND item_id = ?",
        (list_id, item_id),
    )


def list_member_items(conn: sqlite3.Connection, list_id: int) -> list[dict]:
    """Full feed rows filed into a list, most-recently-added first."""
    rows = conn.execute(
        """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM list_memberships m
        JOIN items i ON i.id = m.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE m.list_id = ?
        ORDER BY m.added_at DESC
        """,
        (list_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Analyst workspace: cases, bookmarks, notes -----------------------------
# These power the investigator surface: pin items, attach notes, and group work
# into named cases. All writes are idempotent where a natural key exists.


# Allowed lifecycle states and priorities (constrains user input to a safe set).
_CASE_STATUSES = {"open", "closed"}
_CASE_PRIORITIES = {"high", "medium", "low"}
# Sort weight so high-priority open cases float to the top of the board.
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


# Sentinel so callers can distinguish "don't filter by case" from "case is NULL".
_NO_CASE_FILTER = object()


def create_case(
    conn: sqlite3.Connection,
    name: str,
    description: str | None = None,
    priority: str = "medium",
    parent_id: int | None = None,
) -> int:
    """Create a case. ``parent_id`` makes it a sub-case of another case.

    Sub-cases are one level deep: if ``parent_id`` itself points at a sub-case,
    we re-parent to that sub-case's top-level case so the tree never nests.
    """
    priority = priority if priority in _CASE_PRIORITIES else "medium"
    if parent_id is not None:
        row = conn.execute(
            "SELECT parent_id FROM cases WHERE id = ?", (parent_id,)
        ).fetchone()
        if row is not None and row["parent_id"] is not None:
            parent_id = int(row["parent_id"])  # flatten to one level
    cur = conn.execute(
        "INSERT INTO cases (name, description, priority, parent_id) VALUES (?, ?, ?, ?)",
        (name, description, priority, parent_id),
    )
    return int(cur.lastrowid)


def update_case(
    conn: sqlite3.Connection,
    case_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
) -> None:
    """Patch any subset of a case's editable fields. Unknown enum values ignored."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        name = name.strip()[:120]
        if name:
            sets.append("name = ?")
            params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description.strip() or None)
    if status is not None and status in _CASE_STATUSES:
        sets.append("status = ?")
        params.append(status)
    if priority is not None and priority in _CASE_PRIORITIES:
        sets.append("priority = ?")
        params.append(priority)
    if not sets:
        return
    params.append(case_id)
    conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", params)


def delete_case(conn: sqlite3.Connection, case_id: int) -> None:
    """Delete a case. Its bookmarks/notes cascade; the items themselves stay."""
    conn.execute("DELETE FROM bookmarks WHERE case_id = ?", (case_id,))
    conn.execute("DELETE FROM notes WHERE case_id = ?", (case_id,))
    conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
    # If it was the active case, clear that pointer so the UI doesn't dangle.
    if get_meta(conn, "active_case_id") == str(case_id):
        set_meta(conn, "active_case_id", None)


def list_cases(
    conn: sqlite3.Connection,
    status: str | None = None,
    parent_id=_NO_CASE_FILTER,
) -> list[dict]:
    """Cases with item/note/sub-case counts, ordered open-first then by priority.

    ``status`` filters lifecycle ('open'/'closed'). ``parent_id`` filtering: omit
    for all cases; pass ``None`` for only top-level cases (the hub overview); pass
    an int for one case's sub-cases.
    """
    clauses: list[str] = []
    params: list = []
    if status in _CASE_STATUSES:
        clauses.append("c.status = ?")
        params.append(status)
    if parent_id is not _NO_CASE_FILTER:
        if parent_id is None:
            clauses.append("c.parent_id IS NULL")
        else:
            clauses.append("c.parent_id = ?")
            params.append(int(parent_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT c.id, c.name, c.description, c.created_at, c.parent_id,
               COALESCE(c.status, 'open') AS status,
               COALESCE(c.priority, 'medium') AS priority,
               (SELECT COUNT(*) FROM bookmarks b WHERE b.case_id = c.id) AS item_count,
               (SELECT COUNT(*) FROM notes n WHERE n.case_id = c.id) AS note_count,
               (SELECT COUNT(*) FROM cases s WHERE s.parent_id = c.id) AS subcase_count
        FROM cases c
        {where}
        ORDER BY c.id DESC
        """,
        params,
    ).fetchall()
    out = [dict(r) for r in rows]
    # Final ordering in Python so we can rank by priority cleanly:
    # open before closed, then high>medium>low, then newest id.
    out.sort(
        key=lambda c: (
            0 if c["status"] == "open" else 1,
            _PRIORITY_RANK.get(c["priority"], 1),
            -int(c["id"]),
        )
    )
    return out


def get_case(conn: sqlite3.Connection, case_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, name, description, created_at, parent_id,
               COALESCE(status, 'open') AS status,
               COALESCE(priority, 'medium') AS priority
        FROM cases WHERE id = ?
        """,
        (case_id,),
    ).fetchone()
    return dict(row) if row else None


def set_active_case(conn: sqlite3.Connection, case_id: int | None) -> None:
    """Remember which case the analyst is currently working in (or None)."""
    set_meta(conn, "active_case_id", str(case_id) if case_id else None)


def get_active_case(conn: sqlite3.Connection) -> dict | None:
    """The currently active case as a full row, or None if unset/missing."""
    raw = get_meta(conn, "active_case_id")
    if not raw:
        return None
    try:
        case = get_case(conn, int(raw))
    except (TypeError, ValueError):
        return None
    return case


# --- A case's tracking words (its "word capsule") + derived live feed --------

def case_terms(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """The tracking terms for a case (each as {id, term, kind}), oldest first."""
    rows = conn.execute(
        "SELECT id, term, kind FROM case_terms WHERE case_id = ? ORDER BY id",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def all_case_terms(conn: sqlite3.Connection) -> list[str]:
    """Every distinct tracking word across all cases (for the home 'topics' scope)."""
    rows = conn.execute("SELECT DISTINCT term FROM case_terms ORDER BY term").fetchall()
    return [r["term"] for r in rows]


def add_case_term(
    conn: sqlite3.Connection, case_id: int, term: str, kind: str = "required"
) -> None:
    """Add one tracking word/phrase to a case (idempotent per case)."""
    term = (term or "").strip()
    if term:
        conn.execute(
            "INSERT OR IGNORE INTO case_terms (case_id, term, kind) VALUES (?, ?, ?)",
            (case_id, term, kind),
        )


def remove_case_term(conn: sqlite3.Connection, case_id: int, term_id: int) -> None:
    conn.execute(
        "DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, case_id)
    )


def update_case_term(
    conn: sqlite3.Connection, case_id: int, term_id: int, new_term: str
) -> None:
    """Edit an existing tracking word in place. Blank is ignored; a clash with
    another existing term for the case removes this row instead of duplicating."""
    new_term = (new_term or "").strip()
    if not new_term:
        return
    clash = conn.execute(
        "SELECT id FROM case_terms WHERE case_id = ? AND term = ? AND id <> ?",
        (case_id, new_term, term_id),
    ).fetchone()
    if clash is not None:
        conn.execute(
            "DELETE FROM case_terms WHERE id = ? AND case_id = ?", (term_id, case_id)
        )
        return
    conn.execute(
        "UPDATE case_terms SET term = ? WHERE id = ? AND case_id = ?",
        (new_term, term_id, case_id),
    )


def _required_terms_for_case(conn: sqlite3.Connection, case_id: int) -> list[str]:
    """Return the effective required search terms for a case.

    For a sub-case this includes the parent's required terms (HERITAGE inheritance)
    so the sub-case feed is a superset of the parent's coverage plus its own focus.
    """
    rows = conn.execute(
        "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
        (case_id,),
    ).fetchall()
    terms = [r["term"] for r in rows]

    case_row = conn.execute("SELECT parent_id FROM cases WHERE id = ?", (case_id,)).fetchone()
    parent_id = case_row["parent_id"] if case_row else None
    if parent_id:
        parent_rows = conn.execute(
            "SELECT term FROM case_terms WHERE case_id = ? AND kind = 'required' ORDER BY id",
            (parent_id,),
        ).fetchall()
        seen = set(terms)
        for r in parent_rows:
            if r["term"] not in seen:
                terms.append(r["term"])
    return terms


def case_alert_terms(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Alert words for a case ({id, term}) — passive flags on incoming items only."""
    rows = conn.execute(
        "SELECT id, term FROM case_terms WHERE case_id = ? AND kind = 'alert' ORDER BY id",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_live_items(
    conn: sqlite3.Connection,
    case_id: int,
    limit: int = 100,
    unread_only: bool = True,
    source: str | None = None,
    since: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> list[dict]:
    """Unread (queue) items for a case: matching required terms, newest first.

    Defaults to ``unread_only=True`` so the feed is a triage queue — items
    disappear once marked read and only re-appear when new material arrives.
    Pass ``unread_only=False`` for reports/briefings that need the full history.
    Sub-cases inherit parent required terms automatically.
    """
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return []
    return search_items(
        conn, q=q, terms=terms, limit=limit,
        unread_only=unread_only, source=source, since=since, sort=sort,
    )


def case_unread_count(conn: sqlite3.Connection, case_id: int) -> int:
    """Total unread items matching this case's required terms (ignores last-visit cutoff)."""
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return 0
    fts = build_fts_or_query(terms)
    if not fts:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM items WHERE id IN "
        "(SELECT rowid FROM items_fts WHERE items_fts MATCH ?) "
        "AND id NOT IN (SELECT item_id FROM item_reads)",
        (fts,),
    ).fetchone()
    return row[0] if row else 0


def case_reviewed_items(
    conn: sqlite3.Connection,
    case_id: int,
    limit: int = 100,
    source: str | None = None,
    since: str | None = None,
    q: str | None = None,
    sort: str = "newest",
) -> list[dict]:
    """Items matching this case's required terms that have been marked as read (reviewed)."""
    terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return []
    return search_items(
        conn, q=q, terms=terms, limit=limit,
        read_only=True, source=source, since=since, sort=sort,
    )


# --- "New since last visit" — the daily-routine engine ---------------------

def get_case_last_visit(conn: sqlite3.Connection, case_id: int) -> str | None:
    """ISO timestamp of when the analyst last opened this case (or None)."""
    return get_meta(conn, f"case_visit:{case_id}")


def touch_case_visit(conn: sqlite3.Connection, case_id: int) -> None:
    """Record 'just visited now' so the case's new-count resets."""
    from datetime import datetime, timezone

    set_meta(conn, f"case_visit:{case_id}", datetime.now(timezone.utc).isoformat())


def case_terms_map(
    conn: sqlite3.Connection, case_ids: list[int], kind: str | None = None
) -> dict[int, list[str]]:
    """All tracking terms for many cases in ONE query → {case_id: [term, ...]}.

    Lets the Cases board avoid a per-case term query (N+1). Missing cases map to [].
    Pass ``kind='required'`` to restrict to search terms (excludes alert words).
    """
    out: dict[int, list[str]] = {int(i): [] for i in case_ids}
    if not case_ids:
        return out
    placeholders = ",".join("?" * len(case_ids))
    params: list = list(case_ids)
    sql = f"SELECT case_id, term FROM case_terms WHERE case_id IN ({placeholders})"
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        out.setdefault(int(r["case_id"]), []).append(r["term"])
    return out


def case_new_count(
    conn: sqlite3.Connection, case_id: int, terms: list[str] | None = None
) -> int:
    """How many tracked items have arrived since the case was last opened.

    Before the first visit there is no cutoff, so this is the full live count
    ('you have N items to review'); afterwards it is the delta since last visit.
    Only required terms are counted — alert words are passive and don't inflate
    the badge. ``terms`` may be passed in (from :func:`case_terms_map` with
    ``kind='required'``) to avoid an extra per-case query on the board.
    """
    if terms is None:
        terms = _required_terms_for_case(conn, case_id)
    if not terms:
        return 0
    return count_matching_items(
        conn, terms=terms, since_ts=get_case_last_visit(conn, case_id)
    )


def case_new_items(
    conn: sqlite3.Connection, case_id: int, limit: int = 20
) -> list[dict]:
    """The tracked items that arrived since the case was last opened (newest first)."""
    terms = [t["term"] for t in case_terms(conn, case_id)]
    if not terms:
        return []
    return search_items(
        conn, terms=terms, since_ts=get_case_last_visit(conn, case_id), limit=limit
    )


def mark_case_read(conn: sqlite3.Connection, case_id: int, limit: int = 2000) -> int:
    """Mark every one of a case's tracked items as read. Returns how many."""
    items = case_live_items(conn, case_id, limit=limit)
    for it in items:
        mark_read(conn, it["id"])
    return len(items)


def case_question_items(
    conn: sqlite3.Connection, case_id: int, min_score: int = 1, limit: int = 100
) -> list[dict]:
    """Items scored against THIS case's questions, most relevant first.

    A single scoped join (best score per item across the case's requirements),
    so a case with thousands of scored items stays instant.
    """
    req_ids = [int(r["id"]) for r in list_requirements(conn, case_id=case_id)]
    if not req_ids:
        return []
    placeholders = ",".join("?" * len(req_ids))
    sql = f"""
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence,
            MAX(h.score) AS rel_score,
            COUNT(DISTINCT h.requirement_id) AS rel_match_count
        FROM requirement_hits h
        JOIN items i ON i.id = h.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE h.requirement_id IN ({placeholders}) AND h.score >= ?
        GROUP BY i.id
        ORDER BY rel_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT ?
    """
    return [dict(r) for r in conn.execute(sql, [*req_ids, min_score, limit]).fetchall()]


def add_bookmark(
    conn: sqlite3.Connection, item_id: int, case_id: int | None = None
) -> None:
    """Pin an item, optionally into a case. Idempotent on (item_id, case_id)."""
    conn.execute(
        "INSERT OR IGNORE INTO bookmarks (item_id, case_id) VALUES (?, ?)",
        (item_id, case_id),
    )


def remove_bookmark(
    conn: sqlite3.Connection, item_id: int, case_id: int | None = None
) -> None:
    if case_id is None:
        conn.execute(
            "DELETE FROM bookmarks WHERE item_id = ? AND case_id IS NULL", (item_id,)
        )
    else:
        conn.execute(
            "DELETE FROM bookmarks WHERE item_id = ? AND case_id = ?", (item_id, case_id)
        )


def case_items(
    conn: sqlite3.Connection, case_id: int, query: str | None = None
) -> list[dict]:
    """Full item rows (with analysis) bookmarked into a case, newest first.

    Optional `query` does a simple case-insensitive substring match across the
    item title, content, summary and entities — a quick within-case filter.
    """
    sql = """
        SELECT
            i.id, i.source, i.url, i.author, i.title, i.content, i.language,
            i.published_at, i.fetched_at, i.cluster_id,
            COALESCE(c.shared_count, 1) AS shared_count,
            a.threat_level, a.summary, a.translation, a.target_lang,
            a.entities, a.party, a.contradiction, a.confidence
        FROM bookmarks bm
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN clusters c ON c.id = i.cluster_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE bm.case_id = ?
    """
    params: list = [case_id]
    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        sql += """
          AND (i.title LIKE ? OR i.content LIKE ?
               OR a.summary LIKE ? OR a.entities LIKE ?)
        """
        params += [like, like, like, like]
    sql += " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def case_evidence(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Every evidence screenshot captured for items pinned in a case.

    The basis of a chain-of-custody manifest: each row carries the SHA-256 hash
    and capture timestamp recorded at capture time, plus the item it documents.
    Ordered oldest-first (chronological provenance).
    """
    rows = conn.execute(
        """
        SELECT e.id, e.sha256, e.captured_at, e.screenshot,
               i.id AS item_id, i.title, i.url, i.source
        FROM evidence e
        JOIN items i ON i.id = e.item_id
        JOIN bookmarks b ON b.item_id = e.item_id AND b.case_id = ?
        GROUP BY e.id
        ORDER BY e.captured_at ASC, e.id ASC
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _entity_tokens(raw) -> set[str]:
    """Normalized lowercase entity strings from a stored entities value."""
    _typed, flat = _decode_entities(raw)
    return {t.strip().lower() for t in flat if t and len(t.strip()) >= 3}


def related_cases(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    """Other cases connected to this one, for pivoting across investigations.

    Two kinds of link are detected and merged per other-case:
      - shared_items: the exact same item is pinned in both cases.
      - shared_entities: an entity (person/org/wallet/domain…) named in this
        case's material also appears in the other case's material.
    Returns a list sorted by strength (shared items first, then entity overlap).
    """
    # This case's pinned items + their entity tokens.
    mine = case_items(conn, case_id)
    if not mine:
        return []
    my_item_ids = {int(r["id"]) for r in mine}
    my_tokens: set[str] = set()
    for r in mine:
        my_tokens |= _entity_tokens(r.get("entities"))

    # Every other open/closed case and its pinned items (single pass).
    others: dict[int, dict] = {}
    rows = conn.execute(
        """
        SELECT c.id AS cid, c.name AS cname,
               COALESCE(c.status,'open') AS status,
               COALESCE(c.priority,'medium') AS priority,
               i.id AS iid, i.title AS ititle, a.entities AS entities
        FROM cases c
        JOIN bookmarks bm ON bm.case_id = c.id
        JOIN items i ON i.id = bm.item_id
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE c.id != ?
        """,
        (case_id,),
    ).fetchall()
    for row in rows:
        cid = int(row["cid"])
        bucket = others.setdefault(
            cid,
            {
                "id": cid,
                "name": row["cname"],
                "status": row["status"],
                "priority": row["priority"],
                "shared_items": [],
                "shared_entities": set(),
            },
        )
        iid = int(row["iid"])
        if iid in my_item_ids:
            bucket["shared_items"].append({"id": iid, "title": row["ititle"]})
        overlap = my_tokens & _entity_tokens(row["entities"])
        if overlap:
            bucket["shared_entities"] |= overlap

    out: list[dict] = []
    for b in others.values():
        if not b["shared_items"] and not b["shared_entities"]:
            continue
        # Dedup shared items by id; cap entity list for display.
        seen: set[int] = set()
        uniq_items = []
        for it in b["shared_items"]:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq_items.append(it)
        b["shared_items"] = uniq_items
        b["shared_entities"] = sorted(b["shared_entities"])[:12]
        out.append(b)

    out.sort(
        key=lambda b: (-len(b["shared_items"]), -len(b["shared_entities"]), b["name"].lower())
    )
    return out


def add_note(
    conn: sqlite3.Connection,
    body: str,
    item_id: int | None = None,
    case_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO notes (item_id, case_id, body) VALUES (?, ?, ?)",
        (item_id, case_id, body),
    )
    return int(cur.lastrowid)


def case_notes(conn: sqlite3.Connection, case_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, item_id, body, created_at
        FROM notes WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_note(conn: sqlite3.Connection, note_id: int, body: str) -> int | None:
    """Edit a note's body in place; returns the note's case_id (for re-render)."""
    body = (body or "").strip()
    if not body:
        return None
    conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, note_id))
    row = conn.execute("SELECT case_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def delete_note(conn: sqlite3.Connection, note_id: int) -> int | None:
    """Delete a note; returns the case_id it belonged to (for re-render)."""
    row = conn.execute("SELECT case_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    case_id = int(row[0]) if row and row[0] is not None else None
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return case_id


# --- Watchlists + alerts ----------------------------------------------------
# A watchlist is a pattern (keyword / regex / literal token like a wallet or
# phone number). New items are matched against every watchlist during a scan;
# a match records a watchlist_hit, which surfaces as an alert in the UI.


def create_watchlist(
    conn: sqlite3.Connection, label: str, pattern: str, kind: str = "keyword"
) -> int:
    # Cap regex pattern length up front (ReDoS guardrail): a runaway pattern
    # never gets stored, so it can't be evaluated on every scanned item.
    if kind == "regex":
        pattern = (pattern or "")[:_MAX_REGEX_PATTERN_LEN]
    cur = conn.execute(
        "INSERT INTO watchlists (label, pattern, kind) VALUES (?, ?, ?)",
        (label, pattern, kind),
    )
    return int(cur.lastrowid)


def list_watchlists(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT w.id, w.label, w.pattern, w.kind, w.enabled, w.created_at,
               (SELECT COUNT(*) FROM watchlist_hits h WHERE h.watchlist_id = w.id) AS hit_count
        FROM watchlists w
        ORDER BY w.id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_watchlist(conn: sqlite3.Connection, watchlist_id: int) -> None:
    conn.execute(
        "UPDATE watchlists SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE id = ?",
        (watchlist_id,),
    )


def delete_watchlist(conn: sqlite3.Connection, watchlist_id: int) -> None:
    conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))


def list_watchlist_hits(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Recent alerts: each hit joined to its watchlist and the matched item."""
    rows = conn.execute(
        """
        SELECT h.id, h.hit_at, w.label, w.pattern, w.kind,
               i.id AS item_id, i.source, i.title, i.url,
               COALESCE(i.summary, '') AS summary, i.content
        FROM watchlist_hits h
        JOIN watchlists w ON w.id = h.watchlist_id
        JOIN items i ON i.id = h.item_id
        ORDER BY h.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_new_watchlist_hits(conn: sqlite3.Connection) -> int:
    """Return the number of watchlist hits the analyst hasn't seen yet."""
    last_raw = get_meta(conn, "watchlist_last_seen_hit_id") or "0"
    try:
        last_id = int(last_raw)
    except ValueError:
        last_id = 0
    row = conn.execute(
        "SELECT COUNT(*) FROM watchlist_hits WHERE id > ?", (last_id,)
    ).fetchone()
    return row[0] if row else 0


def mark_watchlist_hits_seen(conn: sqlite3.Connection) -> None:
    """Record that the analyst has now seen all current hits."""
    row = conn.execute("SELECT MAX(id) FROM watchlist_hits").fetchone()
    max_id = row[0] if row and row[0] is not None else 0
    set_meta(conn, "watchlist_last_seen_hit_id", str(max_id))


def _matches(pattern: str, kind: str, text: str) -> bool:
    if not pattern or not text:
        return False
    if kind == "regex":
        try:
            # Bound the haystack so a pathological pattern can't backtrack for an
            # unbounded time on a long item body (ReDoS guardrail).
            return re.search(pattern[:_MAX_REGEX_PATTERN_LEN], text[:_MAX_REGEX_HAYSTACK],
                             re.IGNORECASE) is not None
        except re.error:
            return False
    # keyword / wallet / phone -> case-insensitive substring match.
    return pattern.lower() in text.lower()


def match_watchlists(conn: sqlite3.Connection, item_id: int, text: str) -> int:
    """Record a hit for every watchlist whose pattern matches `text`.

    Idempotent per (watchlist, item) via the table's UNIQUE constraint. Returns
    the number of new hits recorded.
    """
    watchlists = conn.execute(
        "SELECT id, pattern, kind FROM watchlists WHERE enabled = 1"
    ).fetchall()
    hits = 0
    for w in watchlists:
        if _matches(w["pattern"], w["kind"], text):
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist_hits (watchlist_id, item_id) VALUES (?, ?)",
                (w["id"], item_id),
            )
            hits += cur.rowcount or 0
    return hits


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
    return int(cur.lastrowid)


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
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # SQLite datetime('now') is UTC but carries no offset.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    conn.execute(
        """
        INSERT INTO sources (name, track, last_synced)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET last_synced = excluded.last_synced
        """,
        (name, track, when.isoformat()),
    )


# --- Intelligence Requirements (PIRs) ---------------------------------------
# Standing questions the analyst wants answered. The AI scores items against the
# enabled requirements; the Intel view ranks material by that relevance score.


def add_requirement(
    conn: sqlite3.Connection,
    question: str,
    priority: int = 2,
    topic: str | None = None,
    case_id: int | None = None,
) -> int:
    """Add a standing requirement (intelligence question).

    ``case_id`` binds the question to one Case (its Questions tab). None = a
    global question (shown on the standalone Intel/Requirements view). ``topic``
    is the legacy grouping key, kept only for backward compatibility.
    """
    topic = (topic or "").strip() or None
    cur = conn.execute(
        "INSERT INTO requirements (question, priority, enabled, topic, case_id) "
        "VALUES (?, ?, 1, ?, ?)",
        (question, priority, topic, case_id),
    )
    return int(cur.lastrowid)


def list_requirements(
    conn: sqlite3.Connection,
    only_enabled: bool = False,
    topic: str | None = None,
    case_id=_NO_CASE_FILTER,
) -> list[dict]:
    """All requirements (newest first) with how many items matched each.

    ``case_id`` filtering: omit it for all questions; pass ``None`` for only the
    global ones (not bound to any case); pass an int for one case's questions.
    """
    sql = """
        SELECT r.id, r.question, r.priority, r.enabled, r.created_at, r.topic,
               r.case_id,
               (SELECT COUNT(*) FROM requirement_hits h
                WHERE h.requirement_id = r.id AND h.score >= 1) AS match_count
        FROM requirements r
    """
    clauses: list[str] = []
    params: list = []
    if only_enabled:
        clauses.append("r.enabled = 1")
    if topic is not None:
        clauses.append("r.topic = ?")
        params.append((topic or "").strip())
    if case_id is not _NO_CASE_FILTER:
        if case_id is None:
            clauses.append("r.case_id IS NULL")
        else:
            clauses.append("r.case_id = ?")
            params.append(int(case_id))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.priority, r.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_requirement_enabled(
    conn: sqlite3.Connection, req_id: int, enabled: bool
) -> None:
    conn.execute(
        "UPDATE requirements SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, req_id),
    )


def delete_requirement(conn: sqlite3.Connection, req_id: int) -> None:
    conn.execute("DELETE FROM requirements WHERE id = ?", (req_id,))


def active_requirements(conn: sqlite3.Connection) -> list[dict]:
    """Enabled requirements, highest priority first (for the scoring pass)."""
    rows = conn.execute(
        """
        SELECT id, question, priority FROM requirements
        WHERE enabled = 1 ORDER BY priority, id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def unscored_requirements_for_item(
    conn: sqlite3.Connection, item_id: int
) -> list[dict]:
    """Enabled requirements this item has not been scored against yet."""
    rows = conn.execute(
        """
        SELECT r.id, r.question, r.priority FROM requirements r
        WHERE r.enabled = 1
          AND NOT EXISTS (
              SELECT 1 FROM requirement_hits h
              WHERE h.item_id = ? AND h.requirement_id = r.id
          )
        ORDER BY r.priority, r.id
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_requirement_scoring(
    conn: sqlite3.Connection, limit: int = 200
) -> list[dict]:
    """Newest items that still have at least one unscored enabled requirement.

    The work queue for the PIR scoring pass. Empty when no requirements exist,
    so the pass is a no-op until the analyst defines some.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content, i.language
        FROM items i
        WHERE EXISTS (
            SELECT 1 FROM requirements r
            WHERE r.enabled = 1
              AND NOT EXISTS (
                  SELECT 1 FROM requirement_hits h
                  WHERE h.item_id = i.id AND h.requirement_id = r.id
              )
        )
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_requirement_hit(
    conn: sqlite3.Connection,
    item_id: int,
    requirement_id: int,
    score: int,
    rationale: str | None,
    model: str | None,
) -> None:
    """Persist (idempotent) a relevance score of an item to a requirement."""
    conn.execute(
        """
        INSERT INTO requirement_hits
            (item_id, requirement_id, score, rationale, model, scored_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(item_id, requirement_id) DO UPDATE SET
            score = excluded.score,
            rationale = excluded.rationale,
            model = excluded.model,
            scored_at = excluded.scored_at
        """,
        (item_id, requirement_id, int(score), rationale, model),
    )


def intel_items(
    conn: sqlite3.Connection,
    requirement_id: int | None = None,
    min_score: int = 1,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Items ranked by Intelligence-Requirement relevance, highest first.

    With a `requirement_id`, returns that requirement's matches. Without one,
    returns each matching item once at its best score across all requirements.
    """
    base_cols = """
        i.id, i.source, i.url, i.author, i.title, i.content, i.language,
        i.published_at, i.fetched_at, i.cluster_id,
        COALESCE(c.shared_count, 1) AS shared_count,
        a.threat_level, a.summary, a.translation, a.target_lang,
        a.entities, a.party, a.contradiction, a.confidence
    """
    where = ["h.score >= ?"]
    params: list = [min_score]
    if requirement_id:
        where.append("h.requirement_id = ?")
        params.append(requirement_id)
    if since:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) >= ?")
        params.append(since)
    if until:
        where.append("substr(COALESCE(i.published_at, i.fetched_at), 1, 10) <= ?")
        params.append(until)
    if q:
        fts_q = build_fts_query(q.strip())
        if fts_q:
            where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            params.append(fts_q)
    where_sql = " AND ".join(where)

    if requirement_id:
        sql = f"""
            SELECT {base_cols},
                   h.score AS rel_score, h.rationale AS rel_rationale,
                   h.requirement_id AS rel_requirement_id, 1 AS rel_match_count
            FROM requirement_hits h
            JOIN items i ON i.id = h.item_id
            LEFT JOIN clusters c ON c.id = i.cluster_id
            LEFT JOIN analyses a ON a.item_id = i.id
            WHERE {where_sql}
            ORDER BY h.score DESC, COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ? OFFSET ?
        """
    else:
        sql = f"""
            SELECT {base_cols},
                   MAX(h.score) AS rel_score, NULL AS rel_rationale,
                   NULL AS rel_requirement_id,
                   COUNT(DISTINCT h.requirement_id) AS rel_match_count
            FROM requirement_hits h
            JOIN items i ON i.id = h.item_id
            LEFT JOIN clusters c ON c.id = i.cluster_id
            LEFT JOIN analyses a ON a.item_id = i.id
            WHERE {where_sql}
            GROUP BY i.id
            ORDER BY rel_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ? OFFSET ?
        """
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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
    return int(cur.lastrowid)


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
