"""SpiderFoot adapter — broad footprint scan via the `sf` CLI (CSV output).

SpiderFoot's CLI emits results to stdout. We request CSV (`-o csv`) and turn
each row into a finding keyed by SpiderFoot's event type.
"""

from __future__ import annotations

import csv
import io

from nexus.adapters.base import Finding, ToolAdapter

# Map a few common SpiderFoot event types onto our graph entity kinds.
_TYPE_MAP = {
    "EMAILADDR": "email",
    "DOMAIN_NAME": "domain",
    "INTERNET_NAME": "host",
    "IP_ADDRESS": "ip",
    "PHONE_NUMBER": "phone",
    "USERNAME": "account",
}


class SpiderFootAdapter(ToolAdapter):
    name = "spiderfoot"
    binary = "sf"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # -s target, -o csv to stdout, -q quiet banner.
        return [self.binary, "-s", target, "-o", "csv", "-q"]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        if not stdout.strip():
            return findings

        reader = csv.reader(io.StringIO(stdout))
        rows = list(reader)
        if not rows:
            return findings

        # First row is typically a header containing "Type" and "Data".
        header = [h.strip().lower() for h in rows[0]]
        try:
            type_idx = header.index("type")
            data_idx = header.index("data")
            body = rows[1:]
        except ValueError:
            # No recognizable header: assume last column is the data value.
            type_idx, data_idx, body = 0, len(rows[0]) - 1, rows

        seen: set[tuple[str, str]] = set()
        for row in body:
            if len(row) <= max(type_idx, data_idx):
                continue
            sf_type = row[type_idx].strip()
            value = row[data_idx].strip()
            if not value:
                continue
            kind = _TYPE_MAP.get(sf_type.upper(), "entity")
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(kind=kind, value=value, label=sf_type))
        return findings
