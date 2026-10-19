"""Setup checklist, daily-brief card and per-case daily-report status."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from nexus.setup_status import (
    DONE,
    OPTIONAL,
    TODO,
    digest_status,
    human_when,
    next_daily_run,
    setup_steps,
)


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


# --------------------------------------------------------------- timing

def test_next_run_is_today_when_the_slot_is_still_ahead():
    now = datetime(2026, 10, 19, 6, 30)
    assert next_daily_run(now, "08:00", None) == datetime(2026, 10, 19, 8, 0)


def test_next_run_is_tomorrow_once_today_was_sent():
    now = datetime(2026, 10, 19, 9, 0)
    assert next_daily_run(now, "08:00", "2026-10-19T08:00:30") == datetime(2026, 10, 20, 8, 0)


def test_next_run_is_now_when_due_and_not_sent_yet():
    now = datetime(2026, 10, 19, 9, 0)
    assert next_daily_run(now, "08:00", "2026-10-18T08:00:00") == now


def test_bad_time_falls_back_to_default():
    now = datetime(2026, 10, 19, 6, 0)
    assert next_daily_run(now, "not-a-time", None) == datetime(2026, 10, 19, 8, 0)


def test_human_when():
    now = datetime(2026, 10, 19, 12, 0)
    assert human_when(datetime(2026, 10, 19, 8, 0), now) == "today at 08:00"
    assert human_when(datetime(2026, 10, 20, 8, 0), now) == "tomorrow at 08:00"
    assert human_when(datetime(2026, 10, 18, 8, 0), now) == "yesterday at 08:00"
    assert human_when(datetime(2026, 10, 15, 8, 5), now) == "Oct 15 at 08:05"
    assert human_when(None, now) == "never"


# --------------------------------------------------------- digest status

def test_digest_status_blocked_until_email_verified():
    st = digest_status(now=datetime(2026, 10, 19, 6, 0), enabled=True, verified=False,
                       daily_time="07:30", last_sent=None, last_status=None)
    assert st["active"] is False and st["next_run"] == ""
    assert "email" in st["blocked_reason"].lower()
    assert st["time"] == "07:30" and st["last_sent"] == "never"


def test_digest_status_active_reports_next_and_last():
    st = digest_status(now=datetime(2026, 10, 19, 9, 0), enabled=True, verified=True,
                       daily_time="08:00", last_sent="2026-10-19T08:00:10",
                       last_status="ok: sent")
    assert st["active"] is True
    assert st["next_run"] == "tomorrow at 08:00"
    assert st["last_sent"] == "today at 08:00"
    assert st["error"] == ""


def test_digest_status_surfaces_last_error():
    st = digest_status(now=datetime(2026, 10, 19, 9, 0), enabled=True, verified=True,
                       daily_time="08:00", last_sent=None,
                       last_status="error: SMTP login failed")
    assert st["error"] == "SMTP login failed"
    assert st["next_run"] == "within a minute"


# --------------------------------------------------------- checklist

def _steps(**over):
    base = {
        "status": {"ai": "off", "gnews": "off", "gdelt": "off", "reddit": "off", "rss": "off"},
        "active_provider": "off", "email_ready": False, "email_verified": False,
        "email_label": "Gmail", "digest": {"active": False, "time": "08:00", "next_run": ""},
        "tracked_cases": 0, "report_cases": 0,
    }
    base.update(over)
    return {s["key"]: s for s in setup_steps(**base)}


def test_fresh_install_checklist():
    st = _steps()
    assert list(st) == ["sources", "ai", "email", "brief", "reports"]
    assert st["sources"]["state"] == TODO
    assert st["ai"]["state"] == OPTIONAL
    assert st["email"]["state"] == OPTIONAL
    assert st["brief"]["state"] == OPTIONAL and "step 3" in st["brief"]["line"]
    assert st["reports"]["state"] == OPTIONAL
    assert all(s["button"] and s["href"] for s in st.values())


def test_email_saved_but_untested_is_a_todo():
    st = _steps(email_ready=True)
    assert st["email"]["state"] == TODO and "test" in st["email"]["line"].lower()


def test_fully_set_up_checklist():
    st = _steps(
        status={"gemini": "on", "gnews": "on", "gdelt": "on", "reddit": "on", "rss": "off"},
        active_provider="gemini", email_ready=True, email_verified=True,
        digest={"active": True, "time": "07:30", "next_run": "tomorrow at 07:30"},
        tracked_cases=2, report_cases=1,
    )
    assert {s["state"] for s in st.values()} == {DONE}
    assert "Gemini" in st["ai"]["line"]
    assert "07:30" in st["brief"]["line"] and "tomorrow" in st["brief"]["line"]
    assert "1 case " in st["reports"]["line"]


# --------------------------------------------------------- web

def _case(conn, name="Acme watch"):
    from nexus.storage import add_case_term, create_case

    cid = create_case(conn, name, None, "medium")
    add_case_term(conn, cid, "acme")
    return cid


def test_setup_page_renders_checklist_and_cards(temp_db):
    with temp_db() as conn:
        _case(conn)
    with temp_db():
        html = _client().get("/setup").text
    for key in ("sources", "ai", "email", "brief", "reports"):
        assert f'id="step-{key}"' in html
    assert 'id="daily-brief"' in html and 'id="case-reports-list"' in html
    assert "Acme watch" in html
    # Email not verified: the switch is disabled and points at step 3.
    card = html[html.index('id="daily-brief"'):]
    assert 'id="digest-enabled"' in card
    assert "disabled" in card[card.index('id="digest-enabled"') - 200:card.index('id="digest-enabled"') + 200]
    assert "Connect email (step 3)" in card


def test_setup_is_in_the_setup_menu(temp_db):
    with temp_db():
        html = _client().get("/cases").text
    assert 'href="/setup"' in html and "Setup checklist" in html


def test_digest_card_view_returns_only_the_card(temp_db):
    r = _client().post("/settings/digest", data={"enabled": "", "digest_time": "06:45", "view": "card"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith("{#") is False
    assert 'id="daily-brief"' in r.text and 'id="email-settings"' not in r.text
    assert "06:45" in r.text and "daily email brief is off" in r.text


def test_digest_card_shows_next_run_when_active(temp_db, monkeypatch):
    monkeypatch.setattr("nexus.mailer.is_verified", lambda conn, cfg: True)
    with temp_db() as conn:
        from nexus.storage import set_meta

        set_meta(conn, "digest_enabled", "1")
        set_meta(conn, "digest_time", "07:15")
    with temp_db():
        html = _client().get("/setup").text
    card = html[html.index('id="daily-brief"'):]
    nxt = card[card.index('id="digest-next"'):card.index('id="digest-next"') + 120]
    assert "07:15" in nxt or "within a minute" in nxt
    assert "not scheduled" not in nxt


def test_setup_report_row_toggle_saves_config(temp_db):
    with temp_db() as conn:
        cid = _case(conn)
    r = _client().post(f"/cases/{cid}/reports/settings",
                       data={"enabled": "1", "format": "html", "email": "attach", "view": "row"})
    assert r.status_code == 200
    assert f'id="report-row-{cid}"' in r.text and "Saved" in r.text
    with temp_db() as conn:
        from nexus import daily_reports as dr

        assert dr.get_config(conn, cid) == {"enabled": True, "format": "html", "email": "attach"}


def test_case_reports_tab_shows_status_lines(temp_db):
    with temp_db() as conn:
        cid = _case(conn)
        from nexus import daily_reports as dr

        dr.set_config(conn, cid, enabled=True, fmt="pdf", email="link")
    with temp_db():
        html = _client().get(f"/cases/{cid}/reports").text
    assert 'id="report-status"' in html
    assert "Next report" in html and "Last report" in html and "never" in html
    # Daily brief is off, so the email line explains how to get it by email.
    assert "Turn on the daily brief" in html and 'href="/setup#daily-brief"' in html
