"""Shared base for Plug & Play CLI tool adapters.

A ToolAdapter is an on-demand investigator (triggered from the dashboard with a
target like a username or domain), not a scheduled source. It runs a subprocess,
captures output, and returns structured findings. Availability is decided by
whether the underlying binary is on PATH (shutil.which), so a machine without a
given tool degrades gracefully instead of erroring.
"""

from __future__ import annotations

import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from nexus.config import Settings, get_settings

logger = logging.getLogger("nexus.adapters")

# Hard wall-clock cap so a hung tool can never block the request thread forever.
DEFAULT_TIMEOUT = 180


@dataclass
class Finding:
    """One normalized result row from a tool, ready for the entity graph."""

    kind: str  # e.g. "account", "email", "domain", "host"
    value: str  # the found identifier (profile URL, email, host, ...)
    label: str | None = None  # human label (e.g. the site name)
    source_tool: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """Outcome of one adapter run."""

    tool: str
    target: str
    available: bool
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    raw_output: str = ""


class ToolAdapter(ABC):
    #: Logical name shown in the UI (e.g. "sherlock").
    name: str = "tool"
    #: Binary looked up on PATH to decide availability.
    binary: str = ""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def is_available(self) -> bool:
        return self.settings.cli_tool_available(self.binary)

    @abstractmethod
    def build_command(self, target: str, workdir: str) -> list[str]:
        """Return the argv to execute for `target`, writing output into `workdir`."""

    @abstractmethod
    def parse(self, target: str, stdout: str, workdir: str) -> list[Finding]:
        """Turn captured output / files into Findings."""

    def run(self, target: str, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
        """Execute the tool against `target`, never raising on tool failure."""
        target = (target or "").strip()
        if not target:
            return ToolResult(self.name, target, self.is_available(), error="empty target")
        # Argument-injection guard. We never use shell=True (cmd is always an argv
        # list), so there is no shell-metacharacter risk — but most adapters place
        # the target as a *positional* argument, so a target that begins with "-"
        # (e.g. "--output=/etc/cron.d/x") would be parsed by the tool as a flag
        # rather than a value. Reject leading-dash targets outright; no legitimate
        # username / domain / email / phone starts with a dash.
        if target.startswith("-"):
            return ToolResult(
                self.name, target, self.is_available(), error="invalid target (leading dash)"
            )
        if not self.is_available():
            return ToolResult(self.name, target, False, error="tool not installed")

        import tempfile

        with tempfile.TemporaryDirectory(prefix=f"nexus_{self.name}_") as workdir:
            cmd = self.build_command(target, workdir)
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(self.name, target, True, error="timed out")
            except Exception as exc:  # missing exec bit, OS error, etc.
                logger.exception("Adapter %s failed to launch", self.name)
                return ToolResult(self.name, target, True, error=str(exc))

            stdout = proc.stdout or ""
            try:
                findings = self.parse(target, stdout, workdir)
            except Exception as exc:
                logger.exception("Adapter %s failed to parse output", self.name)
                return ToolResult(
                    self.name, target, True, error=f"parse error: {exc}", raw_output=stdout
                )

            for f in findings:
                f.source_tool = self.name
            return ToolResult(self.name, target, True, findings=findings, raw_output=stdout)
