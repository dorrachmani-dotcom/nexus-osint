"""Links between Obsidian notes (``[[wikilinks]]``) and their place on the graph.

Each scanned note keeps its outgoing wikilink targets in ``connector_docs.meta``.
:func:`refresh_note_links` turns those into rows of ``note_links`` (one per
note and target), resolving each target to a note of the same vault the way
Obsidian does: a target with a folder (``Projects/Alpha``) matches that path,
a bare name (``Alpha``) matches the note with that file name (the shortest
path wins when several share it). Targets that match no note are kept with
``dst_key`` NULL, so a note added later resolves on the next scan.

:func:`note_graph` picks the notes to draw on the relationship graph: notes
whose text names one of the entities already on the map (note -> entity
edges) and the wikilinks between the chosen notes (note -> note edges). With
no entities on the map (e.g. no AI configured) it shows the best-linked notes,
so the vault's own structure is still visible. Pure SQL and string matching:
no AI calls, nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
import sqlite3

KIND = "obsidian"
MAX_NOTES = 40          # notes drawn on one graph
SCAN_NOTES = 3000       # notes considered per graph build
_TEXT_CHARS = 200_000   # text searched per note for entity names


def _norm(target: str) -> str:
    t = target.strip().replace("\\", "/").strip("/").lower()
    for ext in (".markdown", ".md"):
        if t.endswith(ext):
            t = t[: -len(ext)]
    return t


def _resolver(keys: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Lookups for one vault: full path (no extension) and file name -> key."""
    by_path: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for key in sorted(keys, key=lambda k: (k.count("/"), len(k), k)):
        path = _norm(key)
        by_path.setdefault(path, key)
        by_name.setdefault(path.rsplit("/", 1)[-1], key)
    return by_path, by_name


def resolve(target: str, by_path: dict[str, str], by_name: dict[str, str]) -> str | None:
    t = _norm(target)
    if not t:
        return None
    if "/" in t:
        return by_path.get(t) or by_path.get(t.lstrip("./"))
    return by_name.get(t)


def refresh_note_links(conn: sqlite3.Connection, source_id: int | None = None) -> int:
    """Rebuild ``note_links`` for one vault (or all). Returns the rows written."""
    if source_id is None:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT source_id FROM connector_docs WHERE kind = ?", (KIND,))]
        conn.execute("DELETE FROM note_links WHERE source_id NOT IN "
                     "(SELECT source_id FROM connector_docs WHERE kind = ?)", (KIND,))
    else:
        ids = [int(source_id)]
    written = 0
    for sid in ids:
        rows = conn.execute(
            "SELECT doc_key, meta FROM connector_docs WHERE kind = ? AND source_id = ?",
            (KIND, sid)).fetchall()
        by_path, by_name = _resolver([r[0] for r in rows])
        links: list[tuple[int, str, str, str | None]] = []
        for key, meta in rows:
            try:
                targets = (json.loads(meta or "{}") or {}).get("links") or []
            except (ValueError, TypeError):
                targets = []
            seen: set[str] = set()
            for target in targets:
                if not isinstance(target, str) or not target.strip():
                    continue
                t = target.strip()[:300]
                if t.lower() in seen:
                    continue
                seen.add(t.lower())
                dst = resolve(t, by_path, by_name)
                links.append((sid, key, t, dst if dst != key else None))
        conn.execute("DELETE FROM note_links WHERE source_id = ?", (sid,))
        conn.executemany(
            "INSERT OR IGNORE INTO note_links (source_id, src_key, target, dst_key) "
            "VALUES (?, ?, ?, ?)", links)
        written += len(links)
    return written


def forget_vault(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM note_links WHERE source_id = ?", (int(source_id),))


def note_id(source_id: int, key: str) -> str:
    """Graph node id of a note (never collides with an entity name)."""
    return f"note:{int(source_id)}:{key}"


def note_graph(conn: sqlite3.Connection, entity_labels: list[str],
               max_notes: int = MAX_NOTES) -> dict:
    """Notes, note -> note links and note -> entity mentions for the graph.

    Returns ``{"notes": [...], "note_edges": [...], "entity_edges": [...]}``
    where each note is ``{"id","label","vault","path","document_id",
    "entities","links"}``, a note edge is ``{"source","target"}`` (note ids)
    and an entity edge is ``{"source": note id, "target": entity label}``.
    """
    rows = conn.execute(
        "SELECT c.source_id, c.doc_key, c.title, c.text, c.document_id, "
        "COALESCE(v.name, '') AS vault "
        "FROM connector_docs c LEFT JOIN obsidian_vaults v ON v.id = c.source_id "
        "WHERE c.kind = ? ORDER BY c.last_changed DESC LIMIT ?",
        (KIND, SCAN_NOTES)).fetchall()
    if not rows:
        return {"notes": [], "note_edges": [], "entity_edges": []}
    labels = sorted({lab for lab in entity_labels if lab and len(lab.strip()) >= 2},
                    key=len, reverse=True)
    pattern = None
    if labels:
        pattern = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(lab) for lab in labels) + r")(?!\w)",
            re.IGNORECASE)
    canon = {lab.lower(): lab for lab in labels}

    links: dict[tuple[int, str], set[str]] = {}
    for sid, src, dst in conn.execute(
            "SELECT source_id, src_key, dst_key FROM note_links WHERE dst_key IS NOT NULL"):
        links.setdefault((int(sid), src), set()).add(dst)
    degree: dict[tuple[int, str], int] = {}
    for (sid, src), dsts in links.items():
        degree[(sid, src)] = degree.get((sid, src), 0) + len(dsts)
        for d in dsts:
            degree[(sid, d)] = degree.get((sid, d), 0) + 1

    info: dict[tuple[int, str], dict] = {}
    mentions: dict[tuple[int, str], set[str]] = {}
    for r in rows:
        k = (int(r["source_id"]), r["doc_key"])
        info[k] = {"label": (r["title"] or r["doc_key"])[:120], "vault": r["vault"],
                   "path": r["doc_key"], "document_id": r["document_id"]}
        if pattern is not None:
            found = {canon[m.group(1).lower()] for m in pattern.finditer((r["text"] or "")[:_TEXT_CHARS])
                     if m.group(1).lower() in canon}
            if found:
                mentions[k] = found

    if mentions:
        # Notes that name entities on the map, most names first; then the
        # notes they link to (or are linked from), while there is room.
        chosen = sorted(mentions, key=lambda k: (-len(mentions[k]), -degree.get(k, 0), k))[:max_notes]
        chosen_set = set(chosen)
        for k in list(chosen):
            for d in sorted(links.get(k, ())):
                nk = (k[0], d)
                if len(chosen_set) >= max_notes:
                    break
                if nk in info and nk not in chosen_set:
                    chosen_set.add(nk)
    else:
        chosen_set = {k for k in sorted(info, key=lambda k: (-degree.get(k, 0), k))
                      if degree.get(k, 0) > 0}
        chosen_set = set(sorted(chosen_set, key=lambda k: (-degree.get(k, 0), k))[:max_notes])

    notes = []
    note_edges: list[dict[str, str]] = []
    entity_edges: list[dict[str, str]] = []
    for k in sorted(chosen_set, key=lambda k: (-len(mentions.get(k, ())), -degree.get(k, 0), k)):
        nid = note_id(*k)
        out = [d for d in sorted(links.get(k, ())) if (k[0], d) in chosen_set]
        notes.append({"id": nid, **info[k], "entities": sorted(mentions.get(k, ())),
                      "links": len(links.get(k, ()))})
        note_edges.extend({"source": nid, "target": note_id(k[0], d)} for d in out)
        entity_edges.extend({"source": nid, "target": e} for e in sorted(mentions.get(k, ())))
    return {"notes": notes, "note_edges": note_edges, "entity_edges": entity_edges}
