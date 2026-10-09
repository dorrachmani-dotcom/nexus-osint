"""GHunt adapter — what a Google (Gmail) account exposes.

GHunt ("ghunt email --json <file> <email>") resolves a Gmail address to its
public Google footprint: display name, Gaia ID, profile photos, and which
Google services are in use. It requires a one-time `ghunt login` beforehand; if
that has not been done the tool errors and we degrade gracefully (no findings).

The JSON schema shifts between GHunt versions, so parsing is deliberately
defensive: we walk the document and pick out the few fields we care about by
key name rather than assuming a fixed layout.
"""

from __future__ import annotations

import json
import os

from nexus.adapters.base import Finding, ToolAdapter

_MAX_NODES = 5000  # guard against a pathological document


class GHuntAdapter(ToolAdapter):
    name = "ghunt"
    binary = "ghunt"

    def build_command(self, target: str, workdir: str) -> list[str]:
        out = os.path.join(workdir, "ghunt.json")
        return [self.binary, "email", "--json", out, target]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        path = os.path.join(workdir, "ghunt.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return []

        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, value: str) -> None:
            value = (value or "").strip()
            if not value or (kind, value) in seen:
                return
            seen.add((kind, value))
            findings.append(Finding(kind=kind, value=value, label=target))

        budget = [_MAX_NODES]  # nodes left to visit, shared by the recursion

        def walk(node, key_hint: str = "") -> None:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, str(k).lower())
            elif isinstance(node, list):
                for v in node:
                    walk(v, key_hint)
            else:
                text = str(node).strip()
                if not text:
                    return
                if text.startswith("http"):
                    kind = "profile_photo" if "googleusercontent" in text else "url"
                    add(kind, text.split()[0])
                elif "gaia" in key_hint and text.isdigit():
                    add("gaia_id", text)
                elif key_hint in {"name", "displayname", "profilename", "fullname"}:
                    add("name", text)
                elif "email" in key_hint and "@" in text:
                    add("email", text)

        walk(data)
        return findings
