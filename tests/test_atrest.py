"""Encryption at rest for stored files (nexus/security/atrest.py)."""

from __future__ import annotations

import hashlib
import io
import json
import sys

import pytest

pytest.importorskip("cryptography")

from fastapi.testclient import TestClient

from nexus.security import atrest

PASS = "correct horse battery staple"
_PROD_SCRYPT = dict(atrest._SCRYPT)  # captured before the fixture patches it


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    # Keep scrypt fast in tests; the production cost is asserted separately.
    monkeypatch.setattr(atrest, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    atrest.THROTTLE.succeeded()
    yield
    atrest.lock()


def test_production_kdf_cost():
    assert _PROD_SCRYPT["n"] >= 2**17 and atrest._SCRYPT_MAXMEM >= 128 * 1024 * 1024


def test_off_by_default(tmp_path):
    assert atrest.status() == "off"
    assert not atrest.is_enabled()
    target = tmp_path / "plain.bin"
    atrest.write_protected(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_enable_wraps_key_and_never_stores_it(tmp_path):
    res = atrest.enable(PASS)
    assert atrest.status() == "unlocked"
    raw = (tmp_path / "keystore.json").read_text()
    assert (tmp_path / "keystore.json.bak").is_file()
    dek = atrest._current_key()
    assert dek is not None
    for enc in (dek.hex(), atrest._b64(dek)):
        assert enc not in raw
    assert PASS not in raw and res.recovery_code not in raw


def test_short_passphrase_and_double_enable_refused():
    with pytest.raises(atrest.EncryptionError):
        atrest.enable("short")
    atrest.enable(PASS)
    with pytest.raises(atrest.EncryptionError):
        atrest.enable(PASS)


@pytest.mark.parametrize("size", [0, 1, 1024 * 1024, 1024 * 1024 + 7, 3 * 1024 * 1024])
def test_roundtrip_sizes(size):
    atrest.enable(PASS)
    data = bytes(i % 251 for i in range(size))
    blob = atrest.encrypt_bytes(data)
    assert blob.startswith(atrest.MAGIC)
    if size >= 64:
        assert data[:64] not in blob[21:]
    assert atrest.decrypt_bytes(blob) == data


def test_tamper_truncate_and_reorder_detected():
    atrest.enable(PASS)
    data = b"x" * (2 * 1024 * 1024 + 5)
    blob = bytearray(atrest.encrypt_bytes(data))
    flipped = bytes(blob[:100]) + bytes([blob[100] ^ 1]) + bytes(blob[101:])
    with pytest.raises(atrest.EncryptionError):
        atrest.decrypt_bytes(flipped)
    chunk = 1024 * 1024 + 16
    truncated = bytes(blob[: 21 + 2 * chunk])  # drop the final chunk
    with pytest.raises(atrest.EncryptionError):
        atrest.decrypt_bytes(truncated)
    c0, c1 = blob[21:21 + chunk], blob[21 + chunk:21 + 2 * chunk]
    swapped = bytes(blob[:21]) + bytes(c1) + bytes(c0) + bytes(blob[21 + 2 * chunk:])
    with pytest.raises(atrest.EncryptionError):
        atrest.decrypt_bytes(swapped)


def test_lock_unlock_with_passphrase_and_recovery_code(tmp_path):
    res = atrest.enable(PASS)
    f = tmp_path / "evidence.png"
    atrest.write_protected(f, b"PNGDATA")
    assert atrest.is_encrypted_file(f)
    atrest.lock()
    assert atrest.status() == "locked"
    with pytest.raises(atrest.LockedError):
        atrest.read_protected(f)
    with pytest.raises(atrest.LockedError):
        atrest.write_protected(tmp_path / "new.bin", b"x")
    with pytest.raises(atrest.WrongPassphraseError):
        atrest.unlock("not the passphrase " + chr(0x5D0))
    atrest.unlock(PASS)
    assert atrest.read_protected(f) == b"PNGDATA"
    atrest.lock()
    atrest.unlock(res.recovery_code.lower())
    assert atrest.read_protected(f) == b"PNGDATA"


def test_keystore_backup_used_when_main_copy_damaged(tmp_path):
    atrest.enable(PASS)
    (tmp_path / "keystore.json").write_text("{not json", encoding="utf-8")
    atrest.lock()
    atrest.unlock(PASS)
    assert atrest.status() == "unlocked"


def test_env_passphrase_auto_unlock(monkeypatch):
    atrest.enable(PASS)
    atrest.lock()
    monkeypatch.setenv("NEXUS_ENCRYPTION_PASSPHRASE", PASS)
    assert atrest.try_auto_unlock()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_auto_unlock():
    res = atrest.enable(PASS, use_dpapi=True)
    assert res.dpapi
    atrest.lock()
    assert atrest.try_auto_unlock()


def test_existing_files_encrypted_in_place(tmp_path):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "evidence").mkdir()
    a = tmp_path / "uploads" / "abc.bin"
    b = tmp_path / "evidence" / "item1.png"
    a.write_bytes(b"doc")
    b.write_bytes(b"img")
    atrest.enable(PASS)
    assert atrest.plaintext_file_count() == 2
    assert atrest.encrypt_existing_files() == {"encrypted": 2, "skipped": 0}
    assert atrest.is_encrypted_file(a) and atrest.is_encrypted_file(b)
    assert atrest.read_protected(a) == b"doc"
    assert atrest.encrypt_existing_files() == {"encrypted": 0, "skipped": 0}
    assert not list((tmp_path / "uploads").glob(".*part"))


def test_upload_original_stored_encrypted(temp_db, tmp_path):
    from nexus.config import get_settings
    from nexus.ingest.upload import _store_original

    atrest.enable(PASS)
    data = b"secret company memo"
    sha = hashlib.sha256(data).hexdigest()
    rel = _store_original(data, sha, get_settings())
    stored = tmp_path / rel
    assert data not in stored.read_bytes()
    assert atrest.read_protected(stored) == data


def test_web_locked_mode_and_unlock_flow(temp_db, tmp_path):
    from nexus.web.app import app

    (tmp_path / "evidence").mkdir(exist_ok=True)
    atrest.enable(PASS)
    atrest.write_protected(tmp_path / "evidence" / "item1_x.png", b"\x89PNG fake")
    client = TestClient(app, base_url="http://127.0.0.1")  # no lifespan: no daemon threads
    r = client.get("/data/evidence/item1_x.png")
    assert r.status_code == 200 and r.content == b"\x89PNG fake"
    atrest.lock()
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/unlock"
    assert client.post("/scan/start").status_code == 423
    assert client.get("/health").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    r = client.post("/unlock", data={"passphrase": "wrong one"})
    assert r.status_code == 401
    r = client.post("/unlock", data={"passphrase": PASS},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/settings").status_code == 200


def test_unlock_throttled_after_repeated_failures(temp_db):
    from nexus.web.app import app

    atrest.enable(PASS)
    atrest.lock()
    client = TestClient(app, base_url="http://127.0.0.1")  # no lifespan: no daemon threads
    codes = [client.post("/unlock", data={"passphrase": f"bad {i}"}).status_code
             for i in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_settings_panel_enable_shows_recovery_code_once(temp_db, tmp_path):
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")  # no lifespan: no daemon threads
    r = client.post("/settings/encryption",
                    data={"passphrase": PASS, "confirm": PASS + "x", "understand": "1"})
    assert "do not match" in r.text and atrest.status() == "off"
    r = client.post("/settings/encryption", data={"passphrase": PASS, "confirm": PASS})
    assert atrest.status() == "off"
    r = client.post("/settings/encryption",
                    data={"passphrase": PASS, "confirm": PASS, "understand": "1"})
    assert "recovery code" in r.text.lower() and atrest.status() == "unlocked"
    store = json.loads((tmp_path / "keystore.json").read_text())
    assert store["dpapi"] is None
    code = r.text.split('break-all">', 1)[1].split("<", 1)[0].strip()
    assert len(code) > 20
    assert code not in client.get("/settings/encryption").text


def test_evidence_report_embeds_encrypted_image_inline(tmp_path):
    from nexus.reporting import _evidence_src

    plain = tmp_path / "p.png"
    plain.write_bytes(b"img")
    assert _evidence_src(plain).startswith("file:")
    atrest.enable(PASS)
    enc = tmp_path / "e.png"
    atrest.write_protected(enc, b"img")
    assert _evidence_src(enc).startswith("data:image/png;base64,")
    atrest.lock()
    assert _evidence_src(enc) == ""


def test_ocr_reads_from_bytes_without_temp_file(monkeypatch):
    from nexus import ocr

    pytest.importorskip("PIL")
    seen = {}

    class _Fake:
        @staticmethod
        def image_to_string(img):
            seen["size"] = img.size
            return "Hello OCR world text"

    monkeypatch.setitem(sys.modules, "pytesseract", _Fake)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 3)).save(buf, format="PNG")
    assert ocr.extract_text_from_bytes(buf.getvalue()) == "Hello OCR world text"
    assert seen["size"] == (4, 3)
