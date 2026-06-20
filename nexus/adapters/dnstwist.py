"""dnstwist adapter — detect typosquatting / brand-impersonation domains.

dnstwist permutes a domain name (typos, homoglyphs, TLD swaps, ...) and resolves
each candidate, surfacing look-alike domains that someone may have registered to
impersonate the target. We request JSON output and keep only the *registered*
permutations (those that resolved to an address), since unregistered noise has
no investigative value. Each hit becomes a "domain" finding with its resolved
A record(s) carried in `extra`.
"""

from __future__ import annotations

import json

from nexus.adapters.base import Finding, ToolAdapter


class DnstwistAdapter(ToolAdapter):
    name = "dnstwist"
    binary = "dnstwist"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # --registered keeps only permutations that actually resolve (real
        # look-alikes); JSON to stdout is clean and easy to parse.
        return [
            self.binary,
            "--format",
            "json",
            "--registered",
            "--threads",
            "20",
            target,
        ]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        stdout = (stdout or "").strip()
        if not stdout:
            return findings
        try:
            rows = json.loads(stdout)
        except json.JSONDecodeError:
            return findings
        if not isinstance(rows, list):
            return findings

        target_norm = target.strip().lower()
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            domain = str(row.get("domain", "")).strip().lower()
            if not domain or domain in seen:
                continue
            # Skip the original domain itself (fuzzer == "*original").
            if domain == target_norm or row.get("fuzzer") == "*original":
                continue
            seen.add(domain)
            a_records = row.get("dns_a") or []
            findings.append(
                Finding(
                    kind="domain",
                    value=domain,
                    label=f"look-alike of {target_norm}",
                    extra={
                        "fuzzer": row.get("fuzzer", ""),
                        "dns_a": a_records,
                        "dns_mx": row.get("dns_mx") or [],
                        "dns_ns": row.get("dns_ns") or [],
                    },
                )
            )
        return findings
