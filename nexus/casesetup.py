"""Make a brand-new case immediately useful.

When an analyst "opens a case", they shouldn't have to separately think up
tracking words — the case and its word-capsule should come together. This module
seeds a new case's tracking words from its name straight away (so its live feed
works even with no AI), and, when an AI provider is configured, expands them with
related terms and a few starter questions.

Used by both the Cases form and Sherlock's create-case action, so "open a case
on X" behaves the same everywhere.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("nexus.casesetup")

# Split a case name into search-worthy parts on common separators.
_SPLIT = re.compile(r"\s*(?:&|\+|,|/|\||\band\b|\bvs\.?\b|–|—|-)\s*", re.IGNORECASE)
_STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "case", "investigation", "watch"}


def _split_name(name: str) -> list[str]:
    """Meaningful tracking words from a case name. Keeps the parts (e.g.
    'Messi & World Cup' -> ['Messi', 'World Cup']) and the whole name if short."""
    name = (name or "").strip()
    if not name:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        term = term.strip()
        key = term.lower()
        if not term or key in seen:
            return
        # Skip parts that are only stop-words / a single short token.
        words = [w for w in re.split(r"\s+", term) if w]
        if all(w.lower() in _STOP for w in words):
            return
        if len(term) < 2:
            return
        seen.add(key)
        out.append(term)

    for part in _SPLIT.split(name):
        _add(part)
    # Also keep the full name as a phrase when it's short and not already covered.
    if len(name) <= 40 and len(out) != 1:
        _add(name)
    return out


def auto_setup_case(
    conn, case_id: int, name: str, description: str = "",
    *, settings=None, brief: str = "",
) -> dict:
    """Seed a new case's tracking words (+ AI questions). Returns a small summary.

    Never raises — a brand-new case always gets at least the name-derived words,
    and AI expansion is best-effort on top.
    """
    from nexus.storage import add_case_term, add_requirement

    added_terms = 0
    added_questions = 0

    # 1) Always seed from the name so the case is useful even with no AI.
    for term in _split_name(name):
        add_case_term(conn, case_id, term)
        added_terms += 1

    # 2) AI expansion: related terms + starter questions, when a model is set up.
    try:
        from nexus.config import get_settings

        settings = settings or get_settings()
        if settings.analysis_enabled:
            from nexus.analysis.query_builder import generate_plan

            seed = (brief or description or name or "").strip()
            plan = generate_plan(name, seed, settings) or {}
            before = added_terms
            for term in plan.get("queries", []) or []:
                if (term or "").strip():
                    add_case_term(conn, case_id, term.strip())
                    added_terms += 1
            for q in plan.get("requirements", []) or []:
                if (q or "").strip():
                    add_requirement(conn, q.strip(), priority=1, case_id=case_id)
                    added_questions += 1
            logger.info("auto_setup_case %s: +%s AI term(s), +%s question(s)",
                        case_id, added_terms - before, added_questions)
    except Exception:
        logger.exception("auto_setup_case: AI expansion failed (kept name words)")

    return {"terms": added_terms, "questions": added_questions}
