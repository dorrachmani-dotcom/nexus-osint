"""Shared SSRF guard for any server-side fetch of an untrusted URL.

Several features fetch a URL that ultimately comes from outside the operator's
control — a collected feed item (evidence capture), a user-defined custom API
source, an RSS feed URL, or a URL proposed by the AI source planner. Without a
guard a crafted URL could point the server at:

  * ``file:///etc/passwd`` / ``ftp:`` / ``data:`` — local-file or scheme abuse, or
  * an internal/loopback/link-local address (``127.0.0.1``, ``169.254.169.254``
    cloud metadata, private LAN hosts) — i.e. SSRF against the very host the
    platform runs on.

``safe_http_url`` centralises that check so every fetch path enforces the same
policy: only ``http``/``https``, and only hosts that resolve exclusively to
public IP addresses.

It never raises; on any unexpected error it fails closed (returns not-ok).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def safe_http_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a URL we are about to fetch server-side.

    ``reason`` is empty when ``ok`` is True; otherwise it is a short human-readable
    explanation suitable for a log line or a user-facing note.
    """
    try:
        try:
            parts = urlsplit(url or "")
        except ValueError:
            return False, "unparseable URL"

        if parts.scheme.lower() not in ("http", "https"):
            return False, f"refusing non-http(s) scheme '{parts.scheme}'"

        host = parts.hostname
        if not host:
            return False, "URL has no host"

        # Resolve every address the host maps to and reject if *any* is non-public
        # — this also blocks DNS names that point at internal IPs, not just literal
        # IP URLs.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # Can't resolve: refuse rather than hand an unverifiable host onward.
            return False, f"could not resolve host '{host}'"

        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr.split("%")[0])  # strip IPv6 zone id
            except ValueError:
                continue
            # Evaluate the address itself and, for an IPv4-mapped IPv6 address
            # (``::ffff:127.0.0.1``), also the embedded IPv4. Older Python (e.g.
            # 3.11 in the Docker image) does NOT delegate is_private/is_loopback
            # through the mapping, so ``::ffff:10.0.0.1`` would otherwise slip
            # past the guard. Checking the mapped form makes this version-proof.
            candidates = [ip]
            mapped = getattr(ip, "ipv4_mapped", None)
            if mapped is not None:
                candidates.append(mapped)
            for cand in candidates:
                if (
                    cand.is_private
                    or cand.is_loopback
                    or cand.is_link_local
                    or cand.is_reserved
                    or cand.is_multicast
                    or cand.is_unspecified
                ):
                    return False, f"refusing non-public host '{host}' ({addr})"

        return True, ""
    except Exception as exc:  # fail closed on anything unexpected
        return False, f"URL safety check failed: {exc}"
