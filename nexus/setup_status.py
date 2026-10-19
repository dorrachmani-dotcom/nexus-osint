"""Plain-English setup status: the Setup checklist and the daily-brief card.

Pure helpers (no web imports) so the status logic is unit-testable. Every
function degrades gracefully: missing meta or unparseable timestamps read as
"not set up yet", never as an error.

Times: ``digest_time`` is local wall-clock time ("HH:MM"); the ``last_*_at``
meta values are stored as ISO UTC. Everything shown to the user is local time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nexus.digest import DEFAULT_DIGEST_TIME, digest_due, parse_digest_time

# ---------------------------------------------------------------- timing


def _local_naive(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def parse_stored_time(value: str | None) -> datetime | None:
    """ISO string from meta -> local naive datetime, or None when unusable."""
    if not value:
        return None
    try:
        return _local_naive(datetime.fromisoformat(str(value).strip()))
    except ValueError:
        return None


def next_daily_run(now: datetime, daily_time: str | None, last_run: str | None) -> datetime:
    """When the once-a-day job will next run, in local time.

    Mirrors :func:`nexus.digest.digest_due`: if it is due right now the next run
    is "now" (the scheduler ticks every minute); if today's slot is still ahead
    it is today; otherwise tomorrow.
    """
    now_l = _local_naive(now).replace(second=0, microsecond=0)
    hour, minute = parse_digest_time(daily_time or DEFAULT_DIGEST_TIME)
    today_slot = now_l.replace(hour=hour, minute=minute)
    if digest_due(now, daily_time, last_run):
        return now_l
    if now_l < today_slot:
        return today_slot
    return today_slot + timedelta(days=1)


def human_when(when: datetime | None, now: datetime) -> str:
    """'today at 08:00' / 'tomorrow at 08:00' / 'yesterday at 08:00' / 'Oct 17 at 08:00'."""
    if when is None:
        return "never"
    now_l = _local_naive(now)
    day = (when.date() - now_l.date()).days
    hm = when.strftime("%H:%M")
    if day == 0:
        return f"today at {hm}"
    if day == 1:
        return f"tomorrow at {hm}"
    if day == -1:
        return f"yesterday at {hm}"
    return f"{when.strftime('%b')} {when.day} at {hm}"


# --------------------------------------------------------- daily brief


def digest_status(
    *,
    now: datetime,
    enabled: bool,
    verified: bool,
    daily_time: str | None,
    last_sent: str | None,
    last_status: str | None,
) -> dict:
    """Everything the daily-brief card says about the schedule.

    ``enabled`` is the stored on/off switch; the brief only actually goes out
    while the email connection is verified, so ``active`` needs both.
    """
    hour, minute = parse_digest_time(daily_time or DEFAULT_DIGEST_TIME)
    hhmm = f"{hour:02d}:{minute:02d}"
    active = bool(enabled and verified)
    last_dt = parse_stored_time(last_sent)
    status = (last_status or "").strip()
    failed = status.startswith("error")
    out: dict[str, object] = {
        "time": hhmm,
        "active": active,
        "verified": bool(verified),
        "last_sent": human_when(last_dt, now) if last_dt else "never",
        "next_run": "",
        "error": status[len("error:"):].strip() if failed else "",
        "blocked_reason": "",
    }
    if active:
        nxt = next_daily_run(now, hhmm, last_sent)
        out["next_run"] = (
            "within a minute" if nxt <= _local_naive(now) else human_when(nxt, now)
        )
    elif not verified:
        out["blocked_reason"] = "Connect and test your email first (step 3)."
    return out


# --------------------------------------------------------- setup checklist

DONE, TODO, OPTIONAL = "done", "todo", "optional"

_PROVIDER_NAMES = {
    "gemini": "Google Gemini",
    "openai": "OpenAI (ChatGPT)",
    "anthropic": "Anthropic (Claude)",
    "grok": "xAI (Grok)",
    "ollama": "a local model (Ollama)",
    "local_openai": "a local AI server",
}


def setup_steps(
    *,
    status: dict[str, str],
    active_provider: str,
    email_ready: bool,
    email_verified: bool,
    email_label: str,
    digest: dict,
    tracked_cases: int,
    report_cases: int,
) -> list[dict]:
    """The five setup steps, each with a state, one plain line and a button."""
    keyless = [k for k in ("gnews", "gdelt", "reddit", "rss") if status.get(k) == "on"]
    sources_on = sum(1 for v in status.values() if v == "on")
    steps: list[dict] = []

    # 1. Sources
    if keyless:
        line = (f"{sources_on} of {len(status)} sources are ready. Google News, GDELT and Reddit "
                "need no keys; optional keys add more.")
        steps.append({"key": "sources", "title": "Sources", "state": DONE, "line": line,
                      "button": "Review sources", "href": "/sources"})
    else:
        steps.append({
            "key": "sources", "title": "Sources", "state": TODO,
            "line": "Nothing is being collected yet. Open a case (or add a topic) with a few "
                    "search words; Google News, GDELT and Reddit then work with no keys.",
            "button": "Open a case", "href": "/cases",
        })

    # 2. AI provider (optional)
    if active_provider and active_provider != "off":
        name = _PROVIDER_NAMES.get(active_provider, active_provider)
        steps.append({"key": "ai", "title": "AI analysis", "state": DONE,
                      "line": f"Using {name} for summaries, translation, threat scores and Sherlock.",
                      "button": "Change", "href": "/settings#settings-left"})
    else:
        steps.append({"key": "ai", "title": "AI analysis", "state": OPTIONAL,
                      "line": "Optional. Adds summaries, translation, threat scores and the Sherlock "
                              "assistant. Gemini has a free tier; a local model keeps everything offline.",
                      "button": "Set up AI", "href": "/settings#settings-left"})

    # 3. Email connection
    if email_verified:
        steps.append({"key": "email", "title": "Email connection", "state": DONE,
                      "line": f"Connected and tested ({email_label}).",
                      "button": "Change", "href": "/settings#email-settings"})
    elif email_ready:
        steps.append({"key": "email", "title": "Email connection", "state": TODO,
                      "line": "Saved but not tested yet. Send a test email to unlock the daily brief.",
                      "button": "Send a test email", "href": "/settings#email-settings"})
    else:
        steps.append({"key": "email", "title": "Email connection", "state": OPTIONAL,
                      "line": "Optional. Needed only if you want the daily brief by email. "
                              "Gmail connects in a few clicks.",
                      "button": "Connect email", "href": "/settings#email-settings"})

    # 4. Daily brief
    if digest.get("active"):
        steps.append({"key": "brief", "title": "Daily brief by email", "state": DONE,
                      "line": f"On: every day at {digest['time']}. Next: {digest['next_run']}.",
                      "button": "Change", "href": "#daily-brief"})
    else:
        line = ("Off. Get one short email a day with what's new in your cases."
                if email_verified else
                "Get one short email a day with what's new in your cases (needs step 3 first).")
        steps.append({"key": "brief", "title": "Daily brief by email", "state": OPTIONAL,
                      "line": line, "button": "Schedule it", "href": "#daily-brief"})

    # 5. Per-case daily reports
    if report_cases:
        steps.append({"key": "reports", "title": "Daily case reports", "state": DONE,
                      "line": f"{report_cases} case{'s' if report_cases != 1 else ''} save a report "
                              f"every day at {digest.get('time', DEFAULT_DIGEST_TIME)}.",
                      "button": "Manage", "href": "#case-reports-list"})
    else:
        line = ("Optional. Save a PDF report of a case every day (and link or attach it in the email)."
                if tracked_cases else
                "Optional. Once you have a case, it can save a PDF report every day.")
        steps.append({"key": "reports", "title": "Daily case reports", "state": OPTIONAL,
                      "line": line, "button": "Choose cases", "href": "#case-reports-list"})
    return steps
