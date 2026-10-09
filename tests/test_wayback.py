"""Internet Archive (Wayback Machine) preservation.

Covers the HTTP parsing for lookup / anonymous Save Page Now / authenticated
SPN2 (with httpx.MockTransport — nothing ever reaches archive.org), the SSRF
guard, the rate limiter and background queue, storage + migration, the web
routes (drawer box, polling, case archive-all, auto-archive, OpSec warning) and
the evidence manifest / case export integration. All URLs are fictional.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus import storage as s, wayback
from nexus.models import RawItem

SRC = "https://news.example.org/harbor/story-17"
SNAP = f"https://web.archive.org/web/20260102030405/{SRC}"


@pytest.fixture
def public_ok(monkeypatch):
    """Treat every host as public so tests never depend on DNS."""
    monkeypatch.setattr(wayback, "safe_http_url", lambda url: (True, ""))


def _client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_keys():
    return SimpleNamespace(archive_org_access_key=None, archive_org_secret_key=None)


def _keys():
    return SimpleNamespace(archive_org_access_key="testaccess", archive_org_secret_key="testsecret")


# ------------------------------------------------------------------ helpers


def test_timestamp_and_snapshot_parsing():
    assert wayback.timestamp_to_iso("20260102030405") == "2026-01-02T03:04:05Z"
    assert wayback.timestamp_to_iso("garbage") == ""
    assert wayback.parse_snapshot_path(f"/web/20260102030405/{SRC}") == (SNAP, "2026-01-02T03:04:05Z")
    # id_/if_ style modifiers and surrounding HTML are tolerated.
    url, ts = wayback.parse_snapshot_path(f'<a href="/web/20260102030405id_/{SRC}">')
    assert url == SNAP and ts.startswith("2026-01-02")
    assert wayback.parse_snapshot_path("nothing here") is None


# ------------------------------------------------------------------- lookup


def test_lookup_found(public_ok):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"archived_snapshots": {"closest": {
            "available": True, "status": "200",
            "url": f"http://web.archive.org/web/20260102030405/{SRC}",
            "timestamp": "20260102030405"}}})

    res = wayback.lookup(SRC, client=_client_for(handler))
    assert res["ok"] is True
    assert res["archive_url"] == SNAP  # upgraded to https
    assert res["archived_at"] == "2026-01-02T03:04:05Z"
    assert seen["url"].startswith(wayback.AVAILABILITY_API)
    assert "news.example.org" in seen["url"]
    assert seen["ua"] == wayback.USER_AGENT


def test_lookup_none_and_errors(public_ok):
    empty = wayback.lookup(SRC, client=_client_for(lambda r: httpx.Response(200, json={"archived_snapshots": {}})))
    assert empty["ok"] is False and "No snapshot" in empty["error"]

    busy = wayback.lookup(SRC, client=_client_for(lambda r: httpx.Response(503)))
    assert busy["ok"] is False and "busy or unavailable" in busy["error"]

    def boom(request):
        raise httpx.ConnectError("offline")

    offline = wayback.lookup(SRC, client=_client_for(boom))
    assert offline["ok"] is False and "Could not reach" in offline["error"]

    bad_json = wayback.lookup(SRC, client=_client_for(lambda r: httpx.Response(200, text="<html>")))
    assert bad_json["ok"] is False and bad_json["error"]


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd", "http://127.0.0.1/admin", "http://10.0.0.5/x", "ftp://files.example.org/a",
])
def test_ssrf_rejected_without_any_request(bad):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    assert wayback.lookup(bad, client=_client_for(handler))["ok"] is False
    res = wayback.save(bad, settings=_no_keys(), client=_client_for(handler))
    assert res["ok"] is False and "can't be archived" in res["error"]
    assert wayback.check_url(bad)
    assert calls == []


# -------------------------------------------------------- save (anonymous)


def test_save_anonymous_content_location(public_ok):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, headers={"Content-Location": f"/web/20260102030405/{SRC}"}, text="ok")

    res = wayback.save(SRC, settings=_no_keys(), client=_client_for(handler))
    assert res == {"ok": True, "archive_url": SNAP, "archived_at": "2026-01-02T03:04:05Z",
                   "error": "", "rate_limited": False}
    assert seen["method"] == "GET"
    assert seen["url"].startswith("https://web.archive.org/save/https://news.example.org")


def test_save_anonymous_redirect_location_and_body(public_ok):
    redirect = wayback.save(SRC, settings=_no_keys(), client=_client_for(
        lambda r: httpx.Response(302, headers={"Location": SNAP})))
    assert redirect["ok"] and redirect["archive_url"] == SNAP

    body = wayback.save(SRC, settings=_no_keys(), client=_client_for(
        lambda r: httpx.Response(200, text=f'var u = "/web/20260102030405/{SRC}";')))
    assert body["ok"] and body["archive_url"] == SNAP

    nothing = wayback.save(SRC, settings=_no_keys(), client=_client_for(
        lambda r: httpx.Response(200, text="queued")))
    # archive.org now usually answers anonymous captures with a login page.
    assert nothing["ok"] is False and "free archive.org account" in nothing["error"]


def test_save_anonymous_errors(public_ok):
    limited = wayback.save(SRC, settings=_no_keys(), client=_client_for(lambda r: httpx.Response(429)))
    assert limited["ok"] is False and limited["rate_limited"] is True
    assert "free archive.org account" in limited["error"]  # points at keys / browser save

    down = wayback.save(SRC, settings=_no_keys(), client=_client_for(lambda r: httpx.Response(520)))
    assert down["ok"] is False and down["rate_limited"] is False

    def slow(request):
        raise httpx.ReadTimeout("slow")

    timeout = wayback.save(SRC, settings=_no_keys(), client=_client_for(slow))
    assert timeout["ok"] is False and "in time" in timeout["error"]


# ------------------------------------------------------- save (SPN2 / keys)


def test_save_spn2_polls_until_success(public_ok):
    calls = []
    statuses = iter([{"status": "pending"}, {"status": "pending"},
                     {"status": "success", "timestamp": "20260102030405", "original_url": SRC}])

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert request.headers["authorization"] == "LOW testaccess:testsecret"
            assert b"url=" in request.content
            return httpx.Response(200, json={"url": SRC, "job_id": "spn2-abc123"})
        assert request.url.path == "/save/status/spn2-abc123"
        return httpx.Response(200, json=next(statuses))

    sleeps = []
    res = wayback.save(SRC, settings=_keys(), client=_client_for(handler), sleep=sleeps.append)
    assert res["ok"] is True and res["archive_url"] == SNAP
    assert res["archived_at"] == "2026-01-02T03:04:05Z"
    assert len(sleeps) == 2  # two "pending" polls
    assert all(r.headers.get("user-agent") == wayback.USER_AGENT for r in calls)


def test_save_spn2_failures(public_ok):
    def declined(request):
        return httpx.Response(200, json={"status": "error", "message": "This host is excluded."})

    res = wayback.save(SRC, settings=_keys(), client=_client_for(declined), sleep=lambda s: None)
    assert res["ok"] is False and "excluded" in res["error"]

    def job_error(request):
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j1"})
        return httpx.Response(200, json={"status": "error", "message": "Connection refused"})

    res = wayback.save(SRC, settings=_keys(), client=_client_for(job_error), sleep=lambda s: None)
    assert res["ok"] is False and "Connection refused" in res["error"]

    def forever(request):
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j2"})
        return httpx.Response(200, json={"status": "pending"})

    res = wayback.save(SRC, settings=_keys(), client=_client_for(forever),
                       sleep=lambda s: None, poll_timeout=8)
    assert res["ok"] is False and "too long" in res["error"]

    res = wayback.save(SRC, settings=_keys(), client=_client_for(lambda r: httpx.Response(401)))
    assert res["ok"] is False and "keys" in res["error"]


def test_keys_configured_needs_both():
    assert wayback.keys_configured(_keys()) is True
    assert wayback.keys_configured(_no_keys()) is False
    half = SimpleNamespace(archive_org_access_key="a", archive_org_secret_key="")
    assert wayback.keys_configured(half) is False


# ----------------------------------------------------- rate limit + queue


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, d):
        self.slept.append(d)
        self.t += d


def test_rate_limiter_spaces_requests():
    clk = FakeClock()
    lim = wayback.RateLimiter(6.0, clock=clk.now, sleep=clk.sleep)
    assert lim.wait() == 0
    assert lim.wait() == pytest.approx(6.0)
    clk.t += 20  # idle for a while -> next request is immediate
    assert lim.wait() == 0
    lim.penalize(90)
    assert lim.wait() == pytest.approx(90.0)


def _seed_item(conn, title="Harbor crane collapse", url=SRC) -> int:
    iid, _ = s.upsert_item(conn, RawItem(source="rss", title=title, content=f"Fictional story: {title}.", url=url))
    return iid


def _queue(saver, limiter=None):
    return wayback.ArchiveQueue(saver=saver, limiter=limiter or wayback.RateLimiter(0),
                                autostart=False)


def test_queue_records_pending_then_done(temp_db):
    with temp_db() as conn:
        iid = _seed_item(conn)
    q = _queue(lambda url: {"ok": True, "archive_url": SNAP, "archived_at": "2026-01-02T03:04:05Z",
                            "error": "", "rate_limited": False})
    assert q.enqueue(iid, SRC) is True
    assert q.enqueue(iid, SRC) is False  # dedup while queued
    assert q.is_queued(iid) and q.pending_count() == 1
    with temp_db() as conn:
        assert s.get_item_archive(conn, iid)["status"] == "pending"
    assert q.process_next() is True
    assert q.process_next() is False  # empty
    assert not q.is_queued(iid)
    with temp_db() as conn:
        row = s.get_item_archive(conn, iid)
    assert row["status"] == "done" and row["archive_url"] == SNAP
    assert row["archived_at"] == "2026-01-02T03:04:05Z" and row["error"] is None


def test_queue_failure_and_rate_limit_backoff(temp_db):
    with temp_db() as conn:
        iid = _seed_item(conn)
    clk = FakeClock()
    lim = wayback.RateLimiter(6.0, clock=clk.now, sleep=clk.sleep)
    q = _queue(lambda url: {"ok": False, "archive_url": "", "archived_at": "",
                            "error": "The Internet Archive is rate-limiting requests right now.",
                            "rate_limited": True}, limiter=lim)
    q.enqueue(iid, SRC)
    q.process_next()
    with temp_db() as conn:
        row = s.get_item_archive(conn, iid)
    assert row["status"] == "failed" and "rate-limiting" in row["error"]
    # The 429 pushed the next slot out by the cooldown.
    assert lim.wait() == pytest.approx(wayback.RATE_LIMIT_COOLDOWN)


def test_queue_survives_a_raising_saver(temp_db):
    with temp_db() as conn:
        iid = _seed_item(conn)

    def bad(url):
        raise RuntimeError("boom")

    q = _queue(bad)
    q.enqueue(iid, SRC)
    assert q.process_next() is True
    with temp_db() as conn:
        assert s.get_item_archive(conn, iid)["status"] == "failed"


def test_queue_worker_thread_drains(temp_db):
    with temp_db() as conn:
        iid = _seed_item(conn)
    q = wayback.ArchiveQueue(
        saver=lambda url: {"ok": True, "archive_url": SNAP, "archived_at": "", "error": "",
                           "rate_limited": False},
        limiter=wayback.RateLimiter(0), autostart=True)
    q.enqueue(iid, SRC)
    q._q.join()  # the background worker processed it
    with temp_db() as conn:
        row = s.get_item_archive(conn, iid)
    assert row["status"] == "done" and row["archived_at"]  # falls back to "now"


# ---------------------------------------------------- storage + migration


def test_migration_is_idempotent_and_recreates_table(temp_db):
    from nexus.db import init_db

    init_db()  # second run: no error
    with temp_db() as conn:
        conn.execute("DROP TABLE item_archives")
    init_db()
    with temp_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(item_archives)")}
        case_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cases)")}
    assert {"item_id", "url", "archive_url", "status", "requested_at", "archived_at", "error"} <= cols
    assert "auto_archive" in case_cols


def test_storage_helpers(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        a = _seed_item(conn, "Story A", "https://news.example.org/a")
        b = _seed_item(conn, "Story B", "https://news.example.org/b")
        c = _seed_item(conn, "Story C", "https://news.example.org/c")
        nolink = _seed_item(conn, "No link", None)
        for i in (a, b, c, nolink):
            s.add_bookmark(conn, i, cid)
        with pytest.raises(ValueError):
            s.set_item_archive(conn, a, "x", "bogus")
        s.set_item_archive(conn, a, "https://news.example.org/a", "done",
                           archive_url=SNAP, archived_at="2026-01-02T03:04:05Z")
        s.set_item_archive(conn, b, "https://news.example.org/b", "failed", error="nope")
        targets = {t["id"] for t in s.case_archive_targets(conn, cid)}
        assert targets == {b, c}  # failed + never tried; done and link-less skipped
        s.set_item_archive(conn, c, "https://news.example.org/c", "pending")
        summ = s.case_archive_summary(conn, cid)
        assert (summ["total"], summ["archived"], summ["pending"], summ["failed"]) == (3, 1, 1, 1)
        assert set(s.item_archives_map(conn, [a, b, nolink])) == {a, b}
        # A new pending attempt clears the old error; a failure keeps the old link.
        s.set_item_archive(conn, a, "https://news.example.org/a", "failed", error="later failure")
        assert s.get_item_archive(conn, a)["archive_url"] == SNAP
        assert s.fail_stale_archive_jobs(conn) == 1
        assert s.get_item_archive(conn, c)["status"] == "failed"
        assert "restarted" in s.get_item_archive(conn, c)["error"]
        assert s.case_auto_archive(conn, cid) is False
        s.set_case_auto_archive(conn, cid, True)
        assert s.case_auto_archive(conn, cid) is True
        assert s.get_case(conn, cid)["auto_archive"] == 1


def test_auto_archive_on_pin_conditions(temp_db):
    q = _queue(lambda url: {})
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        iid = _seed_item(conn)
        s.add_bookmark(conn, iid, cid)
        assert wayback.auto_archive_on_pin(conn, iid, cid, q=q) is False  # case not opted in
        s.set_case_auto_archive(conn, cid, True)
        assert wayback.auto_archive_on_pin(conn, iid, cid, q=q) is False  # warning not acknowledged
        wayback.acknowledge_opsec(conn)
        s.set_meta(conn, wayback.META_ENABLED, "0")
        assert wayback.auto_archive_on_pin(conn, iid, cid, q=q) is False  # globally off
        s.set_meta(conn, wayback.META_ENABLED, "1")
        assert wayback.auto_archive_on_pin(conn, iid, None, q=q) is False  # not a case pin
        assert wayback.auto_archive_on_pin(conn, iid, cid, q=q) is True
        assert s.get_item_archive(conn, iid)["status"] == "pending"
        assert wayback.auto_archive_on_pin(conn, iid, cid, q=q) is False  # already pending


def test_egress_allow_list_includes_archive():
    from nexus.security.egress import classify

    assert classify("web.archive.org", 443) == ("Archive (Internet Archive)", True)
    assert classify("archive.org", 443) == ("Archive (Internet Archive)", True)


# ------------------------------------------------------------------ routes


def _web():
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def test_queue(monkeypatch, public_ok):
    q = _queue(lambda url: {"ok": True, "archive_url": SNAP, "archived_at": "2026-01-02T03:04:05Z",
                            "error": "", "rate_limited": False})
    monkeypatch.setattr(wayback, "_QUEUE", q)
    return q


def test_drawer_shows_archive_box(temp_db, test_queue):
    with temp_db() as conn:
        iid = _seed_item(conn)
    r = _web().get(f"/items/{iid}/detail")
    assert r.status_code == 200
    assert f'id="archive-box-{iid}"' in r.text
    assert "Not archived" in r.text and "Find existing snapshot" in r.text


def test_item_archive_flow_with_one_time_warning(temp_db, test_queue):
    with temp_db() as conn:
        iid = _seed_item(conn)
    c = _web()

    first = c.post(f"/items/{iid}/archive")
    assert first.status_code == 200
    assert "PUBLIC" in first.text and "I understand" in first.text
    with temp_db() as conn:
        assert s.get_item_archive(conn, iid) is None  # nothing queued before consent

    acked = c.post(f"/items/{iid}/archive", data={"acknowledge": "1"})
    assert "archiving" in acked.text
    assert 'hx-trigger="load delay:3s"' in acked.text  # polls while pending
    with temp_db() as conn:
        assert wayback.opsec_acknowledged(conn)
        assert s.get_item_archive(conn, iid)["status"] == "pending"

    test_queue.process_next()
    done = c.get(f"/items/{iid}/archive")
    assert "Archived" in done.text and SNAP in done.text
    assert "load delay:3s" not in done.text

    # The warning is not shown again.
    again = c.post(f"/items/{iid}/archive")
    assert "PUBLIC" not in again.text


def test_item_archive_failure_shows_reason_and_retry(temp_db, test_queue, monkeypatch):
    with temp_db() as conn:
        iid = _seed_item(conn)
        wayback.acknowledge_opsec(conn)
    test_queue._saver = lambda url: {"ok": False, "archive_url": "", "archived_at": "",
                                     "error": "The Internet Archive could not reach this page.",
                                     "rate_limited": False}
    c = _web()
    c.post(f"/items/{iid}/archive")
    test_queue.process_next()
    r = c.get(f"/items/{iid}/archive")
    assert "Archiving failed" in r.text and "could not reach this page" in r.text
    assert "Retry archive" in r.text


def test_item_archive_disabled_and_ssrf(temp_db, test_queue, monkeypatch):
    with temp_db() as conn:
        iid = _seed_item(conn)
        wayback.acknowledge_opsec(conn)
    c = _web()
    r = c.post("/settings/archive", data={})  # unchecked box -> off
    assert r.status_code == 200 and "Saved." in r.text
    off = c.post(f"/items/{iid}/archive")
    assert "turned off" in off.text
    assert test_queue.pending_count() == 0
    c.post("/settings/archive", data={"enabled": "1"})

    monkeypatch.setattr(wayback, "safe_http_url", lambda url: (False, "refusing non-public host"))
    blocked = c.post(f"/items/{iid}/archive")
    assert "be archived (refusing non-public host)" in blocked.text
    assert test_queue.pending_count() == 0


def test_item_lookup_route_records_existing(temp_db, test_queue, monkeypatch):
    with temp_db() as conn:
        iid = _seed_item(conn)
        wayback.acknowledge_opsec(conn)
    monkeypatch.setattr(wayback, "lookup", lambda url: {
        "ok": True, "archive_url": SNAP, "archived_at": "2026-01-02T03:04:05Z", "error": ""})
    r = _web().post(f"/items/{iid}/archive/lookup")
    assert "Found a snapshot from 2026-01-02 03:04" in r.text
    assert "Snapshot" in r.text and SNAP in r.text
    with temp_db() as conn:
        assert s.get_item_archive(conn, iid)["status"] == "existing"

    monkeypatch.setattr(wayback, "lookup", lambda url: {
        "ok": False, "archive_url": "", "archived_at": "",
        "error": "No snapshot of this page exists on the Wayback Machine yet."})
    r = _web().post(f"/items/{iid}/archive/lookup")
    assert "No snapshot" in r.text
    with temp_db() as conn:  # a miss never erases a known capture
        assert s.get_item_archive(conn, iid)["status"] == "existing"


def test_lookup_also_requires_the_warning(temp_db, test_queue, monkeypatch):
    with temp_db() as conn:
        iid = _seed_item(conn)
    monkeypatch.setattr(wayback, "lookup", lambda url: pytest.fail("must not call archive.org"))
    r = _web().post(f"/items/{iid}/archive/lookup")
    assert "PUBLIC" in r.text and "/archive/lookup" in r.text


def test_case_archive_all_with_progress(temp_db, test_queue):
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        ids = [_seed_item(conn, f"Story {n}", f"https://news.example.org/{n}") for n in range(3)]
        for i in ids:
            s.add_bookmark(conn, i, cid)
    c = _web()
    page = c.get(f"/cases/{cid}", params={"tab": "pinned"})
    assert page.status_code == 200
    assert "Archive all pinned items" in page.text
    assert "Auto-archive items when pinned" in page.text
    assert "0 of 3 pinned link(s) archived" in page.text

    warn = c.post(f"/cases/{cid}/archive-all")
    assert "PUBLIC" in warn.text and test_queue.pending_count() == 0

    r = c.post(f"/cases/{cid}/archive-all", data={"acknowledge": "1"})
    assert "Queued 3 item(s)" in r.text
    assert "3 in queue" in r.text and 'hx-trigger="load delay:3s"' in r.text

    while test_queue.process_next():
        pass
    status = c.get(f"/cases/{cid}/archive")
    assert "3 of 3 pinned link(s) archived" in status.text
    assert "load delay:3s" not in status.text
    again = c.post(f"/cases/{cid}/archive-all")
    assert "Nothing to archive" in again.text

    pinned = c.get(f"/cases/{cid}", params={"tab": "pinned"})
    assert "&#127963; archived" in pinned.text or "archived</a>" in pinned.text


def test_case_auto_archive_toggle_and_pin(temp_db, test_queue):
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        iid = _seed_item(conn)
    c = _web()
    warn = c.post(f"/cases/{cid}/auto-archive", data={"enabled": "1"})
    assert "PUBLIC" in warn.text
    with temp_db() as conn:
        assert s.case_auto_archive(conn, cid) is False

    on = c.post(f"/cases/{cid}/auto-archive", data={"enabled": "1", "acknowledge": "1"})
    assert "archived automatically" in on.text and "checked" in on.text
    with temp_db() as conn:
        assert s.case_auto_archive(conn, cid) is True

    c.post(f"/cases/{cid}/items/{iid}/add")
    assert test_queue.is_queued(iid)

    off = c.post(f"/cases/{cid}/auto-archive", data={})
    assert "Auto-archive is off" in off.text
    with temp_db() as conn:
        assert s.case_auto_archive(conn, cid) is False


def test_pin_without_opt_in_does_not_archive(temp_db, test_queue):
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        iid = _seed_item(conn)
        wayback.acknowledge_opsec(conn)
    _web().post(f"/cases/{cid}/items/{iid}/add")
    assert test_queue.pending_count() == 0


def test_settings_page_has_archive_panel(temp_db):
    r = _web().get("/settings")
    assert r.status_code == 200
    assert 'id="archive-settings"' in r.text
    assert "Enable archiving" in r.text
    assert "ARCHIVE_ORG_ACCESS_KEY" in r.text


def test_manifest_and_exports_include_archive(temp_db, test_queue):
    with temp_db() as conn:
        cid = s.create_case(conn, "Harbor investigation")
        iid = _seed_item(conn)
        other = _seed_item(conn, "Unarchived story", "https://news.example.org/other")
        s.add_bookmark(conn, iid, cid)
        s.add_bookmark(conn, other, cid)
        s.set_item_archive(conn, iid, SRC, "done", archive_url=SNAP, archived_at="2026-01-02T03:04:05Z")
        conn.execute(
            "INSERT INTO evidence (item_id, screenshot, sha256) VALUES (?, ?, ?)",
            (iid, "evidence/shot.png", "ab" * 32),
        )
    c = _web()
    manifest = c.get(f"/cases/{cid}/evidence-manifest")
    assert manifest.status_code == 200
    assert "INTERNET ARCHIVE (WAYBACK MACHINE) CAPTURES" in manifest.text
    assert f"archive_url : {SNAP}" in manifest.text
    assert "archived_at : 2026-01-02T03:04:05Z" in manifest.text
    assert "Unarchived story" not in manifest.text.split("CAPTURES", 1)[1]

    csv_text = c.get(f"/cases/{cid}/report", params={"format": "csv"}).text
    assert "archive_url" in csv_text.splitlines()[0] and SNAP in csv_text
    rows = json.loads(c.get(f"/cases/{cid}/report", params={"format": "json"}).text)
    by_id = {r["id"]: r for r in rows}
    assert by_id[iid]["archive_url"] == SNAP and by_id[other]["archive_url"] == ""

    html = c.get(f"/cases/{cid}/report", params={"format": "html"}).text
    assert SNAP in html and "Archived:" in html


def test_lookup_retries_without_trailing_slash(public_ok):
    """The availability API only matches some snapshots without the slash."""
    seen = []

    def handler(request):
        seen.append(request.url.params["url"])
        if request.url.params["url"].endswith("/"):
            return httpx.Response(200, json={"archived_snapshots": {}})
        return httpx.Response(200, json={"archived_snapshots": {"closest": {
            "available": True, "timestamp": "20260102030405",
            "url": "http://web.archive.org/web/20260102030405/https://example.org/news"}}})

    res = wayback.lookup("https://example.org/news/", client=_client_for(handler))
    assert res["ok"] and res["archive_url"].startswith("https://web.archive.org/web/")
    assert seen == ["https://example.org/news/", "https://example.org/news"]


def test_browser_save_url():
    assert wayback.browser_save_url("https://example.org/a") == (
        "https://web.archive.org/save/https://example.org/a")
