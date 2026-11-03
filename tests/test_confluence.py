"""Confluence connector: Cloud (v2, Basic) and Data Center (v1, Bearer),
pagination, incremental change detection, 429 handling, closed-network policy,
targets (case / pool / no-AI), attachments and secret hygiene.

A fake Confluence answers through ``httpx.MockTransport`` behind the real
``nexus.netguard.safe_request``; DNS is faked. All names are fictional.
"""

from __future__ import annotations

import base64
import json
import logging
import socket

import httpx
import pytest

from nexus import netguard
from nexus.sources import confluence as CF

_DNS = {"acme-wiki.atlassian.example": "93.184.216.40", "wiki.corp": "10.20.1.9"}
CLOUD = "https://acme-wiki.atlassian.example"
DC = "https://wiki.corp/confluence"
TOKEN = "tok-SECRET-123"


def _storage(text: str) -> dict:
    return {"storage": {"value": text, "representation": "storage"}}


class FakeConfluence:
    """Just enough of the Cloud v2 / DC v1 REST API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pages = {
            "101": {"title": "Runbook", "space": "ENG", "version": 1,
                    "when": "2026-10-01T10:00:00.000Z",
                    "body": "<h1>Start</h1><p>Restart the <strong>queue</strong>.</p>"
                            "<ac:structured-macro ac:name=\"code\"><ac:parameter ac:name=\"language\">bash"
                            "</ac:parameter><ac:plain-text-body><![CDATA[systemctl restart q]]>"
                            "</ac:plain-text-body></ac:structured-macro>"
                            "<p>See <ac:link><ri:page ri:content-title=\"Escalation\" /></ac:link></p>",
                    "labels": ["ops", "runbook"], "ancestors": ["1", "2"]},
            "102": {"title": "Escalation", "space": "ENG", "version": 3,
                    "when": "2026-10-02T10:00:00.000Z", "body": "<p>Call the duty lead.</p>",
                    "labels": [], "ancestors": ["1"]},
            "103": {"title": "Holiday rota", "space": "ENG", "version": 1,
                    "when": "2026-10-03T10:00:00.000Z", "body": "<p>Rota for Q4.</p>",
                    "labels": [], "ancestors": []},
        }
        self.titles = {"1": "Home", "2": "Operations"}
        self.rate_limit_once = False
        self.attachments: dict[str, list[tuple[str, bytes]]] = {}
        self.comments: dict[str, list[str]] = {}

    # -- helpers
    def _ordered(self, newest_first: bool) -> list[tuple[str, dict]]:
        return sorted(self.pages.items(), key=lambda kv: kv[1]["when"], reverse=newest_first)

    def _v1_obj(self, pid: str, p: dict) -> dict:
        return {
            "id": pid, "type": "page", "title": p["title"],
            "space": {"key": p["space"], "name": "Engineering"},
            "version": {"number": p["version"], "when": p["when"],
                        "by": {"displayName": "Dana Example"}},
            "history": {"createdBy": {"displayName": "Robin Example"}},
            "body": _storage(p["body"]),
            "ancestors": [{"id": a, "title": self.titles[a]} for a in p["ancestors"]],
            "metadata": {"labels": {"results": [{"name": n} for n in p["labels"]]}},
            "_links": {"webui": f"/display/{p['space']}/{pid}"},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, headers={"Retry-After": "2"})
        path = request.url.path
        q = request.url.params
        # ---------------- Cloud v2
        if path == "/wiki/api/v2/spaces":
            spaces = [{"id": "9001", "key": "ENG", "name": "Engineering"},
                      {"id": "9002", "key": "HR", "name": "People"}]
            if q.get("keys"):
                spaces = [s for s in spaces if s["key"] in q["keys"].split(",")]
            return httpx.Response(200, json={"results": spaces, "_links": {}})
        if path == "/wiki/api/v2/spaces/9001/pages":
            items = self._ordered(newest_first=True)
            cursor = int(q.get("cursor") or 0)
            chunk = items[cursor:cursor + 2]
            nxt = (f"/wiki/api/v2/spaces/9001/pages?cursor={cursor + 2}&limit=2"
                   if cursor + 2 < len(items) else None)
            res = [{"id": pid, "title": p["title"], "authorId": "acc-1",
                    "version": {"number": p["version"], "createdAt": p["when"]},
                    "body": _storage(p["body"]), "_links": {"webui": f"/spaces/ENG/pages/{pid}"}}
                   for pid, p in chunk]
            return httpx.Response(200, json={"results": res, "_links": {"next": nxt} if nxt else {}})
        if path.startswith("/wiki/api/v2/pages/") and path.endswith("/labels"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"name": n} for n in self.pages[pid]["labels"]]})
        if path.startswith("/wiki/api/v2/pages/") and path.endswith("/ancestors"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"id": a, "type": "page"} for a in self.pages[pid]["ancestors"]]})
        if path.startswith("/wiki/api/v2/pages/") and path.endswith("/footer-comments"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"body": _storage(f"<p>{c}</p>")} for c in self.comments.get(pid, [])]})
        if path.startswith("/wiki/api/v2/pages/") and path.endswith("/attachments"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"title": n, "fileSize": len(d), "downloadLink": f"/download/attachments/{pid}/{n}"}
                for n, d in self.attachments.get(pid, [])]})
        if path.startswith("/wiki/download/attachments/"):
            pid, name = path.split("/")[4], path.split("/")[5]
            return httpx.Response(200, content=dict(self.attachments[pid])[name])
        if path.startswith("/wiki/api/v2/pages/"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"id": pid, "title": self.titles.get(pid, pid)})
        if path == "/wiki/rest/api/user":
            return httpx.Response(200, json={"displayName": "Robin Example"})
        # ---------------- DC v1
        if path == "/confluence/rest/api/space":
            return httpx.Response(200, json={"results": [{"key": "ENG", "name": "Engineering"}],
                                             "start": 0, "limit": 100, "size": 1})
        if path == "/confluence/rest/api/content/search":
            items = self._ordered(newest_first=False)
            start, limit = int(q.get("start") or 0), 2
            chunk = items[start:start + limit]
            return httpx.Response(200, json={
                "results": [self._v1_obj(pid, p) for pid, p in chunk],
                "start": start, "limit": limit, "size": len(chunk),
                "_links": {"base": DC, "context": "/confluence"}})
        if path.endswith("/child/comment"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"body": _storage(f"<p>{c}</p>"), "history": {"createdBy": {"displayName": "Kim"}}}
                for c in self.comments.get(pid, [])], "size": 0, "limit": 50})
        if path.endswith("/child/attachment"):
            pid = path.split("/")[5]
            return httpx.Response(200, json={"results": [
                {"title": n, "extensions": {"fileSize": len(d)},
                 "_links": {"download": f"/download/attachments/{pid}/{n}"}}
                for n, d in self.attachments.get(pid, [])], "size": 0, "limit": 50})
        if path.startswith("/confluence/download/attachments/"):
            pid, name = path.split("/")[4], path.split("/")[5]
            return httpx.Response(200, content=dict(self.attachments[pid])[name])
        return httpx.Response(404, json={"message": "no route"})


@pytest.fixture
def fake(monkeypatch):
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_DNS.get(host, host), 0))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", resolve)
    server = FakeConfluence()
    transport = httpx.MockTransport(server)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)
    return server


def _allow_internal(monkeypatch):
    from nexus.config import get_settings

    monkeypatch.setenv("INTERNAL_ALLOWED_HOSTS", "wiki.corp")
    get_settings.cache_clear()


def _make(temp_db, **cfg):
    from nexus.envstore import confluence_env_name, update_env
    from nexus.storage.confluence_sources import create_confluence_source

    base = {"name": "Team wiki", "deployment": "cloud", "base_url": CLOUD,
            "email": "analyst@example.org", "spaces": "ENG"}
    base.update(cfg)
    with temp_db() as conn:
        sid = create_confluence_source(conn, base)
    update_env({confluence_env_name(sid): TOKEN})
    return sid


def _src(temp_db, sid, sleep=lambda s: None):
    from nexus.storage.confluence_sources import get_confluence_source

    with temp_db() as conn:
        row = get_confluence_source(conn, sid)
    return CF.ConfluenceSource(row, sleep=sleep)


def _items(temp_db, source="Team wiki"):
    with temp_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, content, url, author, raw FROM items WHERE source = ? ORDER BY id",
            (source,)).fetchall()]


# ---------------------------------------------------------------- text
def test_storage_to_text_keeps_code_and_link_titles():
    text = CF.storage_to_text(FakeConfluence().pages["101"]["body"])
    assert "Restart the queue." in text
    assert "systemctl restart q" in text
    assert "Escalation" in text
    assert "language" not in text  # macro parameters dropped


def test_retry_after_parsing():
    assert CF.retry_after_seconds("5", 0) == 5
    assert CF.retry_after_seconds("9999", 0) == 60
    assert CF.retry_after_seconds(None, 2) == 4


# --------------------------------------------------------------- cloud v2
def test_cloud_v2_pull_with_basic_auth_and_metadata(temp_db, fake):
    sid = _make(temp_db)
    src = _src(temp_db, sid)
    assert src.is_available()
    res = src.ingest()
    assert res["new"] >= 3 and "3 checked, 3 new" in res["status"]
    auth = fake.requests[0].headers["authorization"]
    assert auth == "Basic " + base64.b64encode(f"analyst@example.org:{TOKEN}".encode()).decode()
    assert any("cursor=2" in str(r.url) for r in fake.requests)  # second page followed
    items = _items(temp_db)
    runbook = next(i for i in items if i["title"].startswith("Runbook"))
    raw = json.loads(runbook["raw"])
    assert raw["labels"] == ["ops", "runbook"]
    assert raw["breadcrumb"] == ["Home", "Operations"]
    assert raw["space"] == "ENG" and raw["version"] == "1"
    assert runbook["url"] == f"{CLOUD}/wiki/spaces/ENG/pages/101"
    assert runbook["author"] == "Robin Example"
    with temp_db() as conn:
        docs = conn.execute("SELECT * FROM documents WHERE kind = 'confluence'").fetchall()
    assert len(docs) == 3 and all(d["in_pool"] == 1 and d["stored_path"] == "" for d in docs)


def test_incremental_unchanged_then_changed(temp_db, fake):
    sid = _make(temp_db)
    _src(temp_db, sid).ingest()
    before = len(_items(temp_db))
    n_requests = len(fake.requests)

    res = _src(temp_db, sid).ingest()
    assert res["new"] == 0 and "0 new, 0 updated" in res["status"]
    assert len(_items(temp_db)) == before
    # Unchanged versions short-cut: no label/ancestor calls the second time.
    assert not any(str(r.url).endswith("/labels") for r in fake.requests[n_requests:])

    fake.pages["102"].update(version=4, when="2026-10-05T09:00:00.000Z",
                             body="<p>Call the duty lead.</p><p>Then page the manager.</p>")
    res = _src(temp_db, sid).ingest()
    assert res["changed"] == 1
    items = _items(temp_db)
    updated = [i for i in items if i["title"] == "Escalation — updated"]
    assert len(updated) == 1 and "+ Then page the manager." in updated[0]["content"]
    current = [i for i in items if i["title"] == "Escalation"]
    assert len(current) == 1 and "page the manager" in current[0]["content"]
    with temp_db() as conn:
        row = conn.execute("SELECT watermark FROM confluence_sources WHERE id = ?", (sid,)).fetchone()
    assert row["watermark"].startswith("2026-10-05")


def test_rate_limit_respects_retry_after(temp_db, fake):
    sid = _make(temp_db)
    waits: list[float] = []
    fake.rate_limit_once = True
    res = _src(temp_db, sid, sleep=waits.append).ingest()
    assert waits == [2.0]
    assert "3 new" in res["status"]


# ------------------------------------------------------------- data center
def test_dc_v1_bearer_cql_and_start_pagination(temp_db, fake, monkeypatch):
    _allow_internal(monkeypatch)
    sid = _make(temp_db, deployment="dc", base_url=DC, email="", include_blogposts="1",
                cql='label = "ops" ORDER BY title')
    res = _src(temp_db, sid).ingest()
    assert "3 checked, 3 new" in res["status"]
    searches = [r for r in fake.requests if r.url.path.endswith("/content/search")]
    assert [r.url.params.get("start") for r in searches] == [None, "2"]  # short page = end
    cql = searches[0].url.params["cql"]
    assert 'space in ("ENG")' in cql and "type in (page,blogpost)" in cql
    assert '(label = "ops")' in cql and cql.endswith("ORDER BY lastmodified ASC")
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)
    raw = json.loads(next(i for i in _items(temp_db) if i["title"].startswith("Runbook"))["raw"])
    assert raw["breadcrumb"] == ["Home", "Operations"] and raw["labels"] == ["ops", "runbook"]

    _src(temp_db, sid).ingest()
    second = [r for r in fake.requests if r.url.path.endswith("/content/search")][-1]
    assert 'lastmodified >= "2026-10-02' in second.url.params["cql"]  # watermark - overlap


def test_private_host_blocked_without_allow_list(temp_db, fake, monkeypatch):
    monkeypatch.delenv("INTERNAL_ALLOWED_HOSTS", raising=False)
    sid = _make(temp_db, deployment="dc", base_url=DC, email="")
    res = _src(temp_db, sid).ingest()
    assert res["status"].startswith("error: blocked")
    assert "INTERNAL_ALLOWED_HOSTS" in res["status"]
    assert fake.requests == []
    test = _src(temp_db, sid).test_connection()
    assert not test["ok"] and "blocked" in test["error"]


# ---------------------------------------------------- targets and extras
def test_case_target_no_ai_comments_and_attachments(temp_db, fake):
    from nexus.storage import create_case

    with temp_db() as conn:
        case_id = create_case(conn, "Vendor review")
    fake.comments["103"] = ["Approved for Q4."]
    fake.attachments["103"] = [("rota.csv", b"name,shift\nAlex,early\nSam,late\n")]
    sid = _make(temp_db, case_id=str(case_id), no_ai="1", include_comments="1",
                include_attachments="1")
    _src(temp_db, sid).ingest()
    with temp_db() as conn:
        docs = {d["filename"]: dict(d) for d in conn.execute(
            "SELECT * FROM documents WHERE kind = 'confluence'")}
        pinned = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE case_id = ?",
                              (case_id,)).fetchone()[0]
    rota = docs["Holiday rota"]
    assert rota["case_id"] == case_id and rota["in_pool"] == 0 and rota["no_ai"] == 1
    assert pinned >= 3
    texts = " ".join(i["content"] for i in _items(temp_db) if i["title"].startswith("Holiday rota"))
    assert "Approved for Q4." in texts
    assert "Alex" in texts and "early" in texts  # CSV attachment read by the upload parser


# ----------------------------------------------------------- secrets / UI
def test_token_only_in_env_never_db_ui_or_logs(temp_db, fake, caplog):
    from fastapi.testclient import TestClient

    from nexus import envstore
    from nexus.web.app import app

    caplog.set_level(logging.DEBUG)
    c = TestClient(app, base_url="http://127.0.0.1")
    page = c.get("/sources/confluence")
    assert page.status_code == 200 and "Connect Confluence" in page.text
    r = c.post("/sources/confluence", data={
        "deployment": "cloud", "base_url": CLOUD + "/wiki", "email": "analyst@example.org",
        "token": TOKEN, "destination": "pool", "allow_ai": "1"})
    assert r.status_code == 200 and "token set" in r.text and TOKEN not in r.text
    with temp_db() as conn:
        row = dict(conn.execute("SELECT * FROM confluence_sources").fetchone())
    assert TOKEN not in json.dumps(row) and row["base_url"] == CLOUD
    assert envstore.read_env_value(f"CONFLUENCE_SOURCE_{row['id']}_TOKEN") == TOKEN

    t = c.post(f"/sources/confluence/{row['id']}/test")
    assert "Engineering" in t.text and 'value="HR"' in t.text
    s = c.post(f"/sources/confluence/{row['id']}/spaces", data={"space": ["ENG", "bad key!"]})
    assert "spaces: <span class=\"mono\">ENG</span>" in s.text
    scan = c.post(f"/sources/confluence/{row['id']}/scan")
    assert "Scanned" in scan.text and TOKEN not in scan.text
    assert TOKEN not in caplog.text
    assert base64.b64encode(f"analyst@example.org:{TOKEN}".encode()).decode() not in caplog.text

    c.post(f"/sources/confluence/{row['id']}/delete")
    assert envstore.read_env_value(f"CONFLUENCE_SOURCE_{row['id']}_TOKEN") is None
    with temp_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connector_docs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3  # kept


def test_not_available_without_token_or_scope(temp_db):
    from nexus.envstore import confluence_env_name, update_env

    sid = _make(temp_db)
    assert _src(temp_db, sid).is_available()
    update_env({confluence_env_name(sid): ""})
    assert not _src(temp_db, sid).is_available()
    sid2 = _make(temp_db, spaces="", cql="")
    assert not _src(temp_db, sid2).is_available()


def test_collector_runs_ingest(temp_db, fake):
    from nexus.collector import Collector

    _make(temp_db)
    stats = Collector().scan()
    assert stats["Team wiki"]["new"] >= 3
    assert stats["_update"]["new"] >= 3


def test_document_delete_removes_connector_items(temp_db, fake):
    from nexus.storage.documents import delete_document

    sid = _make(temp_db)
    _src(temp_db, sid).ingest()
    with temp_db() as conn:
        doc_id = conn.execute("SELECT id FROM documents WHERE filename = 'Runbook'").fetchone()[0]
        assert delete_document(conn, doc_id) is None
    assert not [i for i in _items(temp_db) if i["title"].startswith("Runbook")]
