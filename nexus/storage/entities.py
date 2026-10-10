"""Entity helpers: decoding the stored entity JSON, normalisation keys,
display cleanup, kind maps and analyst-defined aliases.
"""

from __future__ import annotations

import json
import sqlite3

# --- Feed presentation enrichment -------------------------------------------
# Feed rows come from search_items / list_items / intel_items / case_items as
# raw DB columns. `enrich_feed_rows` augments them in one batched pass with the
# derived fields the card template needs (typed entities, related sources in
# the same cluster, evidence-captured flag, the best intelligence-requirement
# match, a source-reliability label, a short display URL, and a relative time)
# without N+1 queries. It is purely additive and safe to call on any feed rows.
_ENTITY_GROUP_KEYS = ("people", "organizations", "locations", "identifiers")


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
_ENTITY_EDGE_CHARS = " \t\r\n\"'`.,;:!?()[]{}“”‘’«»"  # noqa: RUF001 (curly quotes on purpose)


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


def _entity_tokens(raw) -> set[str]:
    """Normalized lowercase entity strings from a stored entities value."""
    _typed, flat = _decode_entities(raw)
    return {t.strip().lower() for t in flat if t and len(t.strip()) >= 3}
