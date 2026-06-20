"""theHarvester adapter — emails, hosts and IPs for a domain (JSON output).

Runs theHarvester with `-f <file>` JSON export and reads the emails / hosts /
IPs arrays into typed findings.
"""

from __future__ import annotations

import json
import os

from nexus.adapters.base import Finding, ToolAdapter


class TheHarvesterAdapter(ToolAdapter):
    name = "theHarvester"
    binary = "theHarvester"

    def build_command(self, target: str, workdir: str) -> list[str]:
        out = os.path.join(workdir, "harvest")
        # -b sources: a free, no-key engine keeps this runnable out of the box.
        return [
            self.binary,
            "-d",
            target,
            "-b",
            "duckduckgo",
            "-f",
            out,
        ]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        # theHarvester appends .json to the -f path.
        candidates = [
            os.path.join(workdir, "harvest.json"),
            os.path.join(workdir, "harvest"),
        ]
        data = None
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        data = json.load(fh)
                    break
                except (json.JSONDecodeError, OSError):
                    continue
        if not isinstance(data, dict):
            return findings

        for email in data.get("emails", []) or []:
            findings.append(Finding(kind="email", value=str(email), label=target))
        for host in data.get("hosts", []) or []:
            findings.append(Finding(kind="host", value=str(host), label=target))
        for ip in data.get("ips", []) or []:
            findings.append(Finding(kind="ip", value=str(ip), label=target))
        return findings
