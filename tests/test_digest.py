"""Daily email brief by Sherlock: timing, composition, scheduling, settings routes.

No mail is ever sent (send_email is faked) and no model is called (a fake
provider stands in for Sherlock).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nexus import digest, storage as s
from nexus.models import RawItem

# --- digest_due ----------------------------------------------------------------


def _at(h, m=0, day=8):
    return datetime(2026, 10, day, h, m)


@pytest.mark.parametrize(
    "now, dtime, last, expected",
    [
        (_at(7, 59), "08:00", None, False),            # before the set time
        (_at(8, 0), "08:00", None, True),              # exactly at the time
        (_at(23, 0), "08:00", "", True),               # never sent, late in the day
        (_at(9), "08:00", _at(8, 1).isoformat(), False),     # already sent today
        (_at(9), "08:00", _at(8, 1, day=7).isoformat(), True),  # sent yesterday
        (_at(9), "08:00", _at(10, day=9).isoformat(), False),  # clock skew: future
        (_at(9), "08:00", "not-a-date", True),         # junk = never sent
        (_at(9), "bad", None, True),                   # invalid time -> 08:00
        (_at(7), "bad", None, False),
        # Sent at 01:00 (time since moved to 23:15): still at most once per day.
        (_at(23, 30), "23:15", _at(1).isoformat(), False),
        (_at(0, 5), "00:00", _at(23, 59, day=7).isoformat(), True),  # midnight rollover
    ],
)
def test_digest_due(now, dtime, last, expected):
    assert digest.digest_due(now, dtime, last) is expected


def test_digest_due_handles_aware_utc_last_sent():
    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    sent_today = now.replace(hour=9).astimezone().isoformat()
    assert digest.digest_due(now, "08:00", sent_today) is False
    sent_yesterday = (now - timedelta(days=1)).isoformat()
    assert digest.digest_due(now, "08:00", sent_yesterday) is True


def test_parse_digest_time():
    assert digest.parse_digest_time("07:30") == (7, 30)
    assert digest.parse_digest_time("24:00") == (8, 0)
    assert digest.parse_digest_time(None) == (8, 0)


# --- composition -------------------------------------------------------------

EVIL = '<script>alert(1)</script> Harbor "closure"'


def _seed(conn) -> int:
    cid = s.create_case(conn, "Harbor Watch")
    s.add_case_term(conn, cid, "Harbor")
    s.upsert_item(conn, RawItem(source="rss", title=EVIL, content="Harbor update",
                                url="https://news.example.com/a"))
    s.upsert_item(conn, RawItem(source="rss", title="Harbor dredging plan", content="Harbor",
                                url="javascript:alert(2)"))
    s.upsert_item(conn, RawItem(source="rss", title="Unrelated weather", content="rain"))
    return cid


def _since():
    return (datetime.now().astimezone() - timedelta(hours=24)).isoformat()


def test_collect_counts_case_items_and_totals(temp_db):
    with temp_db() as conn:
        cid = _seed(conn)
        data = digest.collect_digest_data(conn, _since())
    assert data["total_new"] == 3
    assert data["cases"][0]["case_id"] == cid and data["cases"][0]["new_count"] == 2
    assert data["threat_counts"] == {"unscored": 3}


def test_collect_ignores_items_collected_before_since(temp_db):
    with temp_db() as conn:
        _seed(conn)
        future = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
        data = digest.collect_digest_data(conn, future)
    assert data["total_new"] == 0 and data["cases"] == []


def test_case_without_terms_never_reports_the_whole_archive(temp_db):
    with temp_db() as conn:
        s.create_case(conn, "Empty")
        s.upsert_item(conn, RawItem(source="rss", title="anything", content="x"))
        assert s.case_items_collected_since(conn, 1, _since()) == (0, [])


def test_fallback_brief_without_ai_escapes_untrusted_text(temp_db, monkeypatch):
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: None)
    with temp_db() as conn:
        _seed(conn)
        data = digest.collect_digest_data(conn, _since())
    msg = digest.compose_digest(object(), data)
    assert msg["author"] == "Nexus (no AI connected)"
    assert msg["subject"].startswith("Nexus-OSINT daily brief — ") and "3 new items" in msg["subject"]
    assert "HEADLINE:" in msg["text"] and "Harbor Watch" in msg["text"]
    html = msg["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="https://news.example.com/a"' in html
    assert "javascript:" not in html  # non-http links are never rendered as links
    assert "http://127.0.0.1:8000/cases/" in html and "/items/" in html


class FakeSherlock:
    name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat(self, system_prompt, user_prompt, max_tokens=1024):
        self.prompts.append((system_prompt, user_prompt))
        return self.reply


def test_ai_brief_is_grounded_and_escaped(temp_db, monkeypatch):
    fake = FakeSherlock('HEADLINE: Harbor <b>closure</b> reported\n\nKEY DEVELOPMENTS\n- Harbor Watch: "x" [#1]')
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        _seed(conn)
        data = digest.collect_digest_data(conn, _since())
    msg = digest.compose_digest(object(), data)
    assert msg["author"] == "Sherlock (fake)"
    system, user = fake.prompts[0]
    assert "ONLY the supplied items" in system and "untrusted" in system
    assert "Harbor dredging plan" in user and "Unrelated weather" not in user
    assert "Harbor &lt;b&gt;closure&lt;/b&gt; reported" in msg["html"]
    assert "Harbor <b>closure</b> reported" in msg["text"]  # plain text stays plain


def test_ai_failure_falls_back(temp_db, monkeypatch):
    class Broken:
        name = "broken"

        def chat(self, *a, **k):
            raise RuntimeError("quota")

    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: Broken())
    with temp_db() as conn:
        _seed(conn)
        data = digest.collect_digest_data(conn, _since())
    assert digest.compose_digest(object(), data)["author"] == "Nexus (no AI connected)"


# --- scheduling --------------------------------------------------------------


def _gmail_env(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("SMTP_USERNAME", "analyst@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password-123")
    monkeypatch.setenv("DIGEST_TO", "analyst@example.com")
    from nexus.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def sent(monkeypatch):
    from nexus.mailer import SendResult

    box: list[dict] = []

    def fake_send(cfg, subject, text, html=None, attachments=None):
        box.append({"subject": subject, "text": text, "html": html, "attachments": attachments or []})
        return SendResult(True, "Sent via Gmail to 1 recipient(s).")

    monkeypatch.setattr("nexus.mailer.send_email", fake_send)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: None)
    return box


def _local(h):
    return datetime.now().astimezone().replace(hour=h, minute=0, second=0, microsecond=0)


def _verify(conn, settings, enabled="1"):
    from nexus.mailer import mark_verified, resolve_email_config

    mark_verified(conn, resolve_email_config(settings))
    s.set_meta(conn, "digest_enabled", enabled)
    s.set_meta(conn, "digest_time", "08:00")


def test_scheduled_send_once_per_day_after_time(temp_db, monkeypatch, sent):
    settings = _gmail_env(monkeypatch)
    with temp_db() as conn:
        _seed(conn)
        _verify(conn, settings)
    assert digest.run_daily_jobs(settings, None, now=_local(7))["email"] is None
    assert sent == []
    assert digest.run_daily_jobs(settings, None, now=_local(9))["email"] is True
    assert len(sent) == 1 and "daily brief" in sent[0]["subject"]
    assert digest.run_daily_jobs(settings, None, now=_local(10))["email"] is None
    assert len(sent) == 1
    with temp_db() as conn:
        assert s.get_meta(conn, "last_digest_sent_at")
        assert s.get_meta(conn, "last_digest_status").startswith("ok")


def test_no_send_when_not_verified_or_disabled(temp_db, monkeypatch, sent):
    settings = _gmail_env(monkeypatch)
    with temp_db() as conn:
        s.set_meta(conn, "digest_enabled", "1")  # enabled but never verified
    digest.run_daily_jobs(settings, None, now=_local(9))
    with temp_db() as conn:
        _verify(conn, settings, enabled="0")
    digest.run_daily_jobs(settings, None, now=_local(9))
    assert sent == []


def test_failed_send_is_not_retried_every_minute(temp_db, monkeypatch):
    from nexus.mailer import SendResult

    calls = []
    monkeypatch.setattr("nexus.mailer.send_email",
                        lambda *a, **k: calls.append(1) or SendResult(False, "server down"))
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: None)
    settings = _gmail_env(monkeypatch)
    with temp_db() as conn:
        _verify(conn, settings)
    digest.run_daily_jobs(settings, None, now=_local(9))
    digest.run_daily_jobs(settings, None, now=_local(9) + timedelta(minutes=1))
    assert len(calls) == 1
    with temp_db() as conn:
        assert s.get_meta(conn, "last_digest_status").startswith("error")
        assert not s.get_meta(conn, "last_digest_sent_at")


def test_scan_first_runs_collector(temp_db, monkeypatch, sent):
    settings = _gmail_env(monkeypatch)
    with temp_db() as conn:
        _verify(conn, settings)
        s.set_meta(conn, "digest_scan_first", "1")

    class C:
        scanned = 0

        def scan(self):
            C.scanned += 1

    ran = digest.run_daily_jobs(settings, None, C(), now=_local(9))
    assert ran["scan"] is True and C.scanned == 1 and len(sent) == 1


def test_background_loops_are_plain_functions_and_lifespan_is_a_context_manager():
    # Regression: @asynccontextmanager once decorated the auto-scan loop instead
    # of lifespan, so the scheduler thread returned immediately and never ran.
    from nexus.web import app as app_mod

    assert not hasattr(app_mod._auto_scan_loop, "__wrapped__")
    assert not hasattr(app_mod._daily_jobs_loop, "__wrapped__")
    assert hasattr(app_mod.lifespan, "__wrapped__")


# --- settings routes -----------------------------------------------------------


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_settings_page_renders_email_section_without_secrets(temp_db, monkeypatch):
    _gmail_env(monkeypatch)
    html = _client().get("/settings").text
    assert "Daily email brief (by Sherlock)" in html
    assert "app-password-123" not in html
    assert "configured &middot; not tested" in html


def test_email_save_writes_env_and_clears_verification(temp_db, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr("nexus.envstore._env_path", lambda: env)
    settings = _gmail_env(monkeypatch)
    with temp_db() as conn:
        _verify(conn, settings)
    r = _client().post("/settings/email", data={
        "SMTP_USERNAME": "analyst@example.com", "SMTP_PASSWORD": "brand-new-secret",
        "DIGEST_TO": "a@example.com,b@example.org",
    })
    assert r.status_code == 200 and "Saved" in r.text
    assert "brand-new-secret" not in r.text
    text = env.read_text(encoding="utf-8")
    assert "SMTP_PASSWORD=brand-new-secret" in text and "DIGEST_TO" in text
    with temp_db() as conn:
        assert s.get_meta(conn, "email_verified") is None
        assert s.get_meta(conn, "digest_enabled") == "0"


def test_email_save_rejects_bad_input(temp_db, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr("nexus.envstore._env_path", lambda: env)
    r = _client().post("/settings/email", data={"DIGEST_TO": "not-an-address", "SMTP_PORT": "99999"})
    assert "Not saved" in r.text
    assert not env.exists()


def test_test_email_marks_verified_then_digest_can_be_enabled(temp_db, monkeypatch, sent):
    _gmail_env(monkeypatch)
    c = _client()
    r = c.post("/settings/digest", data={"enabled": "1", "digest_time": "07:30"})
    assert "Send test email" in r.text and "first" in r.text  # refused: not verified
    r = c.post("/settings/email/test")
    assert r.status_code == 200 and "verified" in r.text
    assert sent and sent[0]["subject"] == "Nexus-OSINT: test email"
    r = c.post("/settings/digest", data={"enabled": "1", "digest_time": "07:30", "scan_first": "1"})
    assert "07:30" in r.text
    with temp_db() as conn:
        assert s.get_meta(conn, "digest_enabled") == "1"
        assert s.get_meta(conn, "digest_time") == "07:30"
        assert s.get_meta(conn, "digest_scan_first") == "1"


def test_test_brief_route(temp_db, monkeypatch, sent):
    _gmail_env(monkeypatch)
    with temp_db() as conn:
        _seed(conn)
    r = _client().post("/settings/digest/test")
    assert r.status_code == 200 and "Test brief sent" in r.text
    assert sent[0]["subject"].startswith("[Test] Nexus-OSINT daily brief")


def test_test_brief_without_email_is_a_friendly_error(temp_db):
    r = _client().post("/settings/digest/test")
    assert r.status_code == 200 and "Set up the email connection" in r.text


def test_email_provider_card_switch(temp_db):
    r = _client().post("/settings/email/provider", data={"provider": "resend"})
    assert "Resend API key" in r.text
    r = _client().post("/settings/email/provider", data={"provider": "carrier-pigeon"})
    assert "Unknown email provider" in r.text
