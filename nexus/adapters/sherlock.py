"""Sherlock adapter — hunt a username across many sites.

Runs sherlock with file output into a temp dir, then reads the result file
(one found URL per line) into account findings.
"""

from __future__ import annotations

import os

from nexus.adapters.base import Finding, ToolAdapter


class SherlockAdapter(ToolAdapter):
    name = "sherlock"
    binary = "sherlock"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # --print-found keeps stdout tidy; --folderoutput writes <user>.txt there.
        return [
            self.binary,
            target,
            "--print-found",
            "--no-color",
            "--timeout",
            "20",
            "--folderoutput",
            workdir,
        ]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()

        result_file = os.path.join(workdir, f"{target}.txt")
        lines: list[str] = []
        if os.path.exists(result_file):
            with open(result_file, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        else:
            lines = stdout.splitlines()

        for line in lines:
            line = line.strip()
            if not line.startswith("http"):
                # sherlock stdout uses "[+] Site: url" — pull the URL out.
                if "http" in line:
                    line = line[line.index("http"):].strip()
                else:
                    continue
            url = line.split()[0]
            if url in seen:
                continue
            seen.add(url)
            findings.append(Finding(kind="account", value=url, label=target))
        return findings
