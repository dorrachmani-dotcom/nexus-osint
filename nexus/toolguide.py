"""A plain-language catalogue of the optional OSINT command-line tools.

One source of truth, used by two places:
  * the Tools page — to render an install guide written for non-technical users;
  * Sherlock (the assistant) — so when asked "how can I look up X" it can name
    the right tool and how to get it.

Nothing here runs anything; it's descriptive metadata only. The easiest way to
get every tool is to run Nexus with Docker (they're pre-installed). The manual
``install`` steps are for people running Nexus directly.
"""

from __future__ import annotations

import re

# Each tool: key (matches the adapter name), a friendly title, what it does in
# one sentence, who it's for / when to reach for it, the manual install step,
# an optional extra-setup note, and an example target. ``trigger`` words help
# Sherlock map a question to a tool.
TOOL_CATALOG: list[dict] = [
    {
        "key": "sherlock", "title": "Sherlock — username search",
        "what": "Checks hundreds of websites to see where a username/handle exists.",
        "who": "Find someone's accounts from a nickname or handle.",
        "install": "pipx install sherlock-project",
        "note": "This is the command-line tool — not the in-app assistant of the same name.",
        "example": "johndoe", "triggers": ["username", "handle", "nickname", "accounts", "profiles"],
    },
    {
        "key": "maigret", "title": "Maigret — deep username footprint",
        "what": "Like Sherlock but deeper: hundreds of sites plus profile details, in one report.",
        "who": "Map a person's whole digital footprint from a single username.",
        "install": "pipx install maigret", "note": "",
        "example": "johndoe", "triggers": ["username", "footprint", "digital footprint", "profiles"],
    },
    {
        "key": "holehe", "title": "holehe — email → accounts",
        "what": "Tells you which sites an email address is registered on, quietly (no alert to the owner).",
        "who": "See where an email address is used online.",
        "install": "pipx install holehe", "note": "",
        "example": "name@example.com", "triggers": ["email", "registered", "accounts", "signed up"],
    },
    {
        "key": "socialscan", "title": "socialscan — handle/email availability",
        "what": "Checks whether a username or email is already taken on major platforms.",
        "who": "Quickly see where a handle or email is in use.",
        "install": "pipx install socialscan", "note": "",
        "example": "johndoe", "triggers": ["username", "email", "available", "taken"],
    },
    {
        "key": "h8mail", "title": "h8mail — breach lookup",
        "what": "Searches public data-breach records for an email address.",
        "who": "Check whether an email appeared in known leaks.",
        "install": "pipx install h8mail", "note": "Some sources need free API keys for more results.",
        "example": "name@example.com", "triggers": ["breach", "leak", "data breach", "pwned", "email"],
    },
    {
        "key": "theHarvester", "title": "theHarvester — domain recon",
        "what": "Collects emails, sub-domains and IPs tied to a company domain.",
        "who": "Map an organisation's online surface.",
        "install": "pipx install theHarvester", "note": "",
        "example": "example.com", "triggers": ["domain", "company", "emails", "organisation", "organization"],
    },
    {
        "key": "subfinder", "title": "subfinder — sub-domains",
        "what": "Quickly finds a domain's sub-domains from passive public sources.",
        "who": "Map all the sub-domains of a website.",
        "install": "Download the .exe from github.com/projectdiscovery/subfinder/releases and put it on your PATH.",
        "note": "It's a single program file — no Python needed.",
        "example": "example.com", "triggers": ["subdomain", "sub-domain", "domain"],
    },
    {
        "key": "dnstwist", "title": "dnstwist — look-alike domains",
        "what": "Finds typo / look-alike domains that may impersonate a brand (phishing).",
        "who": "Detect impersonation and phishing domains targeting a brand.",
        "install": "pipx install dnstwist", "note": "",
        "example": "example.com", "triggers": ["phishing", "impersonation", "typosquat", "lookalike", "fake domain"],
    },
    {
        "key": "phoneinfoga", "title": "PhoneInfoga — phone number recon",
        "what": "Gathers public info about an international phone number (carrier, region, footprints).",
        "who": "Investigate a phone number.",
        "install": "Download the program from github.com/sundowndev/phoneinfoga/releases and put it on your PATH.",
        "note": "It's a single program file — no Python needed.",
        "example": "+15551234567", "triggers": ["phone", "number", "telephone", "mobile"],
    },
    {
        "key": "ghunt", "title": "GHunt — Google / Gmail",
        "what": "Shows what a Google/Gmail account publicly exposes (name, photo, reviews, maps).",
        "who": "Investigate a Gmail address.",
        "install": "pipx install ghunt",
        "note": "One-time login: run `ghunt login` once and follow the prompts.",
        "example": "name@gmail.com", "triggers": ["gmail", "google account", "google"],
    },
    {
        "key": "toutatis", "title": "Toutatis — Instagram",
        "what": "Pulls the contact details an Instagram account exposes (obfuscated email/phone).",
        "who": "Investigate an Instagram handle.",
        "install": "pipx install toutatis",
        "note": "Needs your own Instagram session id (the tool explains where to find it).",
        "example": "instagram_user", "triggers": ["instagram", "insta", "ig"],
    },
    {
        "key": "onionsearch", "title": "OnionSearch — dark-web search",
        "what": "Searches dark-web (.onion) indexes for a keyword and returns links.",
        "who": "Find dark-web mentions of a name, brand or term.",
        "install": "pipx install onionsearch", "note": "",
        "example": "acme leak", "triggers": ["dark web", "darkweb", "onion", "tor"],
    },
    {
        "key": "spiderfoot", "title": "SpiderFoot — all-in-one recon",
        "what": "A broad automated footprint scan across 200+ data sources at once.",
        "who": "One-click deep recon on a domain, email, name or IP.",
        "install": "pipx install spiderfoot",
        "note": "Provides the `sf` command Nexus uses.",
        "example": "example.com", "triggers": ["everything", "deep scan", "footprint", "recon"],
    },
    {
        "key": "ytdlp", "title": "yt-dlp — video / social metadata",
        "what": "Pulls public metadata (title, uploader, date, description) from a video or social URL.",
        "who": "Capture details about a video or social post.",
        "install": "pipx install yt-dlp", "note": "",
        "example": "https://www.youtube.com/watch?v=...", "triggers": ["video", "youtube", "tiktok", "url", "social media"],
    },
]


# --- Contextual pivots: match an identifier to the right passive recon tool ---
# Maps a detected identifier type to the tool keys that can investigate it (all
# PASSIVE recon — no exploit/offensive tools, consistent with the assistant's
# limits). Used to offer one-click "run the right lookup" from an entity dossier.
_PIVOT_TOOLS: dict[str, list[str]] = {
    "email":    ["holehe", "h8mail", "ghunt"],
    "phone":    ["phoneinfoga"],
    "domain":   ["theHarvester", "subfinder", "dnstwist"],
    "url":      ["ytdlp"],
    "username": ["sherlock", "maigret", "socialscan"],
}

_RE_URL = re.compile(r"^https?://", re.IGNORECASE)
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_DOMAIN = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_RE_PHONE = re.compile(r"^\+?[0-9][0-9\s().\-]{6,}$")
_RE_HANDLE = re.compile(r"^@?[A-Za-z0-9_.]{2,40}$")


def detect_identifier_type(value: str) -> str | None:
    """Classify a value as url / email / domain / phone / username (or None).

    Order matters: the most specific shapes win. A bare token (no @, no dot, no
    URL scheme) is treated as a username only by the caller's discretion."""
    v = (value or "").strip()
    if not v:
        return None
    if _RE_URL.match(v):
        return "url"
    if _RE_EMAIL.match(v):
        return "email"
    if _RE_DOMAIN.match(v):
        return "domain"
    if _RE_PHONE.match(v) and sum(c.isdigit() for c in v) >= 7:
        return "phone"
    if _RE_HANDLE.match(v):
        return "username"
    return None


def pivot_tools_for(value: str, kind: str | None = None) -> tuple[str | None, list[str]]:
    """Return ``(identifier_type, [tool_key, ...])`` for a value, or ``(None, [])``.

    High-confidence types (email/phone/domain/url) always offer pivots. The loose
    "username" shape only offers pivots when the entity is actually typed as an
    identifier, so a one-word person/org name (e.g. "Google") doesn't suggest a
    username lookup.
    """
    itype = detect_identifier_type(value)
    if itype is None:
        return None, []
    if itype == "username" and (kind or "") != "identifier":
        return None, []
    return itype, _PIVOT_TOOLS.get(itype, [])


def recommend_tools(question: str, limit: int = 2) -> list[dict]:
    """Tools whose trigger words appear in a free-text question (best first).

    Used by Sherlock to suggest the right tool for "how can I look up …" asks.
    """
    q = (question or "").lower()
    scored: list[tuple[int, dict]] = []
    for tool in TOOL_CATALOG:
        hits = sum(1 for t in tool["triggers"] if t in q)
        if hits:
            scored.append((hits, tool))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [t for _, t in scored[:limit]]


def tools_brief() -> str:
    """A compact one-line-per-tool reference for the assistant's system prompt."""
    return "\n".join(
        f"- {t['title']}: {t['what']} (install: {t['install']})"
        for t in TOOL_CATALOG
    )
