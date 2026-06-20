"""Security layer: outbound-traffic monitoring and file safety scanning.

Two defensive features live here, both fully local and graceful (never crash the
app, never phone home themselves):

  * ``egress`` — watches every outbound network connection the application's own
    Python code makes and classifies each destination as expected (an AI
    provider, a source the operator configured, or the local machine) or
    UNEXPECTED. This gives the operator an honest, auditable answer to "is this
    tool sending my data anywhere it shouldn't?".
  * ``filescan`` — inspects a file the operator brings in (e.g. an imported
    transfer bundle, or any file they want to vet) and reports whether it looks
    clean or suspicious, entirely on this machine.
"""

from __future__ import annotations

from nexus.security.egress import (
    format_audit_report,
    get_egress_report,
    install_egress_monitor,
    record_connection,
)
from nexus.security.filescan import scan_bytes, scan_file

__all__ = [
    "format_audit_report",
    "get_egress_report",
    "install_egress_monitor",
    "record_connection",
    "scan_bytes",
    "scan_file",
]
