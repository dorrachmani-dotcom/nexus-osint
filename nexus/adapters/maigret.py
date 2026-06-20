"""Maigret adapter — username footprint across hundreds of sites (JSON output).

Maigret writes a `report_<username>_simple.json` (and similar) to the output
folder. We read the first JSON report we find and keep the claimed accounts.
"""

from __future__ import annotations

import glob
import json
import os

from nexus.adapters.base import Finding, ToolAdapter


class MaigretAdapter(ToolAdapter):
    name = "maigret"
    binary = "maigret"

    def build_command(self, target: str, workdir: str) -> list[str]:
        return [
            self.binary,
            target,
            "--json",
            "simple",
            "--folderoutput",
            workdir,
            "--no-color",
            "--no-progressbar",
        ]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        reports = glob.glob(os.path.join(workdir, "*.json"))
        if not reports:
            return findings

        try:
            with open(reports[0], encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return findings

        for site, info in (data.items() if isinstance(data, dict) else []):
            if not isinstance(info, dict):
                continue
            status = info.get("status", {})
            claimed = isinstance(status, dict) and status.get("status") == "Claimed"
            url = info.get("url_user") or info.get("url")
            if claimed and url:
                findings.append(Finding(kind="account", value=url, label=site))
        return findings
