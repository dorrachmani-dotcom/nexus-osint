"""Export a Case as an Obsidian-ready Markdown vault (a .zip of .md notes).

Obsidian (https://obsidian.md) is a popular local Markdown knowledge base with a
first-class **graph view**. By emitting one note per item, linking the entities
the AI extracted as ``[[wikilinks]]``, and an index note for the case, the
analyst can drop the folder into Obsidian and immediately *see and navigate* the
web of people, organisations and places in the case — a richer, interactive
companion to the built-in relationship graph.

Everything is local and derived from already-stored data: no AI calls, no
network. The vault carries only collected open-source content and its analysis —
never API keys or settings.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from nexus.storage import (
    _decode_entities,
    case_live_items,
    case_notes,
    case_terms,
    get_case,
    list_requirements,
)

logger = logging.getLogger("nexus.obsidian")

# Characters Obsidian/most filesystems dislike in a note name.
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def _safe_name(text: str, *, fallback: str = "untitled", limit: int = 80) -> str:
    """A filesystem- and Obsidian-safe note name (no separators or link chars)."""
    name = _BAD_NAME.sub(" ", (text or "").strip())
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:limit].strip() or fallback)


def _wikilink(name: str) -> str:
    """An Obsidian [[wikilink]] whose target is a safe note name."""
    return f"[[{_safe_name(name, fallback='entity')}]]"


def _yaml_escape(value: str) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def build_case_vault(conn, case_id: int) -> tuple[bytes, dict] | None:
    """Build an Obsidian vault (.zip) for a case. Returns (zip_bytes, summary)
    or ``None`` if the case doesn't exist."""
    case = get_case(conn, case_id)
    if case is None:
        return None

    case_name = case["name"]
    case_note = _safe_name(case_name, fallback=f"Case {case_id}")
    terms = [t["term"] for t in case_terms(conn, case_id)]
    questions = [r["question"] for r in list_requirements(conn, case_id=case_id)]
    items = case_live_items(conn, case_id)
    notes = case_notes(conn, case_id)

    buffer = io.BytesIO()
    item_links: list[str] = []
    all_entities: set[str] = set()
    used_names: set[str] = set()
    item_count = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for it in items:
            title = it.get("title") or f"item-{it.get('id')}"
            base = _safe_name(title, fallback=f"item-{it.get('id')}")
            # Disambiguate duplicate titles so notes don't overwrite each other.
            note_name = base
            n = 2
            while note_name.lower() in used_names:
                note_name = f"{base} ({n})"
                n += 1
            used_names.add(note_name.lower())

            _groups, flat = _decode_entities(it.get("entities"))
            ents = []
            seen_e: set[str] = set()
            for e in flat:
                clean = _safe_name(e, fallback="")
                if clean and clean.lower() not in seen_e:
                    seen_e.add(clean.lower())
                    ents.append(clean)
                    all_entities.add(clean)

            tags = ["nexus", f"case/{case_note}"]
            threat = it.get("threat_level") or "none"
            if threat and threat != "none":
                tags.append(f"threat/{threat}")

            fm = ["---"]
            fm.append(f"source: {_yaml_escape(it.get('source') or '')}")
            if it.get("url"):
                fm.append(f"url: {_yaml_escape(it['url'])}")
            if it.get("published_at"):
                fm.append(f"published: {_yaml_escape(it['published_at'])}")
            fm.append(f"threat: {threat}")
            fm.append(f"case: {_yaml_escape(case_name)}")
            fm.append(f"tags: [{', '.join(tags)}]")
            fm.append("---")

            body = [f"# {title}", ""]
            summary = it.get("summary") or ""
            if summary:
                body += [summary, ""]
            translation = it.get("translation") or ""
            if translation and translation != summary:
                body += ["> " + translation.replace("\n", "\n> "), ""]
            content = (it.get("content") or "").strip()
            if content and content != summary:
                body += [content, ""]
            if ents:
                body += ["**Entities:** " + " · ".join(_wikilink(e) for e in ents), ""]
            if it.get("url"):
                body += [f"[Open source]({it['url']})", ""]
            body += [f"Part of {_wikilink(case_name)}.", ""]

            zf.writestr(f"items/{note_name}.md", "\n".join([*fm, "", *body]))
            item_links.append(f"- [[{note_name}]]")
            item_count += 1

        # The case index note: metadata + tracking words + questions + links.
        idx = ["---", "type: case", f"status: {case.get('status') or 'open'}",
               f"priority: {case.get('priority') or 'medium'}",
               "tags: [nexus, case]", "---", "", f"# {case_name}", ""]
        if case.get("description"):
            idx += [case["description"], ""]
        if terms:
            idx += ["## Tracking words", ", ".join(terms), ""]
        if questions:
            idx += ["## Intelligence questions"] + [f"- {q}" for q in questions] + [""]
        if notes:
            idx += ["## Researcher notes"] + [f"- {n['body']}" for n in notes if n.get("body")] + [""]
        if all_entities:
            idx += ["## Key entities",
                    " · ".join(_wikilink(e) for e in sorted(all_entities)), ""]
        idx += [f"## Items ({item_count})"] + (item_links or ["_No tracked items yet._"]) + [""]
        zf.writestr(f"{case_note}.md", "\n".join(idx))

        # A tiny README so the folder is self-explanatory when opened.
        zf.writestr(
            "README.md",
            "# Nexus-OSINT export\n\n"
            f"Open this folder as an Obsidian vault, then open **{case_note}** and\n"
            "switch on the graph view to explore the connections.\n",
        )

    summary = {
        "case": case_name,
        "items": item_count,
        "entities": len(all_entities),
        "questions": len(questions),
        "terms": len(terms),
    }
    return buffer.getvalue(), summary
