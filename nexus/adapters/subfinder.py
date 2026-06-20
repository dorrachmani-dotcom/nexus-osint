"""subfinder adapter — passive subdomain enumeration for a domain.

subfinder (ProjectDiscovery) discovers subdomains of a domain from passive
sources (certificate transparency, DNS aggregators, ...). It runs with no API
key out of the box (extra sources unlock with keys) and emits JSON Lines with
`-silent -oJ`. Each discovered host becomes a "subdomain" finding. The parser
also tolerates plain one-host-per-line output in case the JSON flag is absent.
"""

from __future__ import annotations

import json

from nexus.adapters.base import Finding, ToolAdapter


class SubfinderAdapter(ToolAdapter):
    name = "subfinder"
    binary = "subfinder"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # -silent keeps stdout to results only; -oJ emits JSON Lines to stdout.
        return [self.binary, "-d", target, "-silent", "-oJ"]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            host = ""
            source = ""
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    host = str(obj.get("host", "")).strip()
                    source = str(obj.get("source", "")).strip()
                except json.JSONDecodeError:
                    host = ""
            if not host:
                # Plain output fallback: the line itself is a hostname.
                host = line if "." in line and " " not in line else ""
            host = host.lower()
            if not host or host in seen:
                continue
            seen.add(host)
            findings.append(
                Finding(
                    kind="subdomain",
                    value=host,
                    label=f"subdomain of {target.strip().lower()}",
                    extra={"source": source} if source else {},
                )
            )
        return findings
