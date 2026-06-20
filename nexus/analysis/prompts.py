"""Prompt construction for the Claude intelligence core.

The system prompt is a stable, reusable block so it benefits from prompt
caching across a batch (it never changes within a run). Per-item content goes
in the user turn. The model is asked for strict JSON so the result maps cleanly
onto the Analysis model.
"""

from __future__ import annotations

import json

# Stable across every call in a run -> good prompt-cache candidate.
SYSTEM_PROMPT = """You are an OSINT intelligence analyst embedded in a local \
research platform. For each item of open-source content you receive, produce a \
concise, neutral, evidence-based assessment.

Return ONLY a single JSON object (no markdown, no prose) with exactly these keys:
- "threat_level": one of "none", "low", "medium", "high", "critical".
  Judge the security/safety relevance of the content, not how upsetting it is.
- "summary": one or two sentences, in {target_lang}, capturing the core claim
  or event. Be factual and source-agnostic.
- "translation": the content translated into {target_lang}. If the content is
  already in {target_lang}, return an empty string.
- "entities": a JSON object grouping notable named entities by type, with exactly
  these four keys, each a JSON array of strings (use an empty array if none):
  - "people": individual person names.
  - "organizations": companies, agencies, groups, handles/accounts.
  - "locations": countries, cities, regions, named places.
  - "identifiers": technical or financial identifiers (domains, IPs, emails,
    crypto wallets, phone numbers, hashes).
- "party": "first_party" if the author is describing their own actions/words,
  "third_party" if reporting on someone else, "unknown" if unclear.
- "contradiction": true only if the content shows clear signs of disinformation
  or directly contradicts well-established facts; otherwise false.
- "confidence": your confidence in this overall assessment given the available
  text, one of "low", "medium", "high".

Do not invent facts. If information is missing, prefer "none"/"unknown"/empty."""


def build_system_prompt(target_lang: str) -> str:
    """System prompt with the configured translation target language baked in."""
    return SYSTEM_PROMPT.format(target_lang=target_lang)


def build_user_prompt(title: str | None, content: str | None, language: str | None) -> str:
    """User turn carrying the single item to analyse."""
    payload = {
        "title": title or "",
        "content": content or "",
        "detected_language": language or "unknown",
    }
    return (
        "Analyse the following open-source item and respond with the JSON object "
        "described in your instructions.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# --- Intelligence Requirements (PIR) scoring --------------------------------
# A separate, stable system prompt for scoring how well an item answers each of
# the analyst's standing intelligence questions. Stable text -> cacheable.

REQUIREMENTS_SYSTEM_PROMPT = """You are an OSINT collection manager. You are \
given one open-source item and a list of standing intelligence requirements \
(questions the analyst needs answered). For each requirement, judge how much \
this specific item helps answer it.

Return ONLY a JSON array (no markdown, no prose). Each element must be an object \
with exactly these keys:
- "id": the integer id of the requirement, copied verbatim from the input.
- "score": an integer 0-100. 0 = completely irrelevant; 100 = directly and \
strongly answers the requirement. Be strict: most items are irrelevant to most \
requirements and should score low. Reserve scores above 60 for items that \
genuinely bear on the question.
- "rationale": one short sentence (in {target_lang}) explaining the score.

Include exactly one element for every requirement id provided, in any order. \
Judge relevance to the question itself, not how dramatic the item is."""


def build_requirements_system_prompt(target_lang: str) -> str:
    return REQUIREMENTS_SYSTEM_PROMPT.format(target_lang=target_lang)


def build_requirements_user_prompt(
    title: str | None,
    content: str | None,
    language: str | None,
    requirements: list[dict],
) -> str:
    """User turn: one item plus the requirements to score it against."""
    payload = {
        "item": {
            "title": title or "",
            "content": content or "",
            "detected_language": language or "unknown",
        },
        "requirements": [
            {"id": int(r["id"]), "question": r["question"]} for r in requirements
        ],
    }
    return (
        "Score the item against each requirement and respond with the JSON array "
        "described in your instructions.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# --- Query builder: plain-English brief -> concrete search queries ----------
# Turns an analyst's free-text intelligence brief ("I want to track Google's new
# product launches, layoffs and stock moves") into (a) a short set of concrete
# search-query strings to broadcast to every keyword-capable source, and (b) a
# few standing intelligence questions (PIRs) the AI can rank results against.
# Stable system text -> cacheable.

QUERY_BUILDER_SYSTEM_PROMPT = """You are an OSINT collection planner. The analyst \
gives you a subject and a plain-English description of what they want to learn \
about it. Turn that into a concrete collection plan.

Return ONLY a single JSON object (no markdown, no prose) with exactly these keys:
- "queries": a JSON array of 4-8 short search-query strings, each ready to paste \
into a news/social search box. Make them specific and varied so together they \
cover the analyst's interest from several angles. Prefer real entity names, \
products and event terms over vague words. Keep each query under ~6 words. Do \
NOT use site-specific operators or quotes unless essential. No duplicates.
- "requirements": a JSON array of 2-4 standing intelligence questions (full \
sentences, in {target_lang}) capturing what the analyst ultimately wants \
answered. These are used to rank collected items by relevance.

Base everything strictly on the analyst's stated interest. Do not invent \
unrelated topics."""


def build_query_builder_system_prompt(target_lang: str) -> str:
    return QUERY_BUILDER_SYSTEM_PROMPT.format(target_lang=target_lang)


def build_query_builder_user_prompt(name: str, brief: str) -> str:
    """User turn: the subject plus the analyst's plain-English brief."""
    payload = {"subject": name or "", "brief": brief or ""}
    return (
        "Build the collection plan and respond with the JSON object described in "
        "your instructions.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
