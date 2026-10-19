"""Plain-English labels for the "What's connected" summary.

``Settings.availability_report()`` returns short internal ids (``gnews``,
``holehe`` ...). The header shows a single summary control; when opened it lists
every source with a human label and one line on what it needs to switch on.
Unknown ids fall back to the id itself, so a new source never breaks the header.
"""

from __future__ import annotations

# id -> (label, what it needs / what it does)
SOURCE_INFO: dict[str, tuple[str, str]] = {
    "ai": ("AI analysis", "Optional. Add an AI key or a local model on Setup."),
    "gemini": ("AI analysis (Gemini)", "Summaries, translation and threat scores."),
    "claude": ("AI analysis (Claude)", "Summaries, translation and threat scores."),
    "openai": ("AI analysis (OpenAI)", "Summaries, translation and threat scores."),
    "grok": ("AI analysis (Grok)", "Summaries, translation and threat scores."),
    "local": ("AI analysis (local model)", "Summaries, translation and threat scores."),
    "rss": ("RSS feeds", "No key. Add feed URLs on Sources."),
    "gnews": ("Google News", "No key. On when a case or topic has search words."),
    "gdelt": ("GDELT world news", "No key. On when a case or topic has search words."),
    "reddit": ("Reddit", "No key. On when a case or topic has search words."),
    "serpapi": ("Google Search (SerpAPI)", "Needs a SerpAPI key."),
    "google_cse": ("Google Custom Search", "Needs a Google CSE key and engine id."),
    "telegram": ("Telegram", "Needs Telegram API credentials."),
    "twitter": ("X / Twitter", "Needs an X API bearer token."),
    "sherlock": ("Sherlock (usernames)", "Optional tool. Install it to enable."),
    "maigret": ("Maigret (usernames)", "Optional tool. Install it to enable."),
    "socialscan": ("Socialscan (accounts)", "Optional tool. Install it to enable."),
    "holehe": ("Holehe (email accounts)", "Optional tool. Install it to enable."),
    "h8mail": ("h8mail (breaches)", "Optional tool. Install it to enable."),
    "ghunt": ("GHunt (Google accounts)", "Optional tool. Install it to enable."),
    "phoneinfoga": ("PhoneInfoga (phone numbers)", "Optional tool. Install it to enable."),
    "theHarvester": ("theHarvester (domains)", "Optional tool. Install it to enable."),
    "subfinder": ("Subfinder (subdomains)", "Optional tool. Install it to enable."),
    "dnstwist": ("dnstwist (look-alike domains)", "Optional tool. Install it to enable."),
    "spiderfoot": ("SpiderFoot", "Optional tool. Install it to enable."),
    "yt-dlp": ("yt-dlp (video metadata)", "Optional tool. Install it to enable."),
    "toutatis": ("Toutatis (Instagram)", "Optional tool plus an Instagram session id."),
    "onionsearch": ("OnionSearch (Tor)", "Optional tool. Needs Tor running."),
}


def source_label(name: str) -> str:
    return SOURCE_INFO.get(name, (name, ""))[0]


def source_needs(name: str) -> str:
    return SOURCE_INFO.get(name, (name, ""))[1]


def sorted_status(status: dict[str, str]) -> list[tuple[str, str]]:
    """Active sources first, then the rest, each group in the report's order."""
    items = list(status.items())
    return [kv for kv in items if kv[1] == "on"] + [kv for kv in items if kv[1] != "on"]
