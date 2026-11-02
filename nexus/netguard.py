"""Shared SSRF guard for any server-side fetch of an untrusted URL.

Several features fetch a URL that ultimately comes from outside the operator's
control — a collected feed item (evidence capture), a user-defined custom API
source, a watched web page, an RSS feed URL, or a URL proposed by the AI source
planner. Without a guard a crafted URL could point the server at:

  * ``file:///etc/passwd`` / ``ftp:`` / ``data:`` — local-file or scheme abuse, or
  * an internal/loopback/link-local address (``127.0.0.1``, ``169.254.169.254``
    cloud metadata, private LAN hosts) — i.e. SSRF against the very host the
    platform runs on.

``safe_http_url`` centralises that check so every fetch path enforces the same
policy: only ``http``/``https``, and only hosts that resolve exclusively to
public IP addresses.

**Closed / air-gapped networks.** An analyst may legitimately want to ingest an
intranet wiki or an internal API (``wiki.corp``, ``10.20.4.7``…). That is an
explicit, opt-in allow-list in ``.env`` (also editable on the Settings page):

  * ``INTERNAL_ALLOWED_HOSTS`` — host names or wildcards (``wiki.corp``,
    ``*.corp.example``), and
  * ``INTERNAL_ALLOWED_CIDRS`` — networks (``10.20.0.0/16``).

Only a target that matches the list may resolve to a private address. Even
then, loopback (127.0.0.0/8, ::1), link-local (169.254.0.0/16 — including the
cloud metadata endpoint), multicast, reserved and unspecified addresses stay
blocked, as does the app's own port on any internal address. Every redirect
hop is re-checked against the same policy (see :func:`safe_request`).

Nothing here ever raises from the check itself; on any unexpected error it
fails closed (returns not-ok).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger("nexus.netguard")

_MAX_REDIRECTS = 5
# The app's own HTTP port: never a valid target on an internal address.
_APP_PORT = int(os.environ.get("NEXUS_PORT", "8000") or 8000)

_IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network
_IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


# --------------------------------------------------------------- the policy
@dataclass(frozen=True)
class InternalPolicy:
    """The operator's opt-in allow-list for internal (private) targets."""

    hosts: tuple[str, ...] = ()
    networks: tuple[_IPNet, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.hosts or self.networks)

    def host_allowed(self, host: str | None) -> bool:
        h = (host or "").strip().lower().rstrip(".")
        if not h:
            return False
        return any(fnmatch.fnmatchcase(h, pat) for pat in self.hosts)

    def ip_allowed(self, ip: _IPAddr) -> bool:
        return any(ip.version == net.version and ip in net for net in self.networks)


def parse_policy(hosts_raw: str | None, cidrs_raw: str | None) -> InternalPolicy:
    """Parse the comma/space separated .env values. Bad entries are skipped.

    A host pattern may use ``*`` wildcards. Patterns that would match
    everything (``*``, ``*.*``) are rejected — the allow-list must name
    something specific.
    """
    hosts: list[str] = []
    for tok in (hosts_raw or "").replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip().lower().rstrip(".")
        if not tok or tok.strip("*.") == "":
            continue
        hosts.append(tok)
    nets: list[_IPNet] = []
    for tok in (cidrs_raw or "").replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            logger.warning("Ignoring an invalid INTERNAL_ALLOWED_CIDRS entry")
            continue
        if net.prefixlen == 0:
            logger.warning("Ignoring a catch-all INTERNAL_ALLOWED_CIDRS entry (/0)")
            continue
        nets.append(net)
    return InternalPolicy(hosts=tuple(hosts), networks=tuple(nets))


@lru_cache(maxsize=8)
def _cached_policy(hosts_raw: str, cidrs_raw: str) -> InternalPolicy:
    return parse_policy(hosts_raw, cidrs_raw)


def internal_policy() -> InternalPolicy:
    """The live policy from settings (re-read so a Settings edit applies at once)."""
    try:
        from nexus.config import get_settings

        s = get_settings()
        return _cached_policy(
            str(getattr(s, "internal_allowed_hosts", "") or ""),
            str(getattr(s, "internal_allowed_cidrs", "") or ""),
        )
    except Exception:
        logger.debug("Could not read the internal-network policy", exc_info=True)
        return InternalPolicy()


def _candidates(ip: _IPAddr) -> list[_IPAddr]:
    # Evaluate the address itself and, for an IPv4-mapped IPv6 address
    # (``::ffff:127.0.0.1``), also the embedded IPv4. Older Python (e.g.
    # 3.11 in the Docker image) does NOT delegate is_private/is_loopback
    # through the mapping, so ``::ffff:10.0.0.1`` would otherwise slip
    # past the guard. Checking the mapped form makes this version-proof.
    out: list[_IPAddr] = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        out.append(mapped)
    return out


def _always_blocked(ip: _IPAddr) -> bool:
    """Never a valid target, allow-listed or not."""
    return any(
        c.is_loopback or c.is_link_local or c.is_multicast or c.is_unspecified or c.is_reserved
        for c in _candidates(ip)
    )


def _is_private(ip: _IPAddr) -> bool:
    return any(c.is_private for c in _candidates(ip))


def _effective(ip: _IPAddr) -> _IPAddr:
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _default_port(parts) -> int:
    if parts.port:
        return int(parts.port)
    return 443 if parts.scheme.lower() == "https" else 80


def check_url(url: str, policy: InternalPolicy | None = None) -> tuple[bool, str, bool]:
    """Return ``(ok, reason, internal)`` for a URL about to be fetched.

    ``internal`` is True when the target is an allow-listed private address.
    """
    try:
        try:
            parts = urlsplit(url or "")
        except ValueError:
            return False, "unparseable URL", False

        if parts.scheme.lower() not in ("http", "https"):
            return False, f"refusing non-http(s) scheme '{parts.scheme}'", False

        host = parts.hostname
        if not host:
            return False, "URL has no host", False
        try:
            port = _default_port(parts)
        except ValueError:
            return False, "invalid port", False

        policy = policy if policy is not None else internal_policy()
        host_listed = policy.host_allowed(host)

        # Resolve every address the host maps to and reject if *any* is not
        # acceptable — this also blocks DNS names that point at internal IPs,
        # not just literal IP URLs.
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError):
            # Can't resolve: refuse rather than hand an unverifiable host onward.
            return False, f"could not resolve host '{host}'", False

        internal = False
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(str(addr).split("%")[0])  # strip IPv6 zone id
            except ValueError:
                continue
            if _always_blocked(ip):
                return False, (f"refusing non-public host '{host}' ({addr}): loopback, "
                               "link-local and reserved addresses are never allowed"), False
            if _is_private(ip):
                if host_listed or policy.ip_allowed(_effective(ip)):
                    internal = True
                    continue
                hint = (" — add it to INTERNAL_ALLOWED_HOSTS or INTERNAL_ALLOWED_CIDRS "
                        "(Settings > Internal network) to allow it")
                return False, f"refusing non-public host '{host}' ({addr}){hint}", False

        if internal and port == _APP_PORT:
            return False, f"refusing the app's own port {port} on an internal host", False
        return True, "", internal
    except Exception as exc:  # fail closed on anything unexpected
        return False, f"URL safety check failed: {exc}", False


def safe_http_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a URL we are about to fetch server-side.

    ``reason`` is empty when ``ok`` is True; otherwise it is a short human-readable
    explanation suitable for a log line or a user-facing note.
    """
    ok, reason, _internal = check_url(url)
    return ok, reason


def is_internal_destination(host: str) -> bool:
    """True when ``host`` (a name or an IP literal) is on the internal allow-list.

    Used by the egress monitor to label such connections "internal (allowed)".
    Never resolves DNS and never raises.
    """
    try:
        policy = internal_policy()
        if not policy.enabled:
            return False
        if policy.host_allowed(host):
            return True
        try:
            ip = ipaddress.ip_address((host or "").strip("[]").split("%")[0])
        except ValueError:
            return False
        return not _always_blocked(ip) and policy.ip_allowed(_effective(ip))
    except Exception:
        return False


# ------------------------------------------------------------ fetching
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


class ResponseTooLarge(ValueError):
    """The response body exceeded the caller's ``max_bytes``."""


def tls_verify_for(mode: str | None) -> bool | str:
    """Map a per-source TLS mode to an httpx ``verify`` value.

    ``verify`` (default) → system trust store; ``ca_bundle`` → the PEM file in
    ``INTERNAL_CA_BUNDLE`` (falls back to verifying normally if it is unset or
    missing); ``insecure`` → no verification (opt-in, per source, labelled).
    """
    mode = (mode or "verify").lower()
    if mode == "insecure":
        return False
    if mode == "ca_bundle":
        try:
            from nexus.config import get_settings

            path = str(getattr(get_settings(), "internal_ca_bundle", "") or "").strip()
        except Exception:
            path = ""
        if path and os.path.isfile(path):
            return path
        logger.warning("INTERNAL_CA_BUNDLE is not set or missing; verifying TLS normally")
    return True


def safe_request(
    method: str,
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict | None = None,
    params: dict | None = None,
    json: object | None = None,
    data: dict | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    verify: bool | str = True,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int | None = None,
) -> httpx.Response:
    """Send a request to an untrusted URL, re-checking EVERY redirect hop.

    ``follow_redirects=True`` would let a vetted URL bounce the request to
    ``http://127.0.0.1/...`` or a private LAN host after the first check passed.
    Here redirects are followed by hand and each new target must pass the same
    policy (:func:`check_url`). Credentials (Authorization/Cookie headers and
    ``auth``) are dropped when a redirect leaves the original host. 301/302/303
    turn into a body-less GET, 307/308 keep the method and body.

    The body is read in bounded pieces; more than ``max_bytes`` raises
    :class:`ResponseTooLarge`. Raises ``ValueError`` when a hop is refused and
    the usual httpx errors on network failure. The returned response is fully
    read (safe to use after the client closes).
    """
    current = url
    method = method.upper()
    hdrs = dict(headers or {})
    origin_host = (urlsplit(url).hostname or "").lower()
    cur_params, cur_json, cur_data, cur_auth = params, json, data, auth
    with httpx.Client(timeout=timeout, follow_redirects=False, verify=verify) as client:
        for _ in range(max_redirects + 1):
            ok, reason = safe_http_url(current)  # module-level lookup: patchable
            if not ok:
                raise ValueError(reason)
            request = client.build_request(
                method, current, headers=hdrs, params=cur_params, json=cur_json, data=cur_data,
            )
            resp = client.send(request, auth=cur_auth, stream=True)
            try:
                if resp.is_redirect and resp.headers.get("location"):
                    nxt = urljoin(str(resp.url), resp.headers["location"])
                    if resp.status_code in (301, 302, 303) and method != "GET":
                        method, cur_json, cur_data = "GET", None, None
                    cur_params = None  # the Location already carries its query
                    if (urlsplit(nxt).hostname or "").lower() != origin_host:
                        hdrs = {k: v for k, v in hdrs.items()
                                if k.lower() not in _SENSITIVE_HEADERS}
                        cur_auth = None
                    current = nxt
                    continue
                body = bytearray()
                for piece in resp.iter_bytes():
                    body += piece
                    if max_bytes is not None and len(body) > max_bytes:
                        raise ResponseTooLarge(
                            f"response is larger than {max_bytes // 1024} KB"
                        )
            finally:
                resp.close()
            # Re-wrap the already-decoded body (drop transfer/encoding headers so
            # httpx does not try to decode it a second time).
            clean = [(k, v) for k, v in resp.headers.multi_items()
                     if k.lower() not in ("content-encoding", "content-length",
                                          "transfer-encoding")]
            return httpx.Response(
                resp.status_code, headers=clean, content=bytes(body), request=resp.request,
            )
    raise ValueError("too many redirects")


def safe_get(url: str, *, timeout: float = 20.0, headers: dict | None = None,
             max_redirects: int = _MAX_REDIRECTS, verify: bool | str = True,
             max_bytes: int | None = None,
             auth: httpx.Auth | tuple[str, str] | None = None) -> httpx.Response:
    """GET an untrusted URL with the redirect-safe policy of :func:`safe_request`."""
    return safe_request(
        "GET", url, timeout=timeout, headers=headers, max_redirects=max_redirects,
        verify=verify, max_bytes=max_bytes, auth=auth,
    )


def is_public_host(host: str) -> bool:
    """Host-level verdict for request interception (e.g. the evidence browser),
    where every sub-resource of a page needs a quick check. Cached per policy."""
    policy = internal_policy()
    return _host_verdict(host, policy)


@lru_cache(maxsize=512)
def _host_verdict(host: str, policy: InternalPolicy) -> bool:
    return check_url(f"http://{host}/", policy)[0]
