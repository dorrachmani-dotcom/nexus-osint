"""SharePoint / OneDrive connector against a fake Microsoft Graph. Names are fictional."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from cloud_fake import connect, documents, install, item_text, reload_source
from nexus.sources.sharepoint import parse_account

SECRET = "client-SECRET-123"
ACCESS = "eyJ.ACCESS-SECRET"
ACCOUNT = "contoso-example.onmicrosoft.com, 1111-2222, robin@example.org"
DRIVE = "b!abc-DEF_1"


def _file(fid: str, name: str, tag: str, mime: str = "text/plain", size: int = 30) -> dict:
    return {"id": fid, "name": name, "cTag": tag, "size": size, "file": {"mimeType": mime},
            "lastModifiedDateTime": "2026-10-01T10:00:00Z", "webUrl": f"https://t.example/{fid}"}


class FakeGraph:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tree = {
            "root": [{"id": "f1", "name": "Reports", "folder": {"childCount": 1}},
                     _file("a", "notes.txt", "t1"),
                     _file("v", "talk.mp4", "t2", "video/mp4"),
                     {"id": "n", "name": "Notebook", "package": {"type": "oneNote"}}],
            "f1": [_file("b", "deep.txt", "t3")],
        }
        self.bodies = {"a": b"Plain notes from the field", "b": b"Deep folder findings"}
        self.refuse_token = False
        self.tokens = 0
        self.page = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path, params = request.url.host, request.url.path, request.url.params
        if host == "login.microsoftonline.com":
            return self._token(request)
        if host == "tenant.sharepoint.com":
            return httpx.Response(200, content=self.bodies[path.rsplit("/", 1)[-1]])
        if request.headers.get("authorization") != f"Bearer {ACCESS}":
            return httpx.Response(401, json={})
        if path == "/v1.0/sites":
            return httpx.Response(200, json={"value": [{"id": "host,1,2", "displayName": "Team"}]})
        if path.endswith("/drives"):
            assert "host%2C1%2C2" in str(request.url)
            return httpx.Response(200, json={"value": [{"id": DRIVE, "name": "Documents"}]})
        if path.startswith("/v1.0/users/"):
            if "robin" in path:
                return httpx.Response(200, json={"id": "b!one-drive"})
            return httpx.Response(404, json={})
        if path.endswith("/children"):
            return self._children(request)
        if path.endswith("/content"):
            item = path.split("/")[-2]
            return httpx.Response(302, headers={"location": f"https://tenant.sharepoint.com/dl/{item}"})
        return httpx.Response(404, json={"params": str(params)})

    def _children(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        folder, skip = parts[-2], int(request.url.params.get("$skip") or 0)
        if folder not in self.tree:
            return httpx.Response(404, json={})
        rows = self.tree[folder][skip:skip + self.page]
        body: dict = {"value": rows}
        if skip + self.page < len(self.tree[folder]):
            body["@odata.nextLink"] = f"{str(request.url).split('?')[0]}?$skip={skip + self.page}"
        return httpx.Response(200, json=body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens += 1
        if self.refuse_token:
            return httpx.Response(401, json={"error": "invalid_client"})
        assert request.url.path == "/contoso-example.onmicrosoft.com/oauth2/v2.0/token"
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == [SECRET] and form["client_id"] == ["1111-2222"]
        assert form["grant_type"] == ["client_credentials"]
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})


@pytest.fixture
def fake(monkeypatch) -> FakeGraph:
    server = FakeGraph()
    install(monkeypatch, server)
    return server


def _source(temp_db, scope=DRIVE):
    return connect(temp_db, "sharepoint", SECRET, account=ACCOUNT, scope=scope)


def test_walks_a_library_and_reads_files(temp_db, fake):
    res = _source(temp_db).ingest()
    assert res["status"].startswith("2 checked, 2 new"), res
    assert set(documents(temp_db, "sharepoint")) == {"notes.txt", "deep.txt"}
    text = item_text(temp_db)
    assert "Plain notes from the field" in text and "Deep folder findings" in text
    assert fake.tokens == 1


def test_token_is_not_sent_to_the_download_host(temp_db, fake):
    _source(temp_db).ingest()
    downloads = [r for r in fake.requests if r.url.host == "tenant.sharepoint.com"]
    assert len(downloads) == 2
    assert not any("authorization" in r.headers for r in downloads)


def test_read_only_after_sign_in(temp_db, fake):
    _source(temp_db).ingest()
    assert {r.method for r in fake.requests if r.url.host != "login.microsoftonline.com"} == {"GET"}


def test_pagination_follows_next_links(temp_db, fake):
    fake.page = 1
    assert _source(temp_db).ingest()["fetched"] == 2


def test_unchanged_files_are_not_downloaded_again(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.requests.clear()
    assert "0 new, 0 updated" in reload_source(temp_db, src).ingest()["status"]
    assert not any(r.url.path.endswith("/content") for r in fake.requests)


def test_changed_and_deleted_files(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.tree["root"][1]["cTag"] = "t9"
    fake.bodies["a"] = b"Rewritten field notes"
    fake.tree["f1"].clear()
    res = reload_source(temp_db, src).ingest()
    assert res["changed"] == 1 and res["removed"] == 1
    assert set(documents(temp_db, "sharepoint")) == {"notes.txt"}
    assert "Rewritten field notes" in item_text(temp_db)


def test_lost_library_keeps_what_was_read(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.tree.clear()
    assert reload_source(temp_db, src).ingest()["status"] == "error: HTTP 404"
    assert len(documents(temp_db, "sharepoint")) == 2


def test_too_large_file_is_skipped(temp_db, fake):
    fake.tree["root"][1]["size"] = 10 ** 12
    assert "1 could not be read" in _source(temp_db).ingest()["status"]


def test_odd_scope_ids_never_reach_the_url(temp_db, fake):
    _source(temp_db, scope=f"{DRIVE},../../me").ingest()
    assert not any("me" in r.url.path.rsplit("/", 1)[-1] for r in fake.requests)
    assert not any("..%2F" in str(r.url) or "/../" in str(r.url) for r in fake.requests)


def test_refused_secret_is_reported(temp_db, fake, caplog):
    fake.refuse_token = True
    src = _source(temp_db)
    assert src.ingest()["status"] == "error: HTTP 401: the token was refused"
    assert not src.test_connection()["ok"]
    assert SECRET not in caplog.text


def test_test_connection_offers_libraries_and_onedrives(temp_db, fake):
    result = _source(temp_db).test_connection()
    assert result["ok"] and result["items"] == [
        {"key": DRIVE, "name": "Team / Documents"},
        {"key": "b!one-drive", "name": "OneDrive: robin@example.org"}]


def test_test_connection_names_a_missing_onedrive(temp_db, fake):
    account = "contoso-example.onmicrosoft.com, 1111-2222, sam@example.org"
    result = connect(temp_db, "sharepoint", SECRET, account=account).test_connection()
    assert result["ok"] and result["note"] == "No OneDrive found for: sam@example.org."
    assert result["items"] == [{"key": DRIVE, "name": "Team / Documents"}]


def test_parse_account():
    assert parse_account("t.example , c-1") == ("t.example", "c-1", [])
    assert parse_account(ACCOUNT)[2] == ["robin@example.org"]
    for bad in ("", "only-tenant", "t/../x, c", "t, c, not-a-user", "t, c, a/b@x"):
        with pytest.raises(ValueError):
            parse_account(bad)


def test_ui_validates_the_account(temp_db, fake):
    from fastapi.testclient import TestClient

    from nexus.storage.cloud_sources import list_cloud_sources
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    assert "Tenant ID, client ID" in client.get("/sources/cloud").text
    data = {"kind": "sharepoint", "token": SECRET, "account": "just-one"}
    assert "separated by a comma" in client.post("/sources/cloud", data=data).text
    good = client.post("/sources/cloud", data={**data, "account": ACCOUNT})
    assert "token set" in good.text and SECRET not in good.text
    with temp_db() as conn:
        assert list_cloud_sources(conn)[0]["account"] == ACCOUNT


def test_egress_expects_the_download_host(temp_db):
    from nexus.security import egress

    connect(temp_db, "sharepoint", SECRET, account=ACCOUNT)
    egress._allow_cache = None
    allow = egress._configured_allow()
    egress._allow_cache = None
    expected = "Source (your SharePoint / OneDrive connection)"
    assert allow["graph.microsoft.com"] == expected
    assert allow["sharepoint.com"] == expected
