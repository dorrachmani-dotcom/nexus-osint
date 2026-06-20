"""holehe adapter — find which sites an email address is registered on.

holehe checks an email against many services and prints one line per site. With
`--only-used` it emits only the sites where the address exists, which we map to
"account" findings keyed on the email. Output is parsed from stdout (holehe's
CSV export writes to the process CWD, not our temp dir, so stdout is simpler).
"""

from __future__ import annotations

from nexus.adapters.base import Finding, ToolAdapter


class HoleheAdapter(ToolAdapter):
    name = "holehe"
    binary = "holehe"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # --only-used keeps stdout to the hits; --no-color avoids ANSI noise.
        return [self.binary, target, "--only-used", "--no-color"]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for line in stdout.splitlines():
            line = line.strip()
            # Used services are flagged with a leading "[+]".
            if not line.startswith("[+]"):
                continue
            rest = line[3:].strip()
            if not rest:
                continue
            site = rest.split()[0].strip().lower()
            # Keep only domain-looking tokens, de-duplicated.
            if "." not in site or site in seen:
                continue
            seen.add(site)
            findings.append(Finding(kind="account", value=site, label=target))
        return findings
