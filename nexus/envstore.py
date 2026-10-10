"""Secure, in-place editor for the .env secrets file.

The dashboard lets the analyst paste API keys without touching a text editor.
For OpSec those secrets must behave exactly as before: they live ONLY in the
local, git-ignored ``.env`` file — never in the database and never rendered back
to the screen. This module is the single, audited writer for that file.

Design choices that keep it safe:
  * A strict allow-list (``EDITABLE_KEYS``): the web layer can only ever write
    these known variables, so a crafted form can never inject arbitrary lines.
  * Surgical edits: existing comments, ordering, and unrelated variables are
    preserved; only the targeted ``KEY=`` lines are rewritten (or appended).
  * Values are quoted when they contain whitespace/specials so they round-trip
    through dotenv parsing intact.

Nothing here ever raises on a malformed file — a missing/odd ``.env`` is simply
treated as empty, matching the project-wide graceful-degradation rule.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from nexus.config import get_settings

# Allow-list of secret variables the dashboard may write. Anything not in this
# set is rejected by update_env(), so the editor can never touch arbitrary keys.
EDITABLE_KEYS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "LOCAL_LLM_API_KEY",
        "SERPAPI_KEY",
        "GOOGLE_CSE_KEY",
        "GOOGLE_CSE_CX",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "TWITTER_BEARER_TOKEN",
        "TELEMETRY_API_KEY",
        "INSTAGRAM_SESSIONID",
        # Not a secret, but written here so the keyless-translation endpoint can be
        # configured from the dashboard (no .env editing). It is a plain URL and,
        # unlike the keys above, is shown back to the operator in the UI.
        "LIBRETRANSLATE_URL",
        # Daily email brief. SMTP_PASSWORD / RESEND_API_KEY / SENDGRID_API_KEY
        # are secrets (never shown back). The rest are plain connection details
        # (server, port, addresses) that the email wizard shows back so the
        # operator can see and correct them.
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "DIGEST_TO",
        "RESEND_API_KEY",
        "SENDGRID_API_KEY",
        # "Connect Gmail" (OAuth). The client secret and refresh token are
        # secrets; the client ID and connected address are shown back.
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_EMAIL",
        # The local OpenAI-compatible server address (not a secret; shown back).
        "LOCAL_LLM_BASE_URL",
        # Optional Internet Archive keys (Save Page Now 2). Secrets.
        "ARCHIVE_ORG_ACCESS_KEY",
        "ARCHIVE_ORG_SECRET_KEY",
    }
)


# Per-custom-source secret keys. The NAME is always derived server-side from an
# integer source id (never user input), so the pattern below can never be used to
# write an arbitrary variable. These keys are honoured by update_env in addition
# to EDITABLE_KEYS, keeping every custom source's API key in .env (not the DB).
_CUSTOM_KEY_RE = re.compile(r"^CUSTOM_SOURCE_\d+_KEY$")


def custom_source_env_name(source_id: int) -> str:
    """Canonical .env variable name holding a custom source's API key."""
    return f"CUSTOM_SOURCE_{int(source_id)}_KEY"


def _is_writable_key(key: str) -> bool:
    return key in EDITABLE_KEYS or bool(_CUSTOM_KEY_RE.match(key))


def _env_path() -> Path:
    """Location of the .env file.

    Normally the repo root (next to .env.example). A packaged/frozen build sets
    NEXUS_ENV_FILE to a user-writable path (e.g. %LOCALAPPDATA%\\Nexus\\.env)
    because the bundle directory is read-only; both this writer and the settings
    loader (nexus.config) read the same override so they never diverge.
    """
    override = os.environ.get("NEXUS_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


def _format_value(value: str) -> str:
    """Render a value for a dotenv line, quoting only when necessary."""
    if value == "":
        return ""
    if any(ch in value for ch in ' \t#\'"'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def update_env(updates: dict[str, str]) -> None:
    """Apply ``{KEY: value}`` edits to the .env file in place.

    Only keys in ``EDITABLE_KEYS`` are honoured. An empty-string value clears the
    variable (left as ``KEY=``). Comments, blank lines, ordering, and unrelated
    variables are preserved; allowed keys not already present are appended.
    Newlines in values are stripped so a value can never break the file format.
    """
    clean = {
        key: (val or "").replace("\r", "").replace("\n", "").strip()
        for key, val in updates.items()
        if _is_writable_key(key)
    }
    if not clean:
        return

    path = _env_path()
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []

    # Clearing a per-custom-source key removes its line entirely (deleted sources
    # must not leave dead CUSTOM_SOURCE_<id>_KEY= cruft). Standing allow-listed
    # keys are left as ``KEY=`` to preserve the .env layout.
    def _drop(key: str) -> bool:
        return clean.get(key, "") == "" and bool(_CUSTOM_KEY_RE.match(key))

    out: list[str] = []
    seen: set[str] = set()
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in clean:
                seen.add(key)
                if _drop(key):
                    continue
                out.append(f"{key}={_format_value(clean[key])}")
                continue
        out.append(line)

    for key, val in clean.items():
        if key not in seen and not _drop(key):
            out.append(f"{key}={_format_value(val)}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def reload_settings() -> None:
    """Drop the cached Settings so the next read reflects the new .env."""
    get_settings.cache_clear()


def is_configured(key: str) -> bool:
    """Whether a secret currently has a non-empty value (via live settings).

    Reads through the Settings object rather than the file so the answer matches
    what the running app actually uses. Never reveals the value itself.
    """
    return bool(getattr(get_settings(), key.lower(), None))


def read_env_value(key: str) -> str | None:
    """Read a raw value straight from the .env file by exact key name.

    Needed for variables that are NOT declared fields on Settings (e.g. each
    custom source's CUSTOM_SOURCE_<id>_KEY). Parses the file directly, applying
    the same light unquoting dotenv uses. Returns None if absent/empty. Never
    raises — a missing/odd file is treated as "no value".
    """
    try:
        lines = _env_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return value or None
    return None


def custom_source_key_configured(source_id: int) -> bool:
    """Whether a custom source's API key is present in .env (value hidden)."""
    return bool(read_env_value(custom_source_env_name(source_id)))
