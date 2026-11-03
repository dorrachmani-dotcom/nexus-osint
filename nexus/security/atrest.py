"""Opt-in encryption at rest for stored files (uploaded originals, evidence).

Off by default. When the analyst enables it, a random 256-bit data key (DEK)
is generated and stored only *wrapped* in ``<data>/keystore.json``:

* by the analyst's passphrase (scrypt -> AES-256-GCM) — always;
* by a one-time recovery code shown once at enable time — always;
* by Windows DPAPI for the current Windows user (optional), so the app unlocks
  itself on start without asking.

The key is never written to the database, ``.env``, logs or the UI. While the
app runs it lives only in this process's memory. Without DPAPI (or on Linux /
macOS) the app starts *locked* until the passphrase is entered on ``/unlock``
(or supplied in ``NEXUS_ENCRYPTION_PASSPHRASE`` for headless use).

File format ``NXE1``: ``magic(4) | version(1) | salt(16)`` then 1 MiB chunks,
each AES-256-GCM sealed with a per-file key ``HKDF(file-key, salt)``; the
nonce is the chunk counter and the AAD binds the header, the chunk index and a
"final chunk" flag, so truncating, reordering or splicing chunks is detected.
Plaintext files written before encryption was enabled stay readable (mixed
state); :func:`encrypt_existing_files` converts them.

Graceful degradation: without the ``cryptography`` package the feature is
simply unavailable; nothing else changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from nexus.config import get_settings

logger = logging.getLogger("nexus.security.atrest")

MAGIC = b"NXE1"
_VERSION = 1
_SALT_LEN = 16
_HEADER_LEN = len(MAGIC) + 1 + _SALT_LEN
_CHUNK = 1024 * 1024
_TAG = 16
_KEYSTORE = "keystore.json"
_PASSPHRASE_ENV = "NEXUS_ENCRYPTION_PASSPHRASE"  # noqa: S105 - env var name
MIN_PASSPHRASE = 12

# scrypt cost: ~0.3 s and 128 MiB per unlock — cheap for one login, expensive
# for an offline guesser. maxmem must be raised explicitly (OpenSSL default 32 MiB).
_SCRYPT = {"n": 2**17, "r": 8, "p": 1}
_SCRYPT_MAXMEM = 256 * 1024 * 1024

# Folders (under the data dir) whose files are protected.
PROTECTED_DIRS = ("uploads", "evidence")


class EncryptionError(Exception):
    """Base error for the at-rest layer."""


class LockedError(EncryptionError):
    """Encrypted data was needed but the key has not been unlocked yet."""


class WrongPassphraseError(EncryptionError):
    """The passphrase / recovery code did not unwrap the key."""


def crypto_available() -> bool:
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
    except ImportError:
        return False
    return True


def dpapi_available() -> bool:
    return sys.platform == "win32"


# ----------------------------------------------------------------- key state
_lock = threading.Lock()
# path of the keystore the in-memory key belongs to -> DEK. Keyed by path so a
# different data folder (tests, a second profile) never sees another's key.
_keys: dict[str, bytes] = {}


def keystore_path() -> Path:
    return get_settings().data_path / _KEYSTORE


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def load_keystore(path: Path | None = None) -> dict[str, Any] | None:
    """The keystore as a dict, or None when encryption was never enabled.

    Falls back to the ``.bak`` copy if the main file is unreadable, so a torn
    write can never lose the wrapped key.
    """
    path = path or keystore_path()
    for candidate in (path, path.with_name(path.name + ".bak")):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Keystore copy %s is unreadable; trying the backup", candidate.name)
            continue
        if isinstance(data, dict) and data.get("version") == 1:
            return data
    return None


def _write_keystore(data: dict[str, Any], path: Path) -> None:
    """Atomic write (tmp + fsync + replace) and a ``.bak`` twin."""
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    for target in (path, path.with_name(path.name + ".bak")):
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".keystore-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def status() -> str:
    """``off`` (never enabled), ``locked`` or ``unlocked``."""
    path = keystore_path()
    if load_keystore(path) is None:
        return "off"
    with _lock:
        return "unlocked" if str(path) in _keys else "locked"


def is_enabled() -> bool:
    return status() != "off"


def _current_key() -> bytes | None:
    with _lock:
        return _keys.get(str(keystore_path()))


def _set_key(dek: bytes) -> None:
    with _lock:
        _keys[str(keystore_path())] = dek


def lock() -> None:
    """Forget the in-memory key (tests; a future "lock now" button)."""
    with _lock:
        _keys.pop(str(keystore_path()), None)


# ------------------------------------------------------------ crypto helpers
def _aead(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def _hkdf(key: bytes, info: bytes, salt: bytes | None = None) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def _scrypt(passphrase: str, salt: bytes, params: dict[str, int]) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=int(params["n"]), r=int(params["r"]),
        p=int(params["p"]), maxmem=_SCRYPT_MAXMEM, dklen=32,
    )


def _wrap(kek: bytes, dek: bytes, label: bytes) -> dict[str, str]:
    nonce = secrets.token_bytes(12)
    return {"nonce": _b64(nonce), "ct": _b64(_aead(kek).encrypt(nonce, dek, label))}


def _unwrap(kek: bytes, blob: dict[str, str], label: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag

    try:
        return _aead(kek).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), label)
    except (InvalidTag, KeyError, ValueError) as exc:
        raise WrongPassphraseError("wrong passphrase or recovery code") from exc


_B32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _recovery_kek(code: str) -> bytes:
    # The recovery code is 160 random bits, so a fast KDF is enough.
    norm = "".join(ch for ch in code.upper() if ch in _B32_ALPHABET)
    return _hkdf(norm.encode("ascii"), b"nexus-recovery-v1")


def _new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


# ------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current Windows user."""
    if sys.platform != "win32":
        raise EncryptionError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(b: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    windll = getattr(ctypes, "windll")  # noqa: B009 - Windows-only attribute
    crypt32 = windll.crypt32
    kernel32 = windll.kernel32
    src, entropy, out = _blob(data), _blob(b"nexus-osint-dek-v1"), _Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(out))
    if not ok:
        raise EncryptionError("Windows could not protect/unprotect the key (DPAPI)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------- enable / unlock
@dataclass
class EnableResult:
    recovery_code: str
    dpapi: bool


def enable(passphrase: str, *, use_dpapi: bool = False) -> EnableResult:
    """Turn encryption on: create and wrap a new data key, unlock it.

    Returns the recovery code; it is shown to the analyst exactly once.
    Existing plaintext files are converted by :func:`encrypt_existing_files`.
    """
    if not crypto_available():
        raise EncryptionError("the 'cryptography' package is not installed")
    if len(passphrase) < MIN_PASSPHRASE:
        raise EncryptionError(f"use a passphrase of at least {MIN_PASSPHRASE} characters")
    path = keystore_path()
    if load_keystore(path) is not None:
        raise EncryptionError("encryption is already enabled")
    dek = secrets.token_bytes(32)
    salt = secrets.token_bytes(16)
    code = _new_recovery_code()
    store: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": True,
        "kdf": {"name": "scrypt", **_SCRYPT, "salt": _b64(salt)},
        "passphrase": _wrap(_scrypt(passphrase, salt, _SCRYPT), dek, b"passphrase"),
        "recovery": _wrap(_recovery_kek(code), dek, b"recovery"),
        "dpapi": None,
    }
    dpapi_ok = False
    if use_dpapi:
        try:
            store["dpapi"] = _b64(_dpapi(dek, protect=True))
            dpapi_ok = True
        except EncryptionError:
            logger.warning("DPAPI unavailable; the app will ask for the passphrase on start")
    _write_keystore(store, path)
    _set_key(dek)
    logger.info("Encryption at rest enabled (auto-unlock: %s)", "on" if dpapi_ok else "off")
    return EnableResult(recovery_code=code, dpapi=dpapi_ok)


def unlock(secret: str) -> None:
    """Unlock with the passphrase or the recovery code."""
    store = load_keystore()
    if store is None:
        return
    secret = secret.strip()
    dek: bytes | None = None
    try:
        kdf = store["kdf"]
        dek = _unwrap(_scrypt(secret, _unb64(kdf["salt"]), kdf), store["passphrase"], b"passphrase")
    except WrongPassphraseError:
        if store.get("recovery"):
            dek = _unwrap(_recovery_kek(secret), store["recovery"], b"recovery")
    if dek is None:  # pragma: no cover - _unwrap raises instead
        raise WrongPassphraseError("wrong passphrase or recovery code")
    _set_key(dek)


def try_auto_unlock() -> bool:
    """Unlock without asking: DPAPI first, then the environment passphrase."""
    store = load_keystore()
    if store is None:
        return False
    if _current_key() is not None:
        return True
    if store.get("dpapi") and dpapi_available():
        try:
            _set_key(_dpapi(_unb64(store["dpapi"]), protect=False))
            return True
        except EncryptionError:
            logger.warning("Automatic unlock through Windows failed; passphrase needed")
    env = os.environ.get(_PASSPHRASE_ENV, "")
    if env:
        try:
            unlock(env)
            return True
        except WrongPassphraseError:
            logger.warning("%s is set but does not unlock the keystore", _PASSPHRASE_ENV)
    return False


def _file_master() -> bytes:
    dek = _current_key()
    if dek is None:
        raise LockedError("Nexus is locked: enter the passphrase to read encrypted files")
    return _hkdf(dek, b"nexus-files-v1")


# --------------------------------------------------------------- file format
def _aad(header: bytes, index: int, final: bool) -> bytes:
    return header + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def _nonce(index: int) -> bytes:
    return index.to_bytes(12, "big")


def encrypt_stream(src: BinaryIO, dst: BinaryIO) -> None:
    """Encrypt ``src`` into ``dst`` (NXE1, chunked, constant memory)."""
    salt = secrets.token_bytes(_SALT_LEN)
    header = MAGIC + bytes([_VERSION]) + salt
    aead = _aead(_hkdf(_file_master(), b"nexus-file-v1", salt))
    dst.write(header)
    index = 0
    chunk = src.read(_CHUNK)
    while True:
        nxt = src.read(_CHUNK) if len(chunk) == _CHUNK else b""
        final = not nxt
        dst.write(aead.encrypt(_nonce(index), chunk, _aad(header, index, final)))
        if final:
            return
        chunk, index = nxt, index + 1


def decrypt_stream(src: BinaryIO, dst: BinaryIO) -> None:
    """Decrypt an NXE1 stream; raises EncryptionError if it was tampered with."""
    from cryptography.exceptions import InvalidTag

    header = src.read(_HEADER_LEN)
    if len(header) != _HEADER_LEN or not header.startswith(MAGIC) or header[4] != _VERSION:
        raise EncryptionError("not a Nexus-encrypted file")
    aead = _aead(_hkdf(_file_master(), b"nexus-file-v1", header[5:]))
    index = 0
    block = src.read(_CHUNK + _TAG)
    while True:
        nxt = src.read(_CHUNK + _TAG) if len(block) == _CHUNK + _TAG else b""
        final = not nxt
        try:
            dst.write(aead.decrypt(_nonce(index), block, _aad(header, index, final)))
        except InvalidTag as exc:
            raise EncryptionError("encrypted file is damaged or was modified") from exc
        if final:
            return
        block, index = nxt, index + 1


def encrypt_bytes(data: bytes) -> bytes:
    import io

    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out)
    return out.getvalue()


def decrypt_bytes(blob: bytes) -> bytes:
    import io

    out = io.BytesIO()
    decrypt_stream(io.BytesIO(blob), out)
    return out.getvalue()


def is_encrypted_file(path: Path | str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_protected(path: Path | str) -> bytes:
    """Plaintext of a stored file, decrypting it when it is NXE1-encrypted."""
    with open(path, "rb") as fh:
        head = fh.read(len(MAGIC))
        fh.seek(0)
        if head != MAGIC:
            return fh.read()
        import io

        out = io.BytesIO()
        decrypt_stream(fh, out)
        return out.getvalue()


def write_protected(path: Path | str, data: bytes) -> None:
    """Atomically write ``data``, encrypted when encryption is enabled.

    The temporary file lives next to the target and already holds ciphertext,
    so a crash mid-write never leaves plaintext behind.
    """
    target = Path(path)
    encrypt = is_enabled()
    if encrypt and _current_key() is None:
        raise LockedError("Nexus is locked: enter the passphrase before storing files")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name[:16]}-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            if encrypt:
                import io

                encrypt_stream(io.BytesIO(data), fh)
            else:
                fh.write(data)
        try:
            os.replace(tmp, target)
        except PermissionError:
            # Windows: a concurrent writer of identical content holds the file.
            if not target.exists():
                raise
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ------------------------------------------------------------- conversion
def _plaintext_files() -> list[Path]:
    base = get_settings().data_path
    found: list[Path] = []
    for name in PROTECTED_DIRS:
        folder = base / name
        if not folder.is_dir():
            continue
        with os.scandir(folder) as entries:
            for entry in entries:
                if (entry.is_file(follow_symlinks=False) and not entry.name.startswith(".")
                        and not is_encrypted_file(entry.path)):
                    found.append(Path(entry.path))
    return found


def plaintext_file_count() -> int:
    return len(_plaintext_files())


def encrypt_existing_files() -> dict[str, int]:
    """Encrypt every plaintext upload/evidence file in place (idempotent).

    Each file: encrypt into a temp file, decrypt it back and compare hashes,
    then atomically replace. A file another process holds open is skipped and
    retried on the next start.
    """
    done = skipped = 0
    if _current_key() is None:
        return {"encrypted": 0, "skipped": 0}
    for path in _plaintext_files():
        try:
            plain = path.read_bytes()
            sealed = encrypt_bytes(plain)
            if hashlib.sha256(decrypt_bytes(sealed)).digest() != hashlib.sha256(plain).digest():
                raise EncryptionError("verification failed")
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".enc-", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(sealed)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            done += 1
        except (OSError, EncryptionError):
            logger.warning("Could not encrypt %s yet; will retry on next start", path.name)
            skipped += 1
    if done or skipped:
        logger.info("Encryption at rest: %d file(s) encrypted, %d skipped", done, skipped)
    return {"encrypted": done, "skipped": skipped}


def start_background_conversion() -> None:
    """Convert leftover plaintext files off the request path."""
    if _current_key() is None:
        return
    threading.Thread(target=encrypt_existing_files, daemon=True, name="atrest-convert").start()


# ------------------------------------------------------- unlock rate limit
class UnlockThrottle:
    """Exponential back-off after repeated wrong passphrases (per process)."""

    def __init__(self, free: int = 5, cap: float = 300.0) -> None:
        self.free, self.cap = free, cap
        self.failures = 0
        self.until = 0.0

    def wait_seconds(self) -> int:
        return max(0, int(self.until - time.monotonic() + 0.999))

    def failed(self) -> None:
        self.failures += 1
        if self.failures >= self.free:
            delay = min(self.cap, 2.0 ** (self.failures - self.free + 1))
            self.until = time.monotonic() + delay

    def succeeded(self) -> None:
        self.failures, self.until = 0, 0.0


THROTTLE = UnlockThrottle()


def describe() -> dict[str, Any]:
    """Status for the Settings panel (never contains key material)."""
    store = load_keystore()
    return {
        "available": crypto_available(),
        "status": status(),
        "dpapi_available": dpapi_available(),
        "auto_unlock": bool(store and store.get("dpapi")),
        "created_at": (store or {}).get("created_at"),
        "plaintext_files": plaintext_file_count() if store else 0,
        "min_passphrase": MIN_PASSPHRASE,
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m nexus.security.atrest status|enable|encrypt-files``."""
    import argparse
    import getpass

    parser = argparse.ArgumentParser(prog="python -m nexus.security.atrest")
    parser.add_argument("command", choices=["status", "enable", "encrypt-files"])
    parser.add_argument("--auto-unlock", action="store_true",
                        help="also protect the key with Windows DPAPI (current user)")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(describe(), indent=2))
        return 0
    if args.command == "enable":
        first = getpass.getpass("New passphrase: ")
        if first != getpass.getpass("Repeat passphrase: "):
            print("Passphrases do not match.")
            return 1
        result = enable(first, use_dpapi=args.auto_unlock)
        print("Encryption enabled. Recovery code (shown once, keep it safe):")
        print(result.recovery_code)
    elif not try_auto_unlock():
        unlock(getpass.getpass("Passphrase: "))
    print(json.dumps(encrypt_existing_files()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
