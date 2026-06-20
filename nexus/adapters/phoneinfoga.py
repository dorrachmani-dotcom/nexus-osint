"""PhoneInfoga adapter — reconnaissance on an international phone number.

PhoneInfoga ("phoneinfoga scan -n <number>") prints a human-readable report:
country, carrier, line type, and local/international formats. We pull the
labelled fields out of stdout into typed findings. The number should be given in
international form, e.g. +14155552671.
"""

from __future__ import annotations

import re

from nexus.adapters.base import Finding, ToolAdapter

# Lines look like "Country: United States" / "Carrier: AT&T" etc.
_FIELD_RE = re.compile(r"^\s*([A-Za-z ]+?)\s*[:=]\s*(.+?)\s*$")

# Labels worth surfacing -> the finding kind we store them under.
_FIELDS = {
    "country": "phone_country",
    "carrier": "phone_carrier",
    "line type": "phone_line_type",
    "local": "phone_local",
    "international": "phone_intl",
    "e164": "phone_e164",
}


class PhoneInfogaAdapter(ToolAdapter):
    name = "phoneinfoga"
    binary = "phoneinfoga"

    def build_command(self, target: str, workdir: str) -> list[str]:
        return [self.binary, "scan", "-n", target]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for line in stdout.splitlines():
            m = _FIELD_RE.match(line)
            if not m:
                # Surface any discovered footprint URLs (Google-dork results).
                stripped = line.strip()
                if stripped.startswith("http"):
                    url = stripped.split()[0]
                    if ("url", url) not in seen:
                        seen.add(("url", url))
                        findings.append(Finding(kind="url", value=url, label=target))
                continue
            label = m.group(1).strip().lower()
            value = m.group(2).strip()
            kind = _FIELDS.get(label)
            if not kind or not value or value.lower() in {"n/a", "unknown", ""}:
                continue
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(kind=kind, value=value, label=target))
        return findings
