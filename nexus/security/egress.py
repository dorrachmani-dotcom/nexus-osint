"""Outbound-traffic monitor — "is this tool talking to anywhere it shouldn't?".

The platform is designed to stay local and to reach out only to (a) the AI
provider the operator chose, (b) the open sources the operator configured, and
(c) the operator's own machine (e.g. a local Ollama model). This module gives an
honest, auditable check that nothing else happens.

How it works
------------
We install a thin, fail-open wrapper around the standard library's socket
``connect`` so every outbound TCP connection the application's own Python code
opens is recorded: its destination host/IP, port and the time. A parallel
wrapper around ``getaddrinfo`` remembers which hostname each resolved IP came
from, so the log reads as human hostnames, not bare numbers. Each destination is
then classified against an allow-list built from the live configuration:

  * LOCAL      — loopback / private addresses (your machine, a local Ollama).
  * AI PROVIDER — the Anthropic / Google / OpenAI endpoints.
  * SOURCE     — a host that belongs to a source you enabled (RSS feeds you
                 added, SERPAPI, Reddit, X/Twitter, Telegram, custom APIs).
  * DNS        — name-resolution traffic.
  * UNEXPECTED — anything that does not match the above. These are surfaced
                 prominently so you can investigate.

Scope (stated honestly): this observes connections made *inside this Python
process* — which covers the feed collectors, the AI calls and the bundle
fetches. The headless browser used for evidence screenshots and the external
OSINT command-line tools run as **separate processes**, so their traffic is not
seen here; that is a deliberate limitation, not a guarantee of silence.

Nothing here ever blocks a connection (so it can never break a legitimate fetch)
and nothing here sends data anywhere — it only observes and classifies in
memory.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

logger = logging.getLogger("nexus.security.egress")

# Aggregated record per (host, port). Bounded so a long-running process can never
# grow this without limit.
_MAX_DESTINATIONS = 500
_lock = threading.Lock()
_destinations: "OrderedDict[tuple[str, int], dict]" = OrderedDict()

# Recent IP -> hostname map, filled by the getaddrinfo wrapper so connections by
# IP can be shown as the name the code actually asked for.
_MAX_DNS_CACHE = 512
_ip_to_host: "OrderedDict[str, str]" = OrderedDict()

_installed = False

# Static destinations we always consider expected, with a friendly category.
# Matched as a domain suffix (so "api.anthropic.com" matches "anthropic.com").
_STATIC_ALLOW: tuple[tuple[str, str], ...] = (
    ("anthropic.com", "AI provider (Anthropic)"),
    ("googleapis.com", "AI provider (Google)"),
    ("google.com", "Source / Google"),
    ("gstatic.com", "Support (Google static)"),
    ("openai.com", "AI provider (OpenAI)"),
    ("x.ai", "AI provider (xAI Grok)"),
    ("serpapi.com", "Source (SERPAPI news)"),
    ("reddit.com", "Source (Reddit)"),
    ("redditmedia.com", "Source (Reddit)"),
    ("redd.it", "Source (Reddit)"),
    ("twitter.com", "Source (X / Twitter)"),
    ("x.com", "Source (X / Twitter)"),
    ("twimg.com", "Source (X / Twitter)"),
    ("telemetr.io", "Source (Telegram / Telemetry)"),
    ("telemetryapp.io", "Source (Telegram / Telemetry)"),
    # Wayback Machine captures/lookups (only when the analyst archives a source).
    ("archive.org", "Archive (Internet Archive)"),
)

# How long a built allow-list of configured hosts is reused before refreshing.
_ALLOW_TTL_SEC = 60.0
_allow_cache: dict | None = None
_allow_cache_at = 0.0


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url if "://" in url else "//" + url, scheme="http").hostname
        return host.lower() if host else None
    except Exception:
        return None


def _configured_allow() -> dict[str, str]:
    """Build {host_suffix: category} from the live configuration.

    Best-effort and fully defensive: any failure yields an empty extra map so the
    monitor still works with just the static list.
    """
    global _allow_cache, _allow_cache_at
    now = time.monotonic()
    if _allow_cache is not None and (now - _allow_cache_at) < _ALLOW_TTL_SEC:
        return _allow_cache

    allow: dict[str, str] = {}
    try:
        from nexus.config import get_settings

        settings = get_settings()

        # RSS feeds the operator added (``rss_feed_list`` is a property).
        try:
            feeds = settings.rss_feed_list  # type: ignore[attr-defined]
            for url in (feeds if isinstance(feeds, (list, tuple)) else []):
                h = _host_of(url)
                if h:
                    allow[h] = "Source (your RSS feed)"
        except Exception:
            for url in str(getattr(settings, "rss_feeds", "") or "").split(","):
                h = _host_of(url.strip())
                if h:
                    allow[h] = "Source (your RSS feed)"

        # A remote Ollama server, if one is configured (local ones are caught by
        # the loopback/private check anyway).
        h = _host_of(str(getattr(settings, "ollama_base_url", "") or ""))
        if h:
            allow[h] = "AI provider (Ollama)"

        h = _host_of(str(getattr(settings, "telemetry_base_url", "") or ""))
        if h:
            allow[h] = "Source (Telegram / Telemetry)"

        # A local OpenAI-compatible server (LM Studio etc.). Usually loopback
        # (already "Local machine"), but it may sit on another host the
        # operator configured.
        h = _host_of(str(getattr(settings, "local_llm_base_url", "") or ""))
        if h:
            allow[h] = "AI provider (local OpenAI-compatible server)"

        # The email brief: only the destination the operator configured.
        try:
            from nexus.mailer import resolve_email_config

            cfg = resolve_email_config(settings)
            if cfg.provider in ("gmail", "outlook", "smtp") and cfg.host:
                h = _host_of(cfg.host)
                if h:
                    allow[h] = "Email (your SMTP server)"
            elif cfg.provider in ("resend", "sendgrid") and cfg.api_key:
                h = _host_of(cfg.api_url)
                if h:
                    allow[h] = f"Email (your {cfg.label} API)"
        except Exception:
            logger.debug("Could not read email settings for egress allow-list", exc_info=True)
    except Exception:
        logger.debug("Could not read settings for egress allow-list", exc_info=True)

    # Custom API sources the operator defined.
    try:
        from nexus.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT base_url FROM custom_sources WHERE enabled = 1"
            ).fetchall()
        for row in rows:
            h = _host_of(str(row[0] or ""))
            if h:
                allow[h] = "Source (your custom API)"
    except Exception:
        logger.debug("Could not read custom sources for egress allow-list", exc_info=True)

    _allow_cache = allow
    _allow_cache_at = now
    return allow


def _is_local_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    candidates = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    return any(
        c.is_private or c.is_loopback or c.is_link_local or c.is_unspecified
        for c in candidates
    )


def classify(host: str, port: int) -> tuple[str, bool]:
    """Return ``(category, expected)`` for a destination.

    ``host`` may be a hostname or a bare IP string.
    """
    h = (host or "").strip().lower().strip(".")
    if not h:
        return "Unknown", False

    # Local machine / private network (covers a local Ollama, the app itself).
    if h in ("localhost", "127.0.0.1", "::1") or _is_local_ip(h):
        return "Local machine", True

    if port == 53:
        return "DNS lookup", True

    # Static then configured allow-lists, matched as domain suffixes.
    for suffix, category in _STATIC_ALLOW:
        if h == suffix or h.endswith("." + suffix):
            return category, True

    for suffix, category in _configured_allow().items():
        if h == suffix or h.endswith("." + suffix):
            return category, True

    return "Unexpected", False


def record_connection(address, family: int = socket.AF_INET) -> None:
    """Record one outbound connection attempt. Never raises."""
    try:
        if family not in (socket.AF_INET, getattr(socket, "AF_INET6", -1)):
            return  # ignore AF_UNIX and friends
        if not isinstance(address, tuple) or len(address) < 2:
            return
        ip = str(address[0])
        port = int(address[1])
        host = _ip_to_host.get(ip, ip)
        category, expected = classify(host, port)
        key = (host, port)
        now = time.time()
        with _lock:
            rec = _destinations.get(key)
            if rec is None:
                if len(_destinations) >= _MAX_DESTINATIONS:
                    _destinations.popitem(last=False)  # drop oldest
                rec = {
                    "host": host,
                    "ip": ip,
                    "port": port,
                    "category": category,
                    "expected": expected,
                    "count": 0,
                    "first_seen": now,
                    "last_seen": now,
                }
                _destinations[key] = rec
            rec["count"] += 1
            rec["last_seen"] = now
            rec["category"] = category  # re-classify in case config changed
            rec["expected"] = expected
        if not expected:
            logger.warning(
                "Egress to UNEXPECTED destination %s:%s (not a configured "
                "provider/source or local host)", host, port,
            )
    except Exception:
        # Monitoring must never interfere with real traffic.
        logger.debug("record_connection failed", exc_info=True)


def _remember_dns(host: str, infos) -> None:
    try:
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0]:
                ip = str(sockaddr[0])
                if ip in _ip_to_host:
                    _ip_to_host.move_to_end(ip)
                _ip_to_host[ip] = host.lower()
                while len(_ip_to_host) > _MAX_DNS_CACHE:
                    _ip_to_host.popitem(last=False)
    except Exception:
        pass


def install_egress_monitor() -> None:
    """Install the socket wrappers once. Idempotent and fail-open."""
    global _installed
    if _installed:
        return

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _patched_getaddrinfo(host, *args, **kwargs):
        infos = real_getaddrinfo(host, *args, **kwargs)
        if isinstance(host, str) and host:
            _remember_dns(host, infos)
        return infos

    def _patched_connect(self, address):
        record_connection(address, getattr(self, "family", socket.AF_INET))
        return real_connect(self, address)

    def _patched_connect_ex(self, address):
        record_connection(address, getattr(self, "family", socket.AF_INET))
        return real_connect_ex(self, address)

    try:
        socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = _patched_connect  # type: ignore[assignment]
        socket.socket.connect_ex = _patched_connect_ex  # type: ignore[assignment]
        _installed = True
        logger.info("Egress monitor installed (observing outbound connections).")
    except Exception:
        logger.warning("Could not install egress monitor; continuing without it.", exc_info=True)


def get_egress_report() -> dict:
    """A snapshot of observed outbound destinations for the dashboard."""
    with _lock:
        rows = [dict(r) for r in _destinations.values()]
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    unexpected = [r for r in rows if not r["expected"]]
    return {
        "installed": _installed,
        "destinations": rows,
        "total": len(rows),
        "unexpected": unexpected,
        "unexpected_count": len(unexpected),
        "ok": _installed and not unexpected,
    }


def _fmt_ts(ts) -> str:
    """Epoch seconds -> a readable UTC timestamp (or '-' if unavailable)."""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    except Exception:
        return "-"


def format_audit_report() -> str:
    """Render the egress snapshot as a plain-text data-handling audit report.

    A self-contained, human-readable record an analyst or agency can save or
    hand to a compliance reviewer: it states what the application binds to, lists
    every outbound destination observed this session with its classification, and
    is honest about the monitor's scope. English-only; carries nothing that ties
    it to any particular operator.
    """
    from datetime import datetime, timezone

    report = get_egress_report()
    rows = report["destinations"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    lines: list[str] = []
    lines.append("NEXUS-OSINT - DATA-HANDLING AUDIT REPORT")
    lines.append("=" * 48)
    lines.append(f"Generated: {generated}")
    lines.append("")
    lines.append(
        "This report lists every outbound network destination this application\n"
        "contacted during the current session, as observed by the built-in egress\n"
        "monitor. The application's web server binds only to the local machine\n"
        "(127.0.0.1) and performs no background telemetry of its own."
    )
    lines.append("")
    lines.append(f"Monitor status        : {'ACTIVE' if report['installed'] else 'NOT INSTALLED'}")
    lines.append(f"Destinations observed : {report['total']}")
    lines.append(f"Unexpected destinations: {report['unexpected_count']}")
    lines.append(f"Overall               : {'OK' if report['ok'] else 'REVIEW NEEDED'}")
    lines.append("")
    lines.append("DESTINATIONS")
    lines.append("-" * 48)
    if not rows:
        lines.append("(none observed yet this session)")
    else:
        for r in rows:
            tag = "EXPECTED  " if r["expected"] else "UNEXPECTED"
            host = r.get("host") or r.get("ip") or "?"
            lines.append(
                f"[{tag}] {host}:{r.get('port', '?')}"
                f"  - {r.get('category', '?')}"
                f"  x{r.get('count', 0)}"
                f"  last seen {_fmt_ts(r.get('last_seen'))}"
            )
    lines.append("")
    lines.append("NOTES")
    lines.append("-" * 48)
    lines.append(
        "* 'Expected' = the local machine, a source/feed you configured, an AI\n"
        "  provider you set up, or DNS name resolution. 'Unexpected' = anything\n"
        "  else; these are worth reviewing."
    )
    lines.append(
        "* Scope: this observes connections made inside the application process\n"
        "  (feed collectors, AI calls, bundle fetches). The evidence-screenshot\n"
        "  browser and external OSINT command-line tools run as separate\n"
        "  processes and are not covered here - a stated limitation, not a\n"
        "  guarantee of silence."
    )
    lines.append(
        "* The file safety check and this report run entirely on this computer.\n"
        "  The only optional outbound call is a VirusTotal hash lookup, which is\n"
        "  off unless you set VIRUSTOTAL_API_KEY (and sends a hash, never a file)."
    )
    lines.append("")
    return "\n".join(lines)
