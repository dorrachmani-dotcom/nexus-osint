"""Slack connector against a fake Slack Web API. Names are fictional."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from cloud_fake import connect, documents, install, item_text, reload_source

TOKEN = "xoxb-SECRET-123"


def _ts(day: int, hour: int = 10, minute: int = 0) -> str:
    return f"{datetime(2026, 10, day, hour, minute, tzinfo=UTC).timestamp():.6f}"


class FakeSlack:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.channels = {"C1": "ops", "C2": "random"}
        self.members = {"C1"}
        self.messages = {
            "C1": [{"ts": _ts(1), "user": "U1", "text": "Deploy <@U2> see <https://w.example/a|the plan>"},
                   {"ts": _ts(1, 11), "user": "U2", "text": "Done &amp; dusted", "reply_count": 1,
                    "latest_reply": _ts(1, 12)},
                   {"ts": _ts(1, 9), "subtype": "channel_join", "user": "U2", "text": "joined"},
                   {"ts": _ts(2), "bot_id": "B1", "text": "nightly ok in <#C2|random>"}],
        }
        self.replies = {_ts(1, 11): [{"ts": _ts(1, 11), "user": "U2", "text": "Done"},
                                     {"ts": _ts(1, 12), "user": "U1", "text": "Thanks"}]}
        self.users = {"U1": "Robin", "U2": "Sam"}
        self.user_scope = True
        self.error = ""
        self.page = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        params = request.url.params
        if self.error:
            return httpx.Response(200, json={"ok": False, "error": self.error})
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user": "nexus", "team": "Example Team"})
        if method == "conversations.list":
            chans = [{"id": i, "name": n, "is_member": i in self.members} for i, n in self.channels.items()]
            return httpx.Response(200, json={"ok": True, "channels": chans})
        if method == "conversations.info":
            return httpx.Response(200, json={"ok": True, "channel": {"name": self.channels[params["channel"]]}})
        if method == "conversations.history":
            oldest = float(params["oldest"])
            msgs = [m for m in self.messages.get(params["channel"], []) if float(m["ts"]) >= oldest]
            msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
            start = int(params.get("cursor") or 0)
            part = msgs[start:start + self.page]
            more = start + self.page < len(msgs)
            return httpx.Response(200, json={"ok": True, "messages": part, "response_metadata":
                                             {"next_cursor": str(start + self.page) if more else ""}})
        if method == "conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": self.replies[params["ts"]]})
        if method == "users.info":
            if not self.user_scope:
                return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
            return httpx.Response(200, json={"ok": True, "user": {
                "profile": {"display_name": self.users[params["user"]]}}})
        return httpx.Response(404, json={})

    def calls(self, method: str) -> int:
        return sum(r.url.path.endswith(method) for r in self.requests)


@pytest.fixture
def fake(monkeypatch) -> FakeSlack:
    server = FakeSlack()
    install(monkeypatch, server)
    from nexus.sources import slack as S

    monkeypatch.setattr(S, "FIRST_DAYS", 100000)  # the fixed dates above stay in range
    return server


def _source(temp_db, **cfg):
    return connect(temp_db, "slack", TOKEN, scope="C1", **cfg)


def test_one_document_per_channel_per_day(temp_db, fake):
    res = _source(temp_db).ingest()
    assert res["status"].startswith("2 checked, 2 new")
    assert set(documents(temp_db, "slack")) == {"#ops 2026-10-01", "#ops 2026-10-02"}
    text = item_text(temp_db)
    assert "[10:00] Robin: Deploy @Sam see the plan (https://w.example/a)" in text
    assert "[11:00] Sam: Done & dusted" in text
    assert "    [12:00] Robin: Thanks" in text
    assert "[10:00] B1: nightly ok in #random" in text
    assert "joined" not in text  # channel noise
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


def test_only_get_requests(temp_db, fake):
    _source(temp_db).ingest()
    assert {r.method for r in fake.requests} == {"GET"}


def test_pagination(temp_db, fake):
    fake.page = 1
    assert _source(temp_db).ingest()["fetched"] == 2


def test_rescan_reads_nothing_new_then_a_day_grows(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    fake.requests.clear()
    res = reload_source(temp_db, src).ingest()
    assert res["fetched"] == 1 and "0 new, 0 updated" in res["status"]  # from the last day
    assert fake.calls("conversations.replies") == 0

    fake.messages["C1"].append({"ts": _ts(2, 15), "user": "U1", "text": "Late note"})
    res = reload_source(temp_db, src).ingest()
    assert res["changed"] == 1 and "Late note" in item_text(temp_db)


def test_edit_and_new_reply_update_the_day(temp_db, fake):
    src = _source(temp_db)
    src.ingest()
    last = fake.messages["C1"][3]
    last["text"], last["edited"] = "nightly FAILED", {"ts": _ts(2, 20)}
    res = reload_source(temp_db, src).ingest()
    assert res["changed"] == 1 and "nightly FAILED" in item_text(temp_db)


def test_scope_change_restarts_the_window(temp_db, fake):
    from nexus.storage.cloud_sources import get_cloud_source, set_cloud_scope

    src = _source(temp_db)
    src.ingest()
    fake.messages["C2"] = [{"ts": _ts(1), "user": "U1", "text": "Older channel history"}]
    with temp_db() as conn:
        set_cloud_scope(conn, src.id, ["C1", "C2"])
        assert get_cloud_source(conn, src.id)["watermark"] is None
    reload_source(temp_db, src).ingest()
    assert "Older channel history" in item_text(temp_db)


def test_without_users_read_ids_are_shown(temp_db, fake):
    fake.user_scope = False
    _source(temp_db).ingest()
    assert "[10:00] U1: Deploy @U2" in item_text(temp_db)


def test_needs_a_channel(temp_db, fake):
    assert not connect(temp_db, "slack", TOKEN).is_available()
    assert _source(temp_db).is_available()


def test_test_connection_offers_member_channels(temp_db, fake):
    result = _source(temp_db).test_connection()
    assert result["ok"] and result["account"] == "nexus in Example Team"
    assert result["items"] == [{"key": "C1", "name": "#ops"}]
    fake.members.clear()
    assert "/invite" in _source(temp_db).test_connection()["note"]


def test_errors_are_reported_not_raised(temp_db, fake, caplog):
    src = _source(temp_db)
    fake.error = "invalid_auth"
    assert src.ingest()["status"] == "error: Slack refused the token (invalid_auth)"
    fake.error = "not_in_channel"
    assert reload_source(temp_db, src).ingest()["status"] == "error: Slack said: not_in_channel"
    assert src.test_connection() == {"ok": False, "error": "Slack said: not_in_channel"}
    assert TOKEN not in caplog.text


def test_form_lists_slack_and_picks_channels(temp_db, fake):
    from fastapi.testclient import TestClient

    from nexus.web.app import app

    client = TestClient(app, base_url="http://127.0.0.1")
    assert "Connect Slack" in client.get("/sources/cloud").text
    src = connect(temp_db, "slack", TOKEN)
    page = client.post(f"/sources/cloud/{src.id}/test").text
    assert "Choose the channels to read" in page and 'value="C1"' in page
    assert "Not ready" in client.post(f"/sources/cloud/{src.id}/scan").text
    assert "1 of the channels chosen" in client.post(f"/sources/cloud/{src.id}/scope",
                                                     data={"item": "C1"}).text
    assert "Scanned" in client.post(f"/sources/cloud/{src.id}/scan").text
