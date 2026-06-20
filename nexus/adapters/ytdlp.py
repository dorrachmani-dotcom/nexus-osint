"""yt-dlp adapter — pull public metadata from a video / social media URL.

Given a link (YouTube, TikTok, Twitter/X, many others) yt-dlp can extract rich
metadata *without downloading the media* via `--dump-json --skip-download`. For
investigation we surface who is behind the content: the uploader / channel, its
profile URL, and the canonical page. Nothing is written to disk. Each useful
field becomes a finding (account / url) keyed on the target link.
"""

from __future__ import annotations

import json

from nexus.adapters.base import Finding, ToolAdapter


class YtDlpAdapter(ToolAdapter):
    name = "yt-dlp"
    binary = "yt-dlp"

    def build_command(self, target: str, workdir: str) -> list[str]:
        # --dump-json prints one JSON object per entry; --skip-download keeps it
        # metadata-only; --no-warnings + --ignore-errors keep stdout parseable.
        return [
            self.binary,
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            "--ignore-errors",
            target,
        ]

    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()

        def add(kind: str, value, label: str) -> None:
            value = str(value or "").strip()
            if not value:
                return
            key = f"{kind}:{value.lower()}"
            if key in seen:
                return
            seen.add(key)
            findings.append(Finding(kind=kind, value=value, label=label))

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                meta = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(meta, dict):
                continue

            platform = str(meta.get("extractor_key") or meta.get("extractor") or "").strip()
            uploader = meta.get("uploader") or meta.get("channel") or meta.get("creator")
            add("account", uploader, f"{platform} uploader" if platform else "uploader")
            add("url", meta.get("uploader_url"), "uploader profile")
            add("url", meta.get("channel_url"), "channel")
            add("url", meta.get("webpage_url"), "content page")
            uid = meta.get("uploader_id") or meta.get("channel_id")
            add("account", uid, f"{platform} id" if platform else "uploader id")
        return findings
