"""Toutatis adapter — pull contact details from an Instagram account.

Toutatis ("toutatis -u <username> -s <sessionid>") returns the data Instagram
exposes for an account, including obfuscated email/phone and the numeric user
id. It needs a valid Instagram session cookie; per OpSec that secret lives only
in .env (INSTAGRAM_SESSIONID) — never in the DB. Without it the adapter reports
itself unavailable, so it is simply skipped.
"""

from __future__ import annotations

import re

from nexus.adapters.base import Finding, ToolAdapter

_FIELD_RE = re.compile(r"^\s*(.+?)\s*:\s*(.+?)\s*$")

# Labels Toutatis prints -> the finding kind we keep.
_FIELDS = {
    "full name": "name",
    "user id": "user_id",
    "public email": "email",
    "email": "email",
    "obfuscated email": "email_hint",
    "public phone number": "phone",
    "phone": "phone",
    "obfuscated phone": "phone_hint",
}


class ToutatisAdapter(ToolAdapter):
    name = "toutatis"
    binary = "toutatis"

    def is_available(self) -> bool:
        # Needs both the binary and an Instagram session cookie from .env.
        return super().is_available() and bool(self.settings.instagram_sessionid)

    def build_command(self, target: str, workdir: str) -> list[str]:
        return [self.binary, "-u", target, "-s", self.settings.instagram_sessionid or ""]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for line in stdout.splitlines():
            m = _FIELD_RE.match(line)
            if not m:
                continue
            label = m.group(1).strip().lower()
            value = m.group(2).strip()
            kind = _FIELDS.get(label)
            if not kind or not value or value.lower() in {"none", "n/a"}:
                continue
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(kind=kind, value=value, label=target))
        return findings
