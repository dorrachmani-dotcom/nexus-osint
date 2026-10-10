"""Connect Gmail with send-only permission (OAuth 2.0), no password.

Flow (Google's "OAuth 2.0 for installed applications"):

1. The operator creates their own Google Cloud OAuth client of type *Desktop app*
   and pastes its client ID / secret in Settings (a public repository cannot
   ship a shared client: the secret would not be secret).
2. ``start()`` builds the consent URL with PKCE (S256) and a random ``state``,
   redirecting back to this local app on the loopback address.
3. ``finish()`` checks ``state``, exchanges the code (with the PKCE verifier) for a
   refresh token, and reads the account address from the ID token.
4. ``send_raw()`` trades the refresh token for a short-lived access token and calls
   the Gmail API ``users.messages.send``.

Scopes: ``gmail.send`` (send only; it cannot read, list or delete mail) plus
``openid email`` to learn which address was connected. The refresh token is
stored in ``.env`` only, never in the database, logs or UI, and can be revoked
from the Google account or with "Disconnect".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("nexus.google_oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = ("https://www.googleapis.com/auth/gmail.send", "openid", "email")
TIMEOUT = 20.0
_PENDING_TTL = 600.0  # a started connection must finish within 10 minutes

_lock = threading.Lock()
_pending: dict[str, tuple[str, str, float]] = {}  # state -> (verifier, redirect, t)
_access: dict[str, tuple[str, float]] = {}  # refresh-token fingerprint -> (token, expiry)


@dataclass
class OAuthResult:
    ok: bool
    message: str
    refresh_token: str = ""
    email: str = ""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def is_loopback_redirect(redirect_uri: str) -> bool:
    """Google only accepts loopback redirects for Desktop clients; so do we."""
    return redirect_uri.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))


def start(client_id: str, redirect_uri: str) -> str:
    """Return the Google consent URL for this connection attempt."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        for key, (_v, _r, t) in list(_pending.items()):
            if now - t > _PENDING_TTL:
                del _pending[key]
        _pending[state] = (verifier, redirect_uri, now)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # always return one, even on reconnect
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _email_from_id_token(id_token: str) -> str:
    """Read the address from an ID token received directly from Google's token
    endpoint over TLS (no signature check needed in that case, per OpenID)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if claims.get("email_verified") in (True, "true"):
            return str(claims.get("email") or "")
    except (IndexError, ValueError, TypeError):
        logger.debug("Could not read the ID token", exc_info=True)
    return ""


def finish(client_id: str, client_secret: str, state: str, code: str,
           *, client: httpx.Client | None = None) -> OAuthResult:
    """Complete the connection. Never raises; errors are plain English."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if pending is None or time.monotonic() - pending[2] > _PENDING_TTL:
        return OAuthResult(False, "This connection attempt expired or was not started here. "
                                  "Click “Connect Gmail” again.")
    verifier, redirect_uri, _t = pending
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        c = client or httpx.Client(timeout=TIMEOUT)
        try:
            resp = c.post(TOKEN_URL, data=data)
        finally:
            if client is None:
                c.close()
    except httpx.HTTPError as exc:
        return OAuthResult(False, f"Could not reach Google ({type(exc).__name__}). Check your connection.")
    if resp.status_code != 200:
        return OAuthResult(False, _token_error(resp))
    body = resp.json() or {}
    refresh = str(body.get("refresh_token") or "")
    scope = str(body.get("scope") or "")
    if not refresh:
        return OAuthResult(False, "Google did not return a refresh token. Remove Nexus-OSINT from "
                                  "your Google account's third-party access and connect again.")
    if SCOPES[0] not in scope.split():
        return OAuthResult(False, "The send-only permission was not granted. Connect again and tick "
                                  "“Send email on your behalf”.")
    return OAuthResult(True, "Gmail connected.", refresh_token=refresh,
                       email=_email_from_id_token(str(body.get("id_token") or "")))


def _token_error(resp: httpx.Response) -> str:
    try:
        err = str((resp.json() or {}).get("error") or "")
    except ValueError:
        err = ""
    if err == "invalid_grant":
        return ("Google says the permission is no longer valid (expired or revoked). If your Google "
                "Cloud project is in “Testing” mode, Google expires it after 7 days: publish the app "
                "(see the steps) and click “Connect Gmail” again.")
    if err in ("invalid_client", "unauthorized_client"):
        return "Google rejected the client ID or secret. Check both values from your Desktop OAuth client."
    return f"Google refused the request (HTTP {resp.status_code}{', ' + err if err else ''})."


def _access_token(client_id: str, client_secret: str, refresh_token: str,
                  client: httpx.Client) -> tuple[str, str]:
    """(access token, error). Cached until shortly before it expires."""
    key = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    with _lock:
        cached = _access.get(key)
    if cached and cached[1] > time.monotonic() + 60:
        return cached[0], ""
    resp = client.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        return "", _token_error(resp)
    body = resp.json() or {}
    token = str(body.get("access_token") or "")
    if not token:
        return "", "Google did not return an access token."
    with _lock:
        _access[key] = (token, time.monotonic() + float(body.get("expires_in") or 3000))
    return token, ""


def send_raw(client_id: str, client_secret: str, refresh_token: str, message_bytes: bytes,
             *, client: httpx.Client | None = None) -> tuple[bool, str]:
    """Send one RFC 822 message through the Gmail API. Never raises."""
    c = client or httpx.Client(timeout=TIMEOUT)
    try:
        token, err = _access_token(client_id, client_secret, refresh_token, c)
        if err:
            return False, err
        resp = c.post(SEND_URL, json={"raw": _b64url(message_bytes)},
                      headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (401, 403):
            return False, (f"Gmail refused to send (HTTP {resp.status_code}). Make sure the Gmail "
                           "API is enabled in your Google Cloud project, then reconnect.")
        if resp.status_code >= 400:
            return False, f"Gmail refused the message (HTTP {resp.status_code})."
        return True, "Sent through the Gmail API."
    except httpx.HTTPError as exc:
        return False, f"Could not reach Gmail ({type(exc).__name__}). Check your connection."
    finally:
        if client is None:
            c.close()


def revoke(refresh_token: str, *, client: httpx.Client | None = None) -> None:
    """Best-effort revoke at Google. Local keys are cleared by the caller either way."""
    try:
        c = client or httpx.Client(timeout=TIMEOUT)
        try:
            c.post(REVOKE_URL, data={"token": refresh_token})
        finally:
            if client is None:
                c.close()
    except httpx.HTTPError:
        logger.debug("Google token revoke failed", exc_info=True)
    with _lock:
        _access.pop(hashlib.sha256(refresh_token.encode("utf-8")).hexdigest(), None)
