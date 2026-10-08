"""SQLite storage layer.

Single local database, WAL mode for concurrent reads during a scan, and an
FTS5 virtual table (kept in sync by triggers) powering full-text search across
the entire local history. `init_db()` is idempotent and safe to call on boot.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from nexus.config import get_settings

logger = logging.getLogger("nexus.db")

# --- Schema -----------------------------------------------------------------
# Notes:
#   * items.content_hash is UNIQUE -> a re-seen identical item bumps shared_count
#     instead of inserting a duplicate (handled in the storage helpers later).
#   * items_fts is an external-content FTS5 index over items(title, content,
#     summary); triggers keep it in lockstep with the base table.

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    track         TEXT NOT NULL DEFAULT 'api',
    last_synced   TEXT
);

CREATE TABLE IF NOT EXISTS clusters (
    id            TEXT PRIMARY KEY,             -- content_hash of the canonical item
    shared_count  INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash  TEXT NOT NULL UNIQUE,
    dedup_key     TEXT,                         -- normalized key for near-dup clustering
    source        TEXT NOT NULL,
    track         TEXT NOT NULL DEFAULT 'api',
    external_id   TEXT,
    url           TEXT,
    author        TEXT,
    title         TEXT,
    content       TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',     -- mirror of analyses.summary for FTS
    language      TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    media_urls    TEXT NOT NULL DEFAULT '[]',   -- JSON array
    raw           TEXT NOT NULL DEFAULT '{}',   -- JSON blob
    cluster_id    TEXT REFERENCES clusters(id)
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_dedup ON items(dedup_key);
CREATE INDEX IF NOT EXISTS idx_items_source_extid ON items(source, external_id);

CREATE TABLE IF NOT EXISTS analyses (
    item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    threat_level   TEXT NOT NULL DEFAULT 'none',
    summary        TEXT,
    translation    TEXT,                        -- translated to TRANSLATION_TARGET_LANG
    target_lang    TEXT,
    entities       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    party          TEXT NOT NULL DEFAULT 'unknown',
    contradiction  INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    analyzed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyses_threat ON analyses(threat_level);

CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    screenshot    TEXT NOT NULL,                -- relative path under data/
    sha256        TEXT NOT NULL,
    captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ocr_text      TEXT                          -- text read off the screenshot (OCR), if any
);

CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bookmarks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    case_id       INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, case_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER REFERENCES items(id) ON DELETE CASCADE,
    case_id       INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watchlists (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    pattern       TEXT NOT NULL,                -- keyword / wallet / phone
    kind          TEXT NOT NULL DEFAULT 'keyword',
    enabled       INTEGER NOT NULL DEFAULT 1,   -- 0 = paused, skipped during scan
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User-chosen collection topics (RSS feeds / search queries / channels).
-- Merged at scan time with the env-configured targets for each source.
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,                -- rss / serpapi / twitter / reddit / telegram
    value         TEXT NOT NULL,                -- feed URL / query / subreddit / channel
    label         TEXT,                         -- human-friendly name
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, value)
);

CREATE TABLE IF NOT EXISTS watchlist_hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id  INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    hit_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(watchlist_id, item_id)
);

-- Read/unread tracking. A row here means the analyst has marked the item read.
-- Kept separate from items so marking read never touches the FTS index.
CREATE TABLE IF NOT EXISTS item_reads (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    read_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- User-defined triage lists / lanes (e.g. "Very interesting", "Not interesting").
-- Personal organization on top of the feed; an item can sit in many lists.
CREATE TABLE IF NOT EXISTS lists (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    color         TEXT NOT NULL DEFAULT 'slate',  -- UI accent
    position      INTEGER NOT NULL DEFAULT 0,      -- manual ordering
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS list_memberships (
    list_id       INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_listmem_item ON list_memberships(item_id);

-- Key/value store for runtime-adjustable settings that are NOT secrets, e.g.
-- the chosen AI provider. API keys NEVER live here; they stay in .env (OpSec).
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A case's "word capsule": the tracking terms that drive its live feed. The
-- feed for a case is simply items matching ANY of these terms (reusing the same
-- term-search the old investigation board used). One subject -> one case -> its
-- own terms, replacing the standalone capsule/topic concept.
CREATE TABLE IF NOT EXISTS case_terms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    term          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(case_id, term)
);
CREATE INDEX IF NOT EXISTS idx_caseterms_case ON case_terms(case_id);

-- Intelligence Requirements (a.k.a. PIRs): standing questions the analyst wants
-- answered. The AI scores collected items for relevance to each requirement so
-- the most pertinent material surfaces first.
CREATE TABLE IF NOT EXISTS requirements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 2,    -- 1 high / 2 medium / 3 low
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One scored relevance edge between an item and a requirement.
CREATE TABLE IF NOT EXISTS requirement_hits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    requirement_id  INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    score           INTEGER NOT NULL DEFAULT 0,   -- 0..100 relevance
    rationale       TEXT,
    model           TEXT,
    scored_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, requirement_id)
);
CREATE INDEX IF NOT EXISTS idx_reqhits_req ON requirement_hits(requirement_id);
CREATE INDEX IF NOT EXISTS idx_reqhits_score ON requirement_hits(score);

-- User-defined API sources, added self-service from the dashboard (optionally
-- with AI auto-filling the config). The generic engine in sources/custom.py
-- runs these like any built-in source. SECRETS ARE NOT STORED HERE: the API key
-- (if any) lives in .env under CUSTOM_SOURCE_<id>_KEY, exactly like every other
-- secret. This table holds only non-secret connection config.
CREATE TABLE IF NOT EXISTS custom_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,                  -- label, also the source tag on items
    enabled       INTEGER NOT NULL DEFAULT 1,
    base_url      TEXT NOT NULL,
    endpoint      TEXT NOT NULL DEFAULT '',       -- path appended to base_url; may contain {query}
    http_method   TEXT NOT NULL DEFAULT 'GET',
    auth_type     TEXT NOT NULL DEFAULT 'none',   -- none | header | query | bearer
    auth_param    TEXT,                           -- header/param name (e.g. api_key, X-API-Key)
    query_param   TEXT,                           -- query param that carries the search term
    extra_params  TEXT NOT NULL DEFAULT '{}',     -- JSON of static query params
    items_path    TEXT NOT NULL DEFAULT '',       -- dot-path to the array of items in the JSON
    map_title     TEXT,                           -- dot-path within each item -> title
    map_content   TEXT,                           -- dot-path -> main text (required to be useful)
    map_url       TEXT,                           -- dot-path -> source link
    map_author    TEXT,                           -- dot-path -> author/handle
    map_published TEXT,                           -- dot-path -> publish date
    notes         TEXT,                           -- the AI's plan / human notes
    -- Auto-Adapt health: when an API keeps responding but the mapping yields no
    -- items (its shape drifted), the AI planner re-maps the fields automatically.
    consecutive_empty INTEGER NOT NULL DEFAULT 0, -- responsive-but-zero-mapped scans in a row
    last_sample   TEXT,                           -- most recent raw JSON response (for re-planning)
    last_adapt_at TEXT,                           -- when Auto-Adapt last re-mapped this source
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Entity index: one row per (item, AI-extracted entity). Built from
-- analyses.entities so "every item that mentions X" is an indexed lookup instead
-- of a full JSON scan. Powers the entity dossier, cross-case linking, and a
-- faster relationship graph. Rebuilt per item whenever its analysis is saved.
CREATE TABLE IF NOT EXISTS item_entities (
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,                 -- display form (keeps casing)
    name_norm TEXT NOT NULL,                 -- canonical key (storage._norm_entity_key)
    kind      TEXT,                          -- person | organization | location | identifier
    PRIMARY KEY (item_id, name_norm)
);
CREATE INDEX IF NOT EXISTS idx_item_entities_norm ON item_entities(name_norm);
CREATE INDEX IF NOT EXISTS idx_item_entities_item ON item_entities(item_id);

-- Optional semantic-search vectors: one embedding per item, computed on demand
-- via a LOCAL Ollama model (no cloud, no heavy Python ML dependency). Absent
-- when no embedding backend is available — the feature degrades gracefully.
CREATE TABLE IF NOT EXISTS item_embeddings (
    item_id    INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    model      TEXT,
    vec        TEXT,                          -- JSON array of floats
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full-text search over items (external content).
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content, summary,
    content='items', content_rowid='id', tokenize='unicode61'
);

-- Keep the FTS index in sync with items + analyses.
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, content, summary)
    VALUES (new.id, new.title, new.content, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, summary)
    VALUES ('delete', old.id, old.title, old.content, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, summary)
    VALUES ('delete', old.id, old.title, old.content, old.summary);
    INSERT INTO items_fts(rowid, title, content, summary)
    VALUES (new.id, new.title, new.content, new.summary);
END;
"""


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Context-managed connection that commits on success, rolls back on error."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Closed set of tables this module manages. `_column_exists` interpolates the
# table name into a PRAGMA (PRAGMA cannot take bound parameters), so it must be
# validated against this allow-list before use.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KNOWN_TABLES = frozenset({
    "sources", "clusters", "items", "analyses", "evidence", "cases", "bookmarks",
    "notes", "watchlists", "subscriptions", "watchlist_hits", "item_reads", "lists",
    "list_memberships", "meta", "case_terms", "requirements", "requirement_hits",
    "custom_sources", "item_entities", "item_embeddings", "entity_aliases",
})


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _IDENT_RE.fullmatch(table) or table not in _KNOWN_TABLES:
        raise ValueError(f"unknown table name: {table!r}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    """Create the schema if it does not exist. Idempotent."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Lightweight, idempotent migrations for columns added after the initial
        # schema. Adding a column to an existing DB never destroys data.
        if not _column_exists(conn, "requirements", "topic"):
            # Groups sub-questions under one investigation (the capsule name).
            conn.execute("ALTER TABLE requirements ADD COLUMN topic TEXT")
        if not _column_exists(conn, "analyses", "confidence"):
            # Model self-confidence in the assessment: low / medium / high.
            conn.execute("ALTER TABLE analyses ADD COLUMN confidence TEXT")
        if not _column_exists(conn, "cases", "status"):
            # Case lifecycle: 'open' (active work) / 'closed' (resolved/archived).
            conn.execute(
                "ALTER TABLE cases ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )
        if not _column_exists(conn, "cases", "priority"):
            # Triage priority: 'high' / 'medium' / 'low'.
            conn.execute(
                "ALTER TABLE cases ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'"
            )
        if not _column_exists(conn, "evidence", "ocr_text"):
            # Text extracted from the evidence screenshot via OCR (best-effort).
            conn.execute("ALTER TABLE evidence ADD COLUMN ocr_text TEXT")
        # Auto-Adapt health tracking for user-defined API sources.
        if not _column_exists(conn, "custom_sources", "consecutive_empty"):
            conn.execute(
                "ALTER TABLE custom_sources ADD COLUMN "
                "consecutive_empty INTEGER NOT NULL DEFAULT 0"
            )
        if not _column_exists(conn, "custom_sources", "last_sample"):
            conn.execute("ALTER TABLE custom_sources ADD COLUMN last_sample TEXT")
        if not _column_exists(conn, "custom_sources", "last_adapt_at"):
            conn.execute("ALTER TABLE custom_sources ADD COLUMN last_adapt_at TEXT")
        # Case hub: a case can hold sub-cases (one level) and own its questions.
        if not _column_exists(conn, "cases", "parent_id"):
            conn.execute(
                "ALTER TABLE cases ADD COLUMN parent_id INTEGER "
                "REFERENCES cases(id) ON DELETE SET NULL"
            )
        if not _column_exists(conn, "requirements", "case_id"):
            conn.execute(
                "ALTER TABLE requirements ADD COLUMN case_id INTEGER "
                "REFERENCES cases(id) ON DELETE CASCADE"
            )
        if not _column_exists(conn, "case_terms", "kind"):
            conn.execute(
                "ALTER TABLE case_terms ADD COLUMN kind TEXT NOT NULL DEFAULT 'required'"
            )
        if not _column_exists(conn, "lists", "case_id"):
            conn.execute(
                "ALTER TABLE lists ADD COLUMN case_id INTEGER "
                "REFERENCES cases(id) ON DELETE CASCADE"
            )
        # Entity alias table: maps an alias name to a canonical display name so
        # "Messi" and "Lionel Messi" collapse into one node on the entity graph.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_aliases (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                alias      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                canonical  TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        if not _column_exists(conn, "watchlists", "enabled"):
            conn.execute(
                "ALTER TABLE watchlists ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        # Dismiss column: lets the analyst mark an item as noise / not relevant
        # so it disappears from the feed without being deleted.
        if not _column_exists(conn, "items", "dismissed"):
            conn.execute(
                "ALTER TABLE items ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0"
            )
        _migrate_capsules_to_cases(conn)
        _backfill_entity_index(conn)


def _backfill_entity_index(conn: sqlite3.Connection) -> None:
    """One-time: populate item_entities from existing analyses.entities JSON.

    New analyses index themselves (storage.save_analysis), but a database created
    before the index existed needs a single backfill pass. Guarded by a meta flag
    so it runs once; fail-soft (leaves the flag unset to retry) so a hiccup never
    blocks boot. Reuses storage's entity decoder/normaliser (lazy import — storage
    does not import db, so there is no cycle)."""
    if conn.execute(
        "SELECT 1 FROM meta WHERE key = 'entity_index_backfilled'"
    ).fetchone() is not None:
        return
    try:
        from nexus.storage import (
            _clean_entity_display,
            _decode_entities,
            _entity_kind_map,
            _norm_entity_key,
        )

        rows = conn.execute(
            "SELECT item_id, entities FROM analyses WHERE entities IS NOT NULL"
        ).fetchall()
        for r in rows:
            groups, flat = _decode_entities(r["entities"])
            kind_map = _entity_kind_map(groups)
            seen: set[str] = set()
            for raw_name in flat:
                norm = _norm_entity_key(raw_name)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                conn.execute(
                    "INSERT OR IGNORE INTO item_entities "
                    "(item_id, name, name_norm, kind) VALUES (?, ?, ?, ?)",
                    (r["item_id"], _clean_entity_display(raw_name), norm,
                     kind_map.get(norm, "entity")),
                )
    except Exception:
        logger.exception("entity-index backfill failed; will retry next start")
        return
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES "
        "('entity_index_backfilled', '1')"
    )


def _migrate_capsules_to_cases(conn: sqlite3.Connection) -> None:
    """One-time: fold legacy "query capsules" and topic-linked questions into Cases.

    Historically a subject was scattered: a *capsule* (subscriptions rows with
    ``source='query'`` sharing a ``label`` = capsule name, each ``value`` a search
    term) plus *requirements* loosely tied to it by a ``topic`` text column, plus
    a separate manual *case*. The unified model makes the Case the single hub, so
    on first run we consolidate: for each capsule we find/create a top-level Case
    of that name, copy its terms into ``case_terms``, and re-link its
    ``topic``-scoped requirements via ``requirements.case_id``.

    Guarded by a ``meta`` flag so it runs exactly once. Never deletes the legacy
    rows (no data loss); a failure leaves the flag unset to retry next launch.
    """
    if conn.execute(
        "SELECT 1 FROM meta WHERE key = 'migrated_capsules_to_cases'"
    ).fetchone() is not None:
        return
    try:
        rows = conn.execute(
            "SELECT value, label FROM subscriptions WHERE source = 'query'"
        ).fetchall()
        capsules: dict[str, list[str]] = {}
        for r in rows:
            name = ((r["label"] or "").strip() or "General")
            term = (r["value"] or "").strip()
            if term:
                capsules.setdefault(name, []).append(term)
        for name, terms in capsules.items():
            crow = conn.execute(
                "SELECT id FROM cases WHERE name = ? AND parent_id IS NULL", (name,)
            ).fetchone()
            if crow is None:
                cur = conn.execute("INSERT INTO cases (name) VALUES (?)", (name,))
                case_id = int(cur.lastrowid)
            else:
                case_id = int(crow["id"])
            for term in terms:
                conn.execute(
                    "INSERT OR IGNORE INTO case_terms (case_id, term) VALUES (?, ?)",
                    (case_id, term),
                )
            conn.execute(
                "UPDATE requirements SET case_id = ? "
                "WHERE case_id IS NULL AND topic = ?",
                (case_id, name),
            )
    except Exception:
        logger.exception("capsule->case migration failed; will retry next start")
        return
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES "
        "('migrated_capsules_to_cases', '1')"
    )
