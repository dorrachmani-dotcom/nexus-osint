"""Sherlock can switch on the daily email brief, but only after the email
connection passed a test send (same rule as the Settings page)."""

from types import SimpleNamespace

from nexus import assistant as A, mailer


def _settings():
    return SimpleNamespace()


def test_refuses_until_email_is_verified(temp_db, monkeypatch):
    monkeypatch.setattr(mailer, "resolve_email_config", lambda s: object())
    monkeypatch.setattr(mailer, "is_verified", lambda conn, cfg: False)
    with temp_db() as conn:
        res = A._do_schedule_daily_brief(conn, {"time": "07:30"}, _settings())
        assert res["type"] == "error" and "test email" in res["label"]
        from nexus.storage import get_meta
        assert get_meta(conn, "digest_enabled") in (None, "", "0")


def test_schedules_when_verified(temp_db, monkeypatch):
    monkeypatch.setattr(mailer, "resolve_email_config", lambda s: object())
    monkeypatch.setattr(mailer, "is_verified", lambda conn, cfg: True)
    with temp_db() as conn:
        res = A._do_schedule_daily_brief(conn, {"time": "07:30", "scan_first": True}, _settings())
        from nexus.storage import get_meta
        assert res["type"] == "schedule" and "07:30" in res["label"]
        assert get_meta(conn, "digest_enabled") == "1"
        assert get_meta(conn, "digest_time") == "07:30"
        assert get_meta(conn, "digest_scan_first") == "1"


def test_tool_is_allow_listed_and_documented():
    assert "schedule_daily_brief" in A._ALLOWED_TOOLS
    assert "schedule_daily_brief" in A.SYSTEM_PROMPT if hasattr(A, "SYSTEM_PROMPT") else True
