"""Backup & restore (nexus/backup.py, Settings > Backup & restore)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from nexus import backup
from nexus.security import atrest

PASS = "a long backup passphrase"


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    monkeypatch.setattr(atrest, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    yield
    atrest.lock()


@pytest.fixture
def populated(temp_db, tmp_path):
    with temp_db() as conn:
        conn.execute("INSERT INTO cases (name) VALUES ('Case Alpha')")
    for folder, name, data in (("uploads", "abc.bin", b"doc"), ("evidence", "item1_1.png", b"png"),
                               ("reports", "case-1.html", b"<p>r</p>")):
        (tmp_path / folder).mkdir(exist_ok=True)
        (tmp_path / folder / name).write_bytes(data)
    (tmp_path / "isolated.env").write_text(
        "AI_PROVIDER=gemini\nGEMINI_API_KEY=supersecretvalue123\nSMTP_PASSWORD=hunter22hunter\n"
        "RSS_FEEDS=https://feeds.example.org/a.xml\nCUSTOM_SOURCE_4_KEY=abcdef123456\n"
        "DATA_DIR=/elsewhere\n", encoding="utf-8")
    return tmp_path


def _names(path):
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist()), zf.read("settings.env").decode()


def test_backup_contains_data_but_no_secrets(populated):
    res = backup.create_backup()
    names, settings = _names(res.path)
    assert {"manifest.json", "nexus.db", "settings.env", "uploads/abc.bin",
            "evidence/item1_1.png", "reports/case-1.html"} <= names
    assert "AI_PROVIDER=gemini" in settings and "RSS_FEEDS=" in settings
    for secret in ("supersecretvalue123", "hunter22hunter", "abcdef123456", "DATA_DIR"):
        assert secret not in settings
    assert b"supersecretvalue123" not in res.path.read_bytes()


def test_roundtrip_restore_replaces_state_and_keeps_pre_restore(populated, temp_db):
    res = backup.create_backup()
    with temp_db() as conn:
        conn.execute("INSERT INTO cases (name) VALUES ('Case Beta')")
    (populated / "uploads" / "new.bin").write_bytes(b"later")
    (populated / "uploads" / "abc.bin").write_bytes(b"changed")
    report = backup.restore_backup(res.path)
    assert report.ok and report.pre_restore_backup.startswith("nexus-pre-restore-")
    with temp_db() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM cases")]
    assert names == ["Case Alpha"]
    assert (populated / "uploads" / "abc.bin").read_bytes() == b"doc"
    assert not (populated / "uploads" / "new.bin").exists()
    pre = backup.backup_path(report.pre_restore_backup)
    assert pre is not None and "uploads/new.bin" in _names(pre)[0]


def test_restore_settings_never_writes_secrets(populated):
    res = backup.create_backup()
    env = populated / "isolated.env"
    env.write_text("AI_PROVIDER=off\nGEMINI_API_KEY=keepme12345678\n", encoding="utf-8")
    backup.restore_backup(res.path)
    text = env.read_text(encoding="utf-8")
    assert "AI_PROVIDER=gemini" in text and "GEMINI_API_KEY=keepme12345678" in text
    assert "DATA_DIR" not in text and "SMTP_PASSWORD" not in text


def test_encrypted_backup_needs_passphrase(populated):
    res = backup.create_backup(PASS)
    assert res.path.suffix == ".nxb" and backup.is_encrypted_backup(res.path)
    assert b"PK" not in res.path.read_bytes()[:64]
    with pytest.raises(backup.BackupError, match="encrypted"):
        backup.restore_backup(res.path)
    with pytest.raises(backup.BackupError, match="wrong passphrase"):
        backup.restore_backup(res.path, "not the right passphrase")
    assert backup.restore_backup(res.path, PASS).ok


def _forge(tmp_path, members: dict[str, bytes], manifest_extra=None, files=None):
    import hashlib

    manifest = {"format": "nexus-backup", "format_version": 1, "app_version": "1.0.0",
                "files": files if files is not None else {
                    n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
                    for n, d in members.items()}}
    manifest.update(manifest_extra or {})
    p = tmp_path / "backups" / "nexus-forged-1.zip"
    p.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for n, d in members.items():
            zf.writestr(n, d)
    return p


@pytest.mark.parametrize("evil", ["../escape.txt", "uploads/../../x", "/abs/path",
                                  "uploads/sub/../../x", "C:/win.txt", "other/file.bin",
                                  "uploads\\..\\x"])
def test_zip_slip_and_unknown_paths_rejected(populated, evil):
    p = _forge(populated, {"nexus.db": b"x", evil: b"boom"})
    with pytest.raises(backup.BackupError):
        backup.restore_backup(p)
    assert not (populated.parent / "escape.txt").exists()


def test_checksum_mismatch_and_newer_version_rejected(populated):
    good = backup.create_backup()
    with zipfile.ZipFile(good.path) as zf:
        members = {n: zf.read(n) for n in zf.namelist() if n != "manifest.json"}
        files = json.loads(zf.read("manifest.json"))["files"]
    members["uploads/abc.bin"] = b"tampered"
    with pytest.raises(backup.BackupError, match=r"checksum|larger"):
        backup.restore_backup(_forge(populated, members, files=files))
    members["uploads/abc.bin"] = b"doc"
    with pytest.raises(backup.BackupError, match="update Nexus"):
        backup.restore_backup(_forge(populated, members, {"app_version": "99.0.0"}, files=files))


def test_not_a_nexus_db_rejected(populated):
    p = _forge(populated, {"nexus.db": b"not sqlite at all"})
    with pytest.raises(backup.BackupError, match="database"):
        backup.restore_backup(p)


def test_failed_restore_changes_nothing(populated, temp_db):
    p = _forge(populated, {"nexus.db": b"junk", "uploads/abc.bin": b"x"})
    with pytest.raises(backup.BackupError):
        backup.restore_backup(p)
    assert (populated / "uploads" / "abc.bin").read_bytes() == b"doc"
    assert not [b for b in backup.list_backups() if "pre-restore" in b["name"]]


def test_keystore_travels_with_encrypted_files(populated):
    atrest.enable("a long at-rest passphrase")
    atrest.encrypt_existing_files()
    res = backup.create_backup()
    assert "keystore.json" in _names(res.path)[0]
    report = backup.restore_backup(res.path)
    assert report.ok


def test_web_panel_create_download_restore(populated):
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/settings/backup", data={"passphrase": "", "confirm": ""})
    assert "Backup saved" in r.text
    name = backup.list_backups()[0]["name"]
    dl = client.get(f"/backups/{name}")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    assert client.get("/backups/..%2Fnexus.db").status_code == 404
    r = client.post("/settings/backup/restore", data={"existing": name})
    assert "Tick the box" in r.text
    r = client.post("/settings/backup/restore", data={"understand": "1", "restore_settings": "1"},
                    files={"archive": ("b.zip", io.BytesIO(dl.content), "application/zip")})
    assert "Backup restored" in r.text
    r = client.post("/settings/backup/restore", data={"understand": "1"},
                    files={"archive": ("x.zip", io.BytesIO(b"nope"), "application/zip")})
    assert "Nothing was changed" in r.text
