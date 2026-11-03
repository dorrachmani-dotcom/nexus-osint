"""Encryption at rest: data-loss and race guards found in review."""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("cryptography")

from nexus.security import atrest

PASS = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    monkeypatch.setattr(atrest, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    atrest.THROTTLE.succeeded()
    yield
    atrest.lock()


def test_off_mode_file_starting_with_magic_is_plain_data(tmp_path):
    f = tmp_path / "looks.bin"
    f.write_bytes(atrest.MAGIC + b" not really encrypted")
    assert not atrest.needs_decrypt(f)
    assert atrest.read_protected(f) == atrest.MAGIC + b" not really encrypted"


def test_enable_refused_when_keystore_lost_but_encrypted_files_exist(tmp_path):
    (tmp_path / "uploads").mkdir()
    atrest.enable(PASS)
    atrest.write_protected(tmp_path / "uploads" / "a.bin", b"doc")
    (tmp_path / "keystore.json").unlink()
    (tmp_path / "keystore.json.bak").unlink()
    atrest.lock()
    assert atrest.status() == "off"
    assert atrest.describe()["orphaned_files"] == 1
    with pytest.raises(atrest.EncryptionError, match="keystore"):
        atrest.enable(PASS)


def test_concurrent_enable_creates_one_key():
    results: list = []
    errors: list = []

    def go():
        try:
            results.append(atrest.enable(PASS))
        except atrest.EncryptionError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(errors) == 2
    atrest.lock()
    atrest.unlock(results[0].recovery_code)


def test_exclusive_keystore_create_refuses_existing(tmp_path):
    (tmp_path / "keystore.json").write_text("{}", encoding="utf-8")
    with pytest.raises(atrest.EncryptionError):
        atrest._write_keystore({"version": 1}, tmp_path / "keystore.json", exclusive=True)


def test_conversion_never_double_encrypts(tmp_path):
    (tmp_path / "evidence").mkdir()
    f = tmp_path / "evidence" / "x.png"
    f.write_bytes(b"img")
    atrest.enable(PASS)
    assert atrest._convert_lock.acquire(blocking=False)
    try:  # another converter is running: this one backs off
        assert atrest.encrypt_existing_files() == {"encrypted": 0, "skipped": 0}
    finally:
        atrest._convert_lock.release()
    assert atrest.encrypt_existing_files()["encrypted"] == 1
    assert atrest.encrypt_existing_files()["encrypted"] == 0
    assert atrest.read_protected(f) == b"img"


def test_passphrase_from_dotenv_file(tmp_path):
    from nexus.config import get_settings

    atrest.enable(PASS)
    atrest.lock()
    (tmp_path / "isolated.env").write_text(f"NEXUS_ENCRYPTION_PASSPHRASE={PASS}\n", encoding="utf-8")
    get_settings.cache_clear()
    assert atrest.try_auto_unlock()


def test_passphrase_is_redacted_from_logs(tmp_path):
    from nexus.config import get_settings
    from nexus.logging_safe import _secret_values

    (tmp_path / "isolated.env").write_text(f"NEXUS_ENCRYPTION_PASSPHRASE={PASS}\n", encoding="utf-8")
    get_settings.cache_clear()
    assert PASS in _secret_values()
    assert PASS not in repr(get_settings())


def test_non_content_addressed_write_conflict_is_not_swallowed(tmp_path, monkeypatch):
    target = tmp_path / "evidence.png"
    target.write_bytes(b"old")

    def boom(*a, **k):
        raise PermissionError("held")

    monkeypatch.setattr(atrest.os, "replace", boom)
    with pytest.raises(PermissionError):
        atrest.write_protected(target, b"new")
    atrest.write_protected(target, b"new", content_addressed=True)
