"""AI query builder — plain-English brief -> concrete search queries.

The analyst describes, in free text, what they want to learn about a subject
(their EEI / intelligence requirements). This module asks the active AI provider
to turn that into a small set of concrete search-query strings plus a few
standing intelligence questions, which the Topics page then saves as a capsule.

Same graceful-degradation contract as the rest of the analysis layer: with no
usable provider it returns ``{"enabled": False}`` and the UI falls back to the
manual capsule form. It never raises — parsing is defensive and any failure
yields an empty plan.
"""

from __future__ import annotations

import json
import logging
import re

from nexus.analysis.prompts import (
    build_query_builder_system_prompt,
    build_query_builder_user_prompt,
)
from nexus.analysis.providers import get_provider
from nexus.config import Settings, get_settings

logger = logging.getLogger("nexus.analysis")

# Pull the first JSON object out of a model reply, tolerating stray prose.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# A single JSON string literal, with escape sequences kept intact so we can
# re-parse it. Used to salvage array elements from a truncated reply.
_STRING_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

# Defensive caps so a runaway model reply can't flood the capsule.
_MAX_QUERIES = 8
_MAX_REQUIREMENTS = 4


def _clean_list(value, limit: int) -> list[str]:
    """Coerce a model value into a de-duplicated list of clean strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for el in value:
        text = str(el or "").strip()
        # Drop wrapping quotes the model sometimes adds.
        text = text.strip('"').strip("'").strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _salvage_array(text: str, key: str) -> list[str]:
    """Pull the string elements of a named JSON array out of a reply, even when
    the model's output was truncated before the array's closing ``]``.

    Small/verbose models (and any reply that hits the token ceiling) routinely
    return a JSON object that is cut off mid-array. ``json.loads`` rejects the
    whole thing, but the elements emitted so far are perfectly good — so we find
    the array by key and collect every *complete* string literal up to the
    closing bracket (or end of text). A half-written final element with no
    closing quote is simply dropped.
    """
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if not m:
        return []
    start = m.end()
    end = text.find("]", start)
    segment = text[start:end] if end != -1 else text[start:]
    out: list[str] = []
    for lit in _STRING_LITERAL_RE.finditer(segment):
        try:
            out.append(json.loads('"' + lit.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return out


def _parse_plan(text: str) -> dict:
    text = text or ""
    # 1) Happy path: a clean, complete JSON object.
    match = _JSON_OBJECT_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return {
                "queries": _clean_list(data.get("queries"), _MAX_QUERIES),
                "requirements": _clean_list(
                    data.get("requirements"), _MAX_REQUIREMENTS
                ),
            }
    # 2) Salvage: the reply was truncated or malformed (very common with local
    #    models or when the token budget is hit). Recover whatever complete
    #    array elements the model did produce rather than throwing it all away.
    queries = _salvage_array(text, "queries")
    requirements = _salvage_array(text, "requirements")
    if queries or requirements:
        return {
            "queries": _clean_list(queries, _MAX_QUERIES),
            "requirements": _clean_list(requirements, _MAX_REQUIREMENTS),
        }
    logger.warning("Query builder returned unparseable output; treating as empty.")
    return {"queries": [], "requirements": []}


def generate_plan(name: str, brief: str, settings: Settings | None = None) -> dict:
    """Turn a plain-English brief into a collection plan.

    Returns ``{"enabled": bool, "queries": [...], "requirements": [...]}``.
    ``enabled`` is False when no AI provider is usable (the caller then shows the
    manual form). Never raises.
    """
    settings = settings or get_settings()
    name = (name or "").strip()
    brief = (brief or "").strip()
    if not brief:
        return {"enabled": True, "queries": [], "requirements": []}

    provider = get_provider(settings)
    if provider is None:
        return {"enabled": False, "queries": [], "requirements": []}

    try:
        text = provider.complete(
            build_query_builder_system_prompt(settings.translation_target_lang),
            build_query_builder_user_prompt(name, brief),
            # Roomy enough for 8 queries + 4 full-sentence requirements without
            # truncation; the parser also salvages a truncated reply as a
            # belt-and-braces fallback.
            max_tokens=1200,
        )
    except Exception:
        logger.exception("Query builder generation failed")
        return {"enabled": True, "queries": [], "requirements": [], "error": True}

    plan = _parse_plan(text)
    plan["enabled"] = True
    return plan
