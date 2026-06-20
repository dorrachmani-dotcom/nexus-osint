"""socialscan adapter — check where a username/email is already in use.

socialscan queries platform sign-up endpoints to decide, per site, whether a
username or email is *available* or already *taken*. For investigation we care
about the "taken" verdicts: a confirmed in-use account on a platform. We request
JSON output to a file and emit one "account" finding per platform where the
target is reported in use (available == false, with a successful query).
"""

from __future__ import annotations

import json
import os

from nexus.adapters.base import Finding, ToolAdapter


def _is_false(value) -> bool:
    """socialscan serialises booleans as the strings "True"/"False"."""
    return str(value).strip().lower() in {"false", "0", "no"}


def _is_true(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


class SocialscanAdapter(ToolAdapter):
    name = "socialscan"
    binary = "socialscan"

    def build_command(self, target: str, workdir: str) -> list[str]:
        out = os.path.join(workdir, "socialscan.json")
        return [self.binary, target, "--json", out, "--view-by", "query"]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        path = os.path.join(workdir, "socialscan.json")
        if not os.path.exists(path):
            return findings
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return findings
        if not isinstance(data, dict):
            return findings

        seen: set[str] = set()
        for _query, rows in data.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                platform = str(row.get("platform", "")).strip()
                if not platform:
                    continue
                # A successful, valid query that reports "not available" means the
                # name is in use on that platform — i.e. an existing account.
                if not _is_true(row.get("success")):
                    continue
                if _is_false(row.get("available")) and _is_true(row.get("valid")):
                    key = platform.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(
                        Finding(
                            kind="account",
                            value=platform,
                            label=f"{target} in use",
                            extra={"status": "in use"},
                        )
                    )
        return findings
