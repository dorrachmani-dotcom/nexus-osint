"""Cloud connector framework and Notion, against a fake Notion API. Names are fictional."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from cloud_fake import connect, documents, install, item_text, reload_source

TOKEN = "ntn-SECRET-123"
API = "https://api.notion.com/v1"


def _text(value: str) -> list[dict]:
    return [{"plain_text": value}]


def _page(pid: str, title: str, when: str, **extra) -> dict:
    props = {"Name": {"type": "title", "title": _text(title)}, **extra}
    return {"object": "page", "id": pid, "last_edited_time": when, "properties": props,
            "url": f"https://notion.example/{pid}", "parent": {"type": "workspace"}}


class FakeNotion:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.pages = {
            "p1": _page("p1", "Runbook", "2026-10-03T10:00:00.000Z",
                        Status={"type": "select", "select": {"name": "Active"}}),
            "p2": _page("p2", "Rota", "2026-10-01T10:00:00.000Z"),
        }
        self.blocks = {
            "p1": [{"id": "b1", "type": "heading_1", "heading_1": {"rich_text": _text("Start")}},
                   {"id": "b2", "type": "paragraph", "has_children": True,
                    "paragraph": {"rich_text": _text("Restart the queue.")}},
                   {"id": "b3", "type": "to_do", "to_do": {"rich_text": _text("Check"), "checked": True}},
                   {"id": "b4", "type": "child_page", "has_children": True,
                    "child_page": {"title": "Details"}}],
            "b2": [{"id": "b5", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": _text("Nested step")}}],
            "b4": [{"id": "b6", "type": "paragraph", "paragraph": {"rich_text": _text("MUST NOT APPEAR")}}],
            "p2": [{"id": "b7", "type": "paragraph", "paragraph": {"rich_text": _text("Rota for Q4.")}}],
        }
        self.rate_limit_once = False
        self.status = 200
        self.page_size = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "no"})
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        path = request.url.path
        if path == "/v1/users/me":
            return httpx.Response(200, json={"name": "Nexus reader"})
        if path == "/v1/search":
            body = json.loads(request.content)
            ordered = sorted(self.pages.values(), key=lambda p: p["last_edited_time"], reverse=True)
            start = int(body.get("start_cursor") or 0)
            part = ordered[start:start + self.page_size]
            more = start + self.page_size < len(ordered)
            return httpx.Response(200, json={"results": part, "has_more": more,
                                             "next_cursor": str(start + self.page_size) if more else None})
        if path.endswith("/children"):
            block = path.split("/")[3]
            return httpx.Response(200, json={"results": self.blocks.get(block, []), "has_more": False})
        return httpx.Response(404, json={})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def fake(monkeypatch) -> FakeNotion:
    server = FakeNotion()
    install(monkeypatch, server)
    return server


def test_scan_reads_pages_as_text(temp_db, fake):
    src = connect(temp_db, "notion", TOKEN)
    res = src.ingest()
    assert res["status"].startswith("2 checked, 2 new") and res["new"] > 0
    assert set(documents(temp_db, "notion")) == {"Runbook", "Rota"}
    text = item_text(temp_db)
    for expected in ("Status: Active", "# Start", "Restart the queue.", "- Nested step",
                     "[x] Check", "Sub-page: Details"):
        assert expected in text
    assert "MUST NOT APPEAR" not in text  # a sub-page is listed on its own
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)
    assert all(r.headers["notion-version"] for r in fake.requests)


def test_only_reads_with_get_or_search(temp_db, fake):
    connect(temp_db, "notion", TOKEN).ingest()
    assert {(r.method, r.url.path) for r in fake.requests if r.method != "GET"} == {
        ("POST", "/v1/search")}


def test_pagination(temp_db, fake):
    fake.page_size = 1
    assert connect(temp_db, "notion", TOKEN).ingest()["fetched"] == 2


def test_incremental_scan_reads_only_what_changed(temp_db, fake):
    src = connect(temp_db, "notion", TOKEN)
    src.ingest()
    fake.requests.clear()
    reload_source(temp_db, src).ingest()
    assert not any(p.endswith("/children") for p in fake.paths())

    fake.pages["p2"]["last_edited_time"] = "2026-10-05T10:00:00.000Z"
    fake.blocks["p2"][0]["paragraph"]["rich_text"] = _text("Rota for Q1.")
    fake.requests.clear()
    res = reload_source(temp_db, src).ingest()
    assert res["changed"] == 1 and "1 updated" in res["status"]
    assert [p for p in fake.paths() if p.endswith("/children")] == ["/v1/blocks/p2/children"]
    assert "Rota for Q1." in item_text(temp_db)


def test_stops_at_the_watermark(temp_db, fake):
    src = connect(temp_db, "notion", TOKEN)
    src.ingest()
    fake.pages["p3"] = _page("p3", "Newer", "2026-10-09T10:00:00.000Z")
    fresh = reload_source(temp_db, src)
    assert fresh.ingest()["fetched"] == 2  # p3 and p1 (within the overlap of p1's time)


def test_max_items_finishes_next_scan_without_moving_watermark(temp_db, fake):
    from nexus.storage.cloud_sources import get_cloud_source

    src = connect(temp_db, "notion", TOKEN, max_items=1)
    res = src.ingest()
    assert "stopped at 1" in res["status"] and len(documents(temp_db, "notion")) == 1
    with temp_db() as conn:
        assert get_cloud_source(conn, src.id)["watermark"] is None
    reload_source(temp_db, src).ingest()
    assert len(documents(temp_db, "notion")) == 2


def test_rate_limit_is_waited_out(temp_db, fake):
    fake.rate_limit_once = True
    src = connect(temp_db, "notion", TOKEN)
    waits: list[float] = []
    src._sleep = waits.append
    assert src.ingest()["new"] > 0 and waits == [1.0]


def test_bad_token_is_reported_not_raised(temp_db, fake, caplog):
    fake.status = 401
    src = connect(temp_db, "notion", TOKEN)
    assert src.ingest()["status"] == "error: HTTP 401: the token was refused"
    result = src.test_connection()
    assert result == {"ok": False, "error": "HTTP 401: the token was refused"}
    assert TOKEN not in caplog.text


def test_one_unreadable_page_is_skipped_and_retried(temp_db, fake):
    from nexus.sources.cloud import CloudError
    from nexus.storage.cloud_sources import get_cloud_source

    src = connect(temp_db, "notion", TOKEN)
    real = src._blocks

    def flaky(block_id, depth, lines):
        if block_id == "p1":
            raise CloudError("HTTP 403: this account may not read that")
        real(block_id, depth, lines)

    src._blocks = flaky
    res = src.ingest()
    assert "1 could not be read" in res["status"]
    assert set(documents(temp_db, "notion")) == {"Rota"}
    with temp_db() as conn:
        assert get_cloud_source(conn, src.id)["watermark"] is None  # retried next scan


def test_refuses_other_hosts_and_plain_http(temp_db, fake):
    from nexus.sources.cloud import CloudError

    src = connect(temp_db, "notion", TOKEN)
    for url in ("https://evil.example/v1/search", "http://api.notion.com/v1/search"):
        with pytest.raises(CloudError):
            src.request("GET", url)
    assert fake.requests == []


def test_targets_case_and_no_ai_default(temp_db, fake):
    from nexus.storage import create_case

    with temp_db() as conn:
        case_id = create_case(conn, "Field work")
    connect(temp_db, "notion", TOKEN, case_id=case_id, in_pool="0").ingest()
    docs = documents(temp_db, "notion")
    assert all(d["case_id"] == case_id and not d["in_pool"] and d["no_ai"] for d in docs.values())


def test_full_listing_removes_deleted_but_not_on_empty(temp_db, fake):
    """FULL_LISTING connectors drop what vanished, unless everything did."""
    src = connect(temp_db, "notion", TOKEN)
    src.FULL_LISTING = True
    src.ingest()
    del fake.pages["p2"]
    fresh = reload_source(temp_db, src)
    fresh.FULL_LISTING = True
    fresh.cfg["watermark"] = None
    assert fresh.ingest()["removed"] == 1
    assert set(documents(temp_db, "notion")) == {"Runbook"}

    fake.pages.clear()
    last = reload_source(temp_db, src)
    last.FULL_LISTING = True
    assert last.ingest()["removed"] == 0
    assert set(documents(temp_db, "notion")) == {"Runbook"}  # lost access, not deleted


def test_ui_flow_keeps_the_token_out_of_everything(temp_db, fake, caplog):
    from nexus import envstore
    from nexus.storage.cloud_sources import list_cloud_sources
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    with temp_db() as conn:
        assert list_cloud_sources(conn) == []
    assert "Connect Notion" in client.get("/sources/cloud").text
    r = client.post("/sources/cloud", data={"kind": "notion", "token": TOKEN, "destination": "pool"})
    assert r.status_code == 200 and "token set" in r.text and TOKEN not in r.text
    with temp_db() as conn:
        row = list_cloud_sources(conn)[0]
    assert TOKEN not in json.dumps(row) and row["no_ai"] == 1
    name = f"CLOUD_SOURCE_{row['id']}_TOKEN"
    assert envstore.read_env_value(name) == TOKEN

    test = client.post(f"/sources/cloud/{row['id']}/test")
    assert "Connected as Nexus reader" in test.text and "Runbook" in test.text
    scan = client.post(f"/sources/cloud/{row['id']}/scan")
    assert "Scanned" in scan.text and TOKEN not in scan.text and TOKEN not in caplog.text
    assert "Disconnect" in client.get("/sources/cloud").text

    assert client.post(f"/sources/cloud/{row['id']}/delete").status_code == 200
    assert envstore.read_env_value(name) is None
    assert set(documents(temp_db, "notion")) == {"Runbook", "Rota"}  # what was read stays


def test_form_errors(temp_db, fake):
    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    assert "Unknown connector" in client.post("/sources/cloud", data={"kind": "x", "token": "t"}).text
    assert "Paste the integration secret" in client.post("/sources/cloud", data={"kind": "notion"}).text


def test_collector_runs_connections_and_skips_tokenless(temp_db, fake):
    from nexus.envstore import cloud_env_name, update_env
    from nexus.sources.cloud import load_cloud_sources

    src = connect(temp_db, "notion", TOKEN)
    assert [s.id for s in load_cloud_sources() if s.is_available()] == [src.id]
    update_env({cloud_env_name(src.id): ""})
    assert [s for s in load_cloud_sources() if s.is_available()] == []


def test_egress_allow_list_names_connected_services(temp_db):
    from nexus.security import egress
    from nexus.storage.confluence_sources import create_confluence_source

    connect(temp_db, "notion", TOKEN)
    with temp_db() as conn:
        create_confluence_source(conn, {"base_url": "https://wiki.example.org", "deployment": "dc"})
    egress._allow_cache = None
    allow = egress._configured_allow()
    egress._allow_cache = None
    assert allow["api.notion.com"] == "Source (your Notion connection)"
    assert allow["wiki.example.org"] == "Source (your Confluence)"


def test_token_counts_as_a_secret_in_backups():
    from nexus.envstore import is_per_source_secret

    assert is_per_source_secret("CLOUD_SOURCE_7_TOKEN")


def test_every_connector_ships_in_the_installer():
    """Connectors are imported by name, so PyInstaller needs them listed."""
    from nexus.sources.cloud import _CONNECTORS

    spec = (Path(__file__).parent.parent / "installer" / "nexus.spec").read_text(encoding="utf-8")
    for target in _CONNECTORS.values():
        assert f'"{target.split(":")[0]}"' in spec
