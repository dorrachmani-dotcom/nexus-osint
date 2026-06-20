"""h8mail adapter — hunt an email address in data breaches / leaks.

h8mail looks an email up against breach sources and writes a JSON report. Out of
the box (no API keys, no local breach dump) results are limited — its full power
comes from configured keys or a local breach file — but the adapter degrades
gracefully: with nothing configured it simply returns no findings instead of
failing. Each breach/source hit becomes a "breach" finding keyed on the email.
"""

from __future__ import annotations

import json
import os

from nexus.adapters.base import Finding, ToolAdapter


class H8mailAdapter(ToolAdapter):
    name = "h8mail"
    binary = "h8mail"

    def build_command(self, target: str, workdir: str) -> list[str]:
        out = os.path.join(workdir, "h8mail.json")
        # -sk skips the unreliable default online sources (Scylla/HunterIO) so a
        # keyless run stays fast instead of hanging on a dead endpoint.
        return [self.binary, "-t", target, "-j", out, "-sk"]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        path = os.path.join(workdir, "h8mail.json")
        if not os.path.exists(path):
            return findings
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return findings

        # h8mail's JSON shape is a list of target records, each with a "data"
        # list of "<source>:<value>" style entries. Be tolerant of variations.
        targets = data.get("targets", data) if isinstance(data, dict) else data
        if not isinstance(targets, list):
            return findings

        seen: set[str] = set()
        for rec in targets:
            if not isinstance(rec, dict):
                continue
            email = str(rec.get("target", target)).strip() or target
            for entry in rec.get("data", []) or []:
                text = entry if isinstance(entry, str) else json.dumps(entry)
                text = text.strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                # Entries look like "SOURCE:detail"; use the source as the label.
                source = text.split(":", 1)[0].strip() if ":" in text else "breach"
                findings.append(
                    Finding(
                        kind="breach",
                        value=text,
                        label=f"{email} ({source})",
                    )
                )
        return findings
