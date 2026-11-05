"""Optional app password. Off by default.

Nexus listens on loopback only, so this is about the next person at the keyboard
(or another account on a shared computer), not about the network. It does not
encrypt anything: see "Encryption at rest" for data on disk.

  * Only a salted PBKDF2-HMAC-SHA256 hash is kept, in ``.env``. Forgot the
    password? Delete the ``APP_PASSWORD_HASH`` line and restart.
  * A sign-in gives a random session token in an HttpOnly, SameSite=Strict
    cookie. Sessions live in memory, so a restart signs everyone out. Not marked
    Secure: the app is served over plain http on loopback.
  * Wrong passwords back off exponentially, one attempt at a time.

``python -m nexus.security.apppass`` prints a hash line to paste into ``.env``.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import logging
import secrets
import threading
import time

from nexus.envstore import read_env_value, update_env
from nexus.security.atrest import UnlockThrottle

logger = logging.getLogger("nexus")

ENV_KEY = "APP_PASSWORD_HASH"
COOKIE = "nexus_session"
MIN_LENGTH = 8
MAX_LENGTH = 1024
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 3600
_SCHEME = "pbkdf2-sha256"

THROTTLE = UnlockThrottle()
_GATE = threading.Lock()  # parallel guesses cannot skip the back-off
_sessions: dict[str, float] = {}  # sha256(token) -> expiry (monotonic)
_sessions_lock = threading.Lock()


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> str:
    # Hex and colons only, so the value needs no quoting in .env.
    salt = secrets.token_bytes(16)
    return f"{_SCHEME}:{ITERATIONS}:{salt.hex()}:{_derive(password, salt, ITERATIONS).hex()}"


def verify(password: str, stored: str) -> bool:
    """Whether the password matches. A malformed hash matches nothing."""
    try:
        scheme, iterations, salt, expected = stored.split(":")
        if scheme != _SCHEME or len(password) > MAX_LENGTH:
            return False
        derived = _derive(password, bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(derived, bytes.fromhex(expected))
    except ValueError:
        return False


def is_enabled() -> bool:
    return bool(read_env_value(ENV_KEY))


def set_password(password: str | None) -> bool:
    """Set, or with ``None`` remove, the password. Ends every session.

    Returns whether ``.env`` now says what was asked. Written through ``protected``
    because the key is deliberately outside the keys the Settings form may write:
    only this module changes it, after checking the current password.
    """
    update_env({ENV_KEY: hash_password(password) if password else ""},
               protected=frozenset({ENV_KEY}))
    end_all_sessions()
    return is_enabled() == bool(password)


def attempt(password: str) -> tuple[bool, str | None, int]:
    """One password check, serialised. Returns (ok, error, seconds to wait)."""
    with _GATE:
        wait = THROTTLE.wait_seconds()
        if wait:
            return False, f"Too many wrong attempts. Try again in {wait} s.", wait
        if verify(password, read_env_value(ENV_KEY) or ""):
            THROTTLE.succeeded()
            return True, None, 0
        THROTTLE.failed()
        logger.warning("Sign-in attempt with a wrong app password")
        return False, "That password is not correct.", 0


def _key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.monotonic()
        for key in [k for k, until in _sessions.items() if until <= now]:
            del _sessions[key]
        _sessions[_key(token)] = now + SESSION_SECONDS
    return token


def session_valid(token: str | None) -> bool:
    """Whether the token is a live session; use extends it."""
    if not token:
        return False
    key = _key(token)
    with _sessions_lock:
        now = time.monotonic()
        if _sessions.get(key, 0.0) <= now:
            _sessions.pop(key, None)
            return False
        _sessions[key] = now + SESSION_SECONDS
    return True


def end_session(token: str | None) -> None:
    if token:
        with _sessions_lock:
            _sessions.pop(_key(token), None)


def end_all_sessions() -> None:
    with _sessions_lock:
        _sessions.clear()


def main() -> None:
    password = getpass.getpass("New app password: ")
    if len(password) < MIN_LENGTH or len(password) > MAX_LENGTH:
        raise SystemExit(f"Use {MIN_LENGTH} to {MAX_LENGTH} characters.")
    if getpass.getpass("Repeat it: ") != password:
        raise SystemExit("The two passwords differ.")
    print(f"{ENV_KEY}={hash_password(password)}")


if __name__ == "__main__":
    main()
