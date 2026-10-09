"""Scheduled daily case reports: generation, scheduling, the case Reports tab,
serving (view / download), deleting, and path-traversal safety.

The PDF renderer is mocked so tests are fast and need no PDF backend.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from nexus import daily_reports as dr
from nexus import storage as s
from nexus.models import RawItem


@pytest.fixture(autouse=True)
def fake_pdf(monkeypatch):
    monkeypatch.setattr("nexus.reporting.render_report_pdf", lambda html, settings=None: b"%PDF-1.4 fake")


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _seed(conn) -> int:
    cid = s.create_case(conn, "Harbor Watch / Q4")
    s.add_case_term(conn, cid, "Harbor")
    s.upsert_item(conn, RawItem(source="rss", title="Harbor <b>update</b>", content="Harbor"))
    return cid


def _settings():
    from nexus.config import get_settings

    return get_settings()


# --- unit: slugs, paths, config -------------------------------------------------


def test_slug_and_validation():
    assert dr.case_slug({"id": 7, "name": "Harbor Watch / Q4"}) == "case-7-harbor-watch-q4"
    assert dr.case_slug({"id": 7, "name": "!!!"}) == "case-7"
    assert dr.valid_slug("case-7-harbor-watch-q4")
    for bad in ("../case-7", "case-7/..", "case-x", "Case-7", "case-7-", ""):
        assert not dr.valid_slug(bad)
    assert dr.valid_filename("2026-10-08.pdf") and dr.valid_filename("2026-10-08.html")
    for bad in ("../2026-10-08.pdf", "2026-10-08.json", "2026-10-08.pdf.exe", "x.pdf", "..%2Fdb"):
        assert not dr.valid_filename(bad)


def test_resolve_blocks_traversal(temp_db, tmp_path):
    settings = _settings()
    (tmp_path / "reports" / "case-1").mkdir(parents=True)
    (tmp_path / "reports" / "case-1" / "2026-10-08.pdf").write_bytes(b"x")
    (tmp_path / "secret.pdf").write_bytes(b"y")
    assert dr.resolve_report_path(settings, "case-1", "2026-10-08.pdf") is not None
    assert dr.resolve_report_path(settings, "..", "secret.pdf") is None
    assert dr.resolve_report_path(settings, "case-1", "../../secret.pdf") is None
    assert dr.resolve_report_path(settings, "case-1", "2026-10-09.pdf") is None  # missing


def test_config_roundtrip_and_enabled_cases(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        assert dr.get_config(conn, cid) == dr.DEFAULT_CONFIG
        dr.set_config(conn, cid, enabled=True, fmt="html", email="attach")
        assert dr.get_config(conn, cid) == {"enabled": True, "format": "html", "email": "attach"}
        dr.set_config(conn, 999, enabled=True, fmt="weird", email="weird")  # no such case
        assert dr.enabled_case_ids(conn) == [cid]
        assert dr.get_config(conn, 999)["format"] == "pdf"


# --- generation ----------------------------------------------------------------


def test_generate_writes_pdf_and_sidecar(temp_db, tmp_path):
    with temp_db() as conn:
        cid = _seed(conn)
        info = dr.generate_case_report(_settings(), _templates(), conn, cid)
    assert info["format"] == "pdf" and info["items"] == 1
    path = tmp_path / "reports" / info["slug"] / info["filename"]
    assert path.read_bytes().startswith(b"%PDF")
    assert (path.parent / f"{info['date']}.json").is_file()
    listed = dr.list_reports(_settings(), case_id=cid)
    assert listed[0]["items"] == 1 and listed[0]["case_name"] == "Harbor Watch / Q4"


def test_falls_back_to_html_without_pdf_backend(temp_db, monkeypatch):
    monkeypatch.setattr("nexus.reporting.render_report_pdf", lambda html, settings=None: None)
    with temp_db() as conn:
        cid = _seed(conn)
        info = dr.generate_case_report(_settings(), _templates(), conn, cid)
    assert info["format"] == "html" and info["filename"].endswith(".html")


def test_second_run_only_counts_new_items(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        dr.generate_case_report(_settings(), _templates(), conn, cid)
        again = dr.generate_case_report(_settings(), _templates(), conn, cid)
    assert again["items"] == 0


def test_scheduler_generates_for_enabled_cases_once_a_day(temp_db, monkeypatch):
    from nexus import digest

    with temp_db() as conn:
        cid = _seed(conn)
        dr.set_config(conn, cid, enabled=True, fmt="pdf", email="link")
        s.set_meta(conn, "digest_time", "08:00")
    nine = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
    seven = nine.replace(hour=7)
    assert digest.run_daily_jobs(_settings(), _templates(), now=seven)["reports"] == []
    ran = digest.run_daily_jobs(_settings(), _templates(), now=nine)
    assert [r["case_id"] for r in ran["reports"]] == [cid]
    assert digest.run_daily_jobs(_settings(), _templates(), now=nine.replace(hour=10))["reports"] == []


def _templates():
    from nexus.web.app import TEMPLATES

    return TEMPLATES


# --- routes: tab, generate, view, download, delete, traversal ----------------


def test_reports_tab_and_generate_now(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
    c = _client()
    page = c.get(f"/cases/{cid}?tab=reports")
    assert page.status_code == 200 and f'hx-get="/cases/{cid}/reports"' in page.text
    tab = c.get(f"/cases/{cid}/reports")
    assert "No reports yet" in tab.text
    r = c.post(f"/cases/{cid}/reports/generate")
    assert r.status_code == 200 and "saved" in r.text and ".pdf" in r.text
    r = c.post(f"/cases/{cid}/reports/settings", data={"enabled": "1", "format": "html", "email": "attach"})
    assert "scheduled" in r.text
    assert c.get("/cases/99999/reports").status_code == 404


def test_view_download_delete(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        info = dr.generate_case_report(_settings(), _templates(), conn, cid)
    c = _client()
    url = f"/cases/{cid}/reports/{info['filename']}"
    view = c.get(url)
    assert view.status_code == 200 and view.headers["content-type"] == "application/pdf"
    assert view.headers["content-disposition"].startswith("inline")
    dl = c.get(url + "?download=1")
    assert dl.headers["content-disposition"].startswith("attachment")
    # The brief page shows a "Latest reports" strip linking to it.
    assert url in c.get("/brief").text
    r = c.post(url + "/delete")
    assert "Report deleted" in r.text
    assert c.get(url).status_code == 404
    assert dr.list_reports(_settings()) == []


def test_html_report_served_inline_with_escaped_content(temp_db, monkeypatch):
    monkeypatch.setattr("nexus.reporting.render_report_pdf", lambda html, settings=None: None)
    with temp_db() as conn:
        cid = _seed(conn)
        info = dr.generate_case_report(_settings(), _templates(), conn, cid)
    r = _client().get(f"/cases/{cid}/reports/{info['filename']}")
    assert r.headers["content-type"].startswith("text/html")
    assert "<b>update</b>" not in r.text


@pytest.mark.parametrize("bad", [
    "..%2F..%2Ftest.db", "%2E%2E%2Ftest.db", "2026-10-08.json", "test.db",
    "..\\..\\test.db", "2026-10-08.pdf%00.txt",
])
def test_traversal_and_other_files_are_refused(temp_db, bad):
    with temp_db() as conn:
        cid = _seed(conn)
        dr.generate_case_report(_settings(), _templates(), conn, cid)
    c = _client()
    r = c.get(f"/cases/{cid}/reports/{bad}")
    assert r.status_code == 404
    assert c.post(f"/cases/{cid}/reports/{bad}/delete").status_code in (200, 404, 405)
    assert dr.list_reports(_settings())  # nothing was deleted


def test_other_cases_report_is_not_reachable(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        other = s.create_case(conn, "Other")
        info = dr.generate_case_report(_settings(), _templates(), conn, cid)
    r = _client().get(f"/cases/{other}/reports/{info['filename']}")
    assert r.status_code == 404
