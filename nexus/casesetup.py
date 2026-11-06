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
import threading

logger = logging.getLogger("nexus.casesetup")

# Split a case name into search-worthy parts on common separators. No
# surrounding \s* in the pattern (that made it polynomial); _add() strips parts.
_SPLIT = re.compile(r"&|\+|,|/|\||\band\b|\bvs\.?\b|–|—|-", re.IGNORECASE)  # noqa: RUF001 (dashes are intentional separators)
_STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "case", "investigation", "watch"}


def _split_name(name: str) -> list[str]:
    """Meaningful tracking words from a case name. Keeps the parts (e.g.
    'Acme & Globex' -> ['Acme', 'Globex']) and the whole name if short."""
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


def _apply_plan(conn, case_id: int, plan: dict) -> tuple[int, int]:
    """Store an AI plan on a case; returns (terms added, questions added)."""
    from nexus.storage import add_case_term, add_requirement

    terms = questions = 0
    for term in plan.get("queries", []) or []:
        if (term or "").strip():
            add_case_term(conn, case_id, term.strip())
            terms += 1
    for q in plan.get("requirements", []) or []:
        if (q or "").strip():
            add_requirement(conn, q.strip(), priority=1, case_id=case_id)
            questions += 1
    logger.info("auto_setup_case %s: +%s AI term(s), +%s question(s)",
                case_id, terms, questions)
    return terms, questions


def _expand_in_background(case_id: int, name: str, seed: str, settings) -> None:
    """Ask the model, then store the result on a short connection of its own."""
    from nexus.analysis.query_builder import generate_plan
    from nexus.db import get_connection
    from nexus.storage import get_case

    try:
        plan = generate_plan(name, seed, settings) or {}
        if not (plan.get("queries") or plan.get("requirements")):
            return
        with get_connection() as conn:
            if get_case(conn, case_id) is not None:  # may be deleted by now
                _apply_plan(conn, case_id, plan)
    except Exception:
        logger.exception("auto_setup_case: background AI expansion failed (kept name words)")


def auto_setup_case(
    conn, case_id: int, name: str, description: str = "",
    *, settings=None, brief: str = "", background: bool = False,
) -> dict:
    """Seed a new case's tracking words (+ AI questions). Returns a small summary.

    Never raises — a brand-new case always gets at least the name-derived words,
    and AI expansion is best-effort on top. With ``background`` the model is
    asked on a separate thread, so a slow or unreachable model can't delay the
    caller; the summary then counts the name-derived words only.
    """
    from nexus.storage import add_case_term

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
            # Never hold the write lock across a model call: a provider logs each
            # call on its own connection, which would wait out busy_timeout here
            # and then lose the row. This also keeps a new case if the call dies.
            conn.commit()
            seed = (brief or description or name or "").strip()
            if background:
                threading.Thread(
                    target=_expand_in_background, args=(case_id, name, seed, settings),
                    name="case-ai-setup", daemon=True,
                ).start()
            else:
                from nexus.analysis.query_builder import generate_plan

                plan = generate_plan(name, seed, settings) or {}
                terms, added_questions = _apply_plan(conn, case_id, plan)
                added_terms += terms
    except Exception:
        logger.exception("auto_setup_case: AI expansion failed (kept name words)")

    return {"terms": added_terms, "questions": added_questions}
