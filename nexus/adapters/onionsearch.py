"""OnionSearch adapter — discover .onion URLs for a keyword.

OnionSearch ("onionsearch <query> --output <file>") scrapes several dark-web
search engines and writes a CSV of results (engine, page name, link). We read
that CSV and keep the .onion links as findings, tagged with the engine that
surfaced them. Some engines require Tor; those simply yield nothing here.
"""

from __future__ import annotations

import csv
import os

from nexus.adapters.base import Finding, ToolAdapter


class OnionSearchAdapter(ToolAdapter):
    name = "onionsearch"
    binary = "onionsearch"

    def build_command(self, target: str, workdir: str) -> list[str]:
        out = os.path.join(workdir, "onion.csv")
        return [self.binary, target, "--output", out]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        path = os.path.join(workdir, "onion.csv")
        if not os.path.exists(path):
            return []

        findings: list[Finding] = []
        seen: set[str] = set()
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as fh:
                for row in csv.reader(fh):
                    engine = row[0].strip() if row else target
                    # The link is the first .onion URL in the row.
                    url = next(
                        (
                            c.strip()
                            for c in row
                            if c.strip().startswith("http") and ".onion" in c
                        ),
                        None,
                    )
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    findings.append(Finding(kind="onion", value=url, label=engine))
        except OSError:
            return findings
        return findings
