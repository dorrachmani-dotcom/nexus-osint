"""Google Drive connector against a fake Drive API. Names are fictional."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from cloud_fake import connect, documents, install, item_text, reload_source

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nexus.sources import gdrive as G

PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
EMAIL = "reader@example-project.iam.example"
KEY_JSON = json.dumps({"type": "service_account", "client_email": EMAIL, "private_key": PEM,
                       "private_key_id": "drop-me", "token_uri": "https://evil.example/token"})
ACCESS = "ya29.ACCESS-SECRET"


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _file(fid: str, name: str, mime: str, **extra) -> dict:
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": "2026-10-01T10:00:00.000Z",
            "webViewLink": f"https://drive.example/{fid}", **extra}


class FakeDrive:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tree = {
            "root1": [_file("sub1", "Sub", G.FOLDER),
                      _file("doc1", "Plan", "application/vnd.google-apps.document"),
                      _file("sheet1", "Budget", "application/vnd.google-apps.spreadsheet"),
                      _file("txt1", "notes.txt", "text/plain", md5Checksum="aaa", size="30"),
                      _file("form1", "Survey", "application/vnd.google-apps.form"),
                      _file("vid1", "talk.mp4", "video/mp4", size="9")],
            "sub1": [_file("txt2", "deep.txt", "text/plain", md5Checksum="bbb", size="20"),
                     _file("root1", "Loop back", G.FOLDER)],
        }
        self.bodies = {"doc1": b"Plan text body", "sheet1": b"item,cost\nrope,12\n",
                       "txt1": b"Plain notes from the field", "txt2": b"Deep folder findings"}
        self.tokens = 0
        self.refuse_token = False
        self.page = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if request.url.host == "oauth2.googleapis.com":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {ACCESS}":
            return httpx.Response(401, json={})
        if path == "/drive/v3/files":
            query = params["q"]
            if query.startswith("mimeType="):
                return httpx.Response(200, json={"files": [{"id": "root1", "name": "Field work"}]})
            parent = query.split("'")[1]
            if parent not in self.tree:
                return httpx.Response(404, json={})
            start = int(params.get("pageToken") or 0)
            part = self.tree[parent][start:start + self.page]
            more = start + self.page < len(self.tree[parent])
            return httpx.Response(200, json={"files": part,
                                             **({"nextPageToken": str(start + self.page)} if more else {})})
        fid = path.split("/")[4]
        if path.endswith("/export") or params.get("alt") == "media":
            return httpx.Response(200, content=self.bodies[fid])
        return httpx.Response(404, json={})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens += 1
        if self.refuse_token:
            return httpx.Response(400, json={"error": "invalid_grant"})
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        head, body, sig = form["assertion"][0].split(".")
        PRIVATE.public_key().verify(_unb64(sig), f"{head}.{body}".encode(),
                                    padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(_unb64(body))
        assert claims["iss"] == EMAIL and claims["aud"] == G.TOKEN_URL
        assert claims["scope"] == G.READ_ONLY_SCOPE
        assert claims["exp"] - claims["iat"] == 3600
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})

    def api_calls(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.host != "oauth2.googleapis.com"]


@pytest.fixture
def fake(monkeypatch) -> FakeDrive:
    server = FakeDrive()
    install(monkeypatch, server)
    return server


def _source(temp_db, scope="root1"):
    return connect(temp_db, "gdrive", G.GoogleDriveSource.normalize_key(KEY_JSON), scope=scope)


def test_walks_folders_and_reads_files(temp_db, fake):
    res = _source(temp_db).ingest()
    assert res["status"].startswith("4 checked, 4 new"), res
    assert set(documents(temp_db, "gdrive")) == {"Plan", "Budget", "notes.txt", "deep.txt"}
    text = item_text(temp_db)
    for expected in ("Plan text body", "rope", "Plain notes from the field", "Deep folder findings"):
        assert expected in text


def test_token_is_requested_once_and_read_only(temp_db, fake):
    _source(temp_db).ingest()
    assert fake.tokens == 1
    assert {r.method for r in fake.requests if r.url.host != "oauth2.googleapis.com"} == {"GET"}


def test_pagination(temp_db, fake):
    fake.page = 2
    assert _source(temp_db).ingest()["fetched"] == 4


def test_unchanged_files_are_not_downloaded_again(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.requests.clear()
    assert "0 new, 0 updated" in reload_source(temp_db, src).ingest()["status"]
    assert all(p == "/drive/v3/files" for p in fake.api_calls())


def test_changed_file_and_deleted_file(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.tree["root1"][3]["md5Checksum"] = "ccc"
    fake.bodies["txt1"] = b"Rewritten field notes"
    del fake.tree["sub1"][0]
    res = reload_source(temp_db, src).ingest()
    assert res["changed"] == 1 and res["removed"] == 1
    assert "deep.txt" not in documents(temp_db, "gdrive")
    assert "Rewritten field notes" in item_text(temp_db)


def test_lost_folder_keeps_what_was_read(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.tree.clear()
    assert reload_source(temp_db, src).ingest()["status"] == "error: HTTP 404"
    assert len(documents(temp_db, "gdrive")) == 4


def test_too_large_file_is_skipped(temp_db, fake):
    fake.tree["root1"][3]["size"] = str(10 ** 12)
    res = _source(temp_db).ingest()
    assert "1 could not be read" in res["status"]
    assert "notes.txt" not in documents(temp_db, "gdrive")


def test_odd_scope_ids_never_reach_the_query(temp_db, fake):
    src = _source(temp_db, scope="root1,bad'id")
    src.ingest()
    assert not any("bad" in str(r.url) for r in fake.requests)


def test_normalize_key_keeps_only_what_is_needed():
    stored = G.GoogleDriveSource.normalize_key(KEY_JSON)
    assert set(json.loads(base64.b64decode(stored))) == {"client_email", "private_key"}
    assert "\n" not in stored
    for bad in ("nonsense", "{}", '{"client_email": "a", "private_key": "b"}', "[1]"):
        with pytest.raises(ValueError):
            G.GoogleDriveSource.normalize_key(bad)


def test_key_survives_the_env_file(temp_db, fake):
    src = _source(temp_db)
    assert src._key()["private_key"] == PEM


def test_refused_key_is_reported(temp_db, fake, caplog):
    fake.refuse_token = True
    src = _source(temp_db)
    assert src.ingest()["status"] == "error: Google refused this key"
    assert src.test_connection() == {"ok": False, "error": "Google refused this key"}
    assert PEM[:60] not in caplog.text and ACCESS not in caplog.text


def test_damaged_key_is_reported(temp_db, fake):
    src = connect(temp_db, "gdrive", "not-base64-json", scope="root1")
    assert src.ingest()["status"].startswith("error: the stored key is not readable")


def test_test_connection_offers_folders(temp_db, fake):
    result = _source(temp_db).test_connection()
    assert result == {"ok": True, "account": EMAIL, "note": "",
                      "items": [{"key": "root1", "name": "Field work"}]}


def test_ui_validates_and_stores_the_key(temp_db, fake):
    from fastapi.testclient import TestClient

    from nexus.storage.cloud_sources import list_cloud_sources
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    bad = client.post("/sources/cloud", data={"kind": "gdrive", "token": "oops"})
    assert "not a service account JSON key" in bad.text
    with temp_db() as conn:
        assert list_cloud_sources(conn) == []
    good = client.post("/sources/cloud", data={"kind": "gdrive", "token": KEY_JSON})
    assert "token set" in good.text and PEM[:60] not in good.text
    with temp_db() as conn:
        assert "PRIVATE" not in json.dumps(list_cloud_sources(conn))
