"""Defense-in-depth: keep secrets out of the logs.

Even with noisy HTTP loggers quieted, a future code path could accidentally log
a URL or payload that embeds an API key. This module installs a logging filter
on the root logger that scrubs any *currently configured* secret value from
every log record before it is emitted — so a leak becomes a redacted ``***``
instead of a plaintext key sitting in ``data/server.log``.

It reads secret values live from Settings (which loads them from .env), and only
redacts values long enough to be unambiguous (so a short token can't blank out
unrelated text). Like everything else here it never raises: any error while
redacting falls back to passing the record through unchanged.
"""

from __future__ import annotations

import logging

# Don't redact very short values — they could collide with ordinary words and
# turn benign logs into noise. Real API keys/tokens are comfortably longer.
_MIN_REDACT_LEN = 8
_PLACEHOLDER = "***REDACTED***"


def _secret_values() -> list[str]:
    """Live, non-empty secret values to scrub, longest first (so prefixes of a
    longer secret are handled by the longer match first)."""
    try:
        from nexus.envstore import EDITABLE_KEYS, _CUSTOM_KEY_RE, _env_path
        from nexus.config import get_settings

        settings = get_settings()
        values = []
        for key in EDITABLE_KEYS:
            val = getattr(settings, key.lower(), None)
            if isinstance(val, str) and len(val) >= _MIN_REDACT_LEN:
                values.append(val)

        # Per-custom-source API keys (CUSTOM_SOURCE_<id>_KEY) are not Settings
        # fields, so read them straight from .env. Without this a query-auth key
        # embedded in a failed-request URL could reach server.log un-redacted.
        try:
            for line in _env_path().read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, _, raw = stripped.partition("=")
                if not _CUSTOM_KEY_RE.match(name.strip()):
                    continue
                val = raw.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if len(val) >= _MIN_REDACT_LEN:
                    values.append(val)
        except OSError:
            pass

        return sorted(values, key=len, reverse=True)
    except Exception:
        return []


class SecretRedactingFilter(logging.Filter):
    """Replaces any configured secret value in a log record with a placeholder."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            secrets = _secret_values()
            if not secrets:
                return True

            def scrub(text: str) -> str:
                for secret in secrets:
                    if secret in text:
                        text = text.replace(secret, _PLACEHOLDER)
                return text

            # 1) The message itself (folding args in so they're scrubbed too).
            message = record.getMessage()
            redacted = scrub(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()

            # 2) The traceback. logger.exception()/exc_info renders the traceback
            # from record.exc_info at *format* time — after this filter runs — so
            # a secret in an exception string (e.g. an httpx error carrying a URL
            # with ?key=...) would otherwise reach the log un-redacted. We pre-
            # render and scrub it into record.exc_text; the stdlib Formatter then
            # reuses our cached (clean) text instead of re-deriving it.
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_text = scrub(record.exc_text)
        except Exception:
            # Never let logging-safety break logging itself.
            pass
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to every root handler (idempotent).

    A filter on a logger only sees records logged *directly* to it; records from
    child loggers (httpx, nexus.*, uvicorn …) reach the root logger's *handlers*,
    not its filter. So we attach the filter to the handlers, where every emitted
    record passes through it.
    """
    root = logging.getLogger()
    handlers = root.handlers or []
    for handler in handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
