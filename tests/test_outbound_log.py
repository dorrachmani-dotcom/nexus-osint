"""Outbound activity log (nexus/security/outbound.py, /security/outbound)."""

from __future__ import annotations

import contextlib

import httpx
from fastapi.testclient import TestClient

from nexus.security import outbound

SECRET_PROMPT = "TOP-SECRET-ITEM-TEXT about Harbor Corp"


def _rows(get_conn, **kw):
    with get_conn() as conn:
        return outbound.query(conn, **kw)


def test_record_and_query_with_filters(temp_db):
    outbound.record("ai", "gemini (m)", "complete", items=1, bytes_out=10)
    outbound.record("email", "SMTP", "send", items=2, ok=False)
    assert len(_rows(temp_db)) == 2
    assert [r["category"] for r in _rows(temp_db, category="email")] == ["email"]
    assert [r["ok"] for r in _rows(temp_db, status="failed")] == [0]
    with temp_db() as conn:
        assert {t["target"] for t in outbound.summary(conn)} == {"gemini (m)", "SMTP"}


def test_record_never_raises_and_scrubs(temp_db, monkeypatch):
    outbound.record("source", "https://api.example.org/x?api_key=abc123secret", "fetch",
                    detail="token=abcdef1234 failed")
    row = _rows(temp_db)[0]
    assert "abc123secret" not in row["target"] and "abcdef1234" not in row["detail"]
    monkeypatch.setattr("nexus.db.get_connection", lambda: (_ for _ in ()).throw(RuntimeError))
    outbound.record("ai", "x", "y")  # must not raise


def test_ai_calls_logged_without_content(temp_db):
    from nexus.analysis.providers import LLMProvider

    class Fake(LLMProvider):
        name = "fakecloud"

        def complete(self, system_prompt, user_prompt, max_tokens=1024):
            return "reply text"

    class FakeChat(Fake):
        def chat(self, system_prompt, user_prompt, max_tokens=1024):
            return self.complete(system_prompt, user_prompt, max_tokens)

    Fake("m1").complete("sys", SECRET_PROMPT)
    FakeChat("m2").chat("sys", SECRET_PROMPT)
    rows = _rows(temp_db, category="ai")
    assert len(rows) == 2  # chat() calling complete() is one event
    assert rows[1]["bytes_out"] == len(("sys" + SECRET_PROMPT).encode()) and rows[1]["bytes_in"] == 10
    with temp_db() as conn:
        dump = " ".join(str(v) for r in conn.execute("SELECT * FROM outbound_log") for v in tuple(r))
    assert "SECRET" not in dump and "Harbor" not in dump and "reply text" not in dump


def test_failed_ai_call_logged_as_failed(temp_db):
    from nexus.analysis.providers import LLMProvider

    class Boom(LLMProvider):
        name = "boom"

        def complete(self, system_prompt, user_prompt, max_tokens=1024):
            raise RuntimeError("down")

    with contextlib.suppress(RuntimeError):
        Boom("m").complete("s", "u")
    assert _rows(temp_db)[0]["ok"] == 0


def test_wayback_lookup_logged_without_url(temp_db):
    from nexus import wayback

    def handler(request):
        return httpx.Response(200, json={"archived_snapshots": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        wayback.lookup("https://news.example.com/private-story", client=client)
    row = _rows(temp_db)[0]
    assert row["category"] == "archive" and row["target"] == "web.archive.org"
    assert "private-story" not in str(row)


def test_email_send_logged(temp_db, monkeypatch):
    from nexus import mailer

    cfg = mailer.EmailConfig(provider="smtp", label="SMTP (mail.example.org)", kind="smtp",
                             host="mail.example.org", username="u", password="p" * 10,
                             sender="n@example.org",
                             recipients=["a@example.org", "b@example.org"])
    assert cfg.ready
    monkeypatch.setattr(mailer, "resolve_email_config", lambda s: cfg)
    monkeypatch.setattr(mailer, "_build_message", lambda *a, **k: object())
    monkeypatch.setattr(mailer, "_send_smtp", lambda c, m: mailer.SendResult(True, "ok"))
    mailer.send_email(object(), "Subject " + SECRET_PROMPT, SECRET_PROMPT)
    row = _rows(temp_db)[0]
    assert row["category"] == "email" and row["items"] == 2 and "SECRET" not in str(row)


def test_source_fetch_summary(temp_db):
    from nexus.collector import _log_fetches

    _log_fetches({"rss": {"fetched": 12, "new": 3}, "reddit": {"error": "https://x?key=1"},
                  "_analysis": {"done": 1}})
    rows = {r["target"]: r for r in _rows(temp_db)}
    assert rows["rss"]["items"] == 12 and rows["rss"]["ok"] == 1
    assert rows["reddit"]["ok"] == 0 and rows["reddit"]["detail"] == ""
    assert "_analysis" not in rows


def test_publish_logged_only_for_off_machine_targets(temp_db):
    from nexus.publish.service import Prepared, _log_publish

    def prep(target):
        return Prepared("item", 1, target, 1, "create", "Title " + SECRET_PROMPT, "body", "dest")

    _log_publish(None, prep("confluence"), "create", ok=True, url="https://wiki.example.org/pages/1")
    _log_publish(None, prep("obsidian"), "note", ok=True)
    _log_publish(None, prep("pool"), "pool", ok=True)
    rows = _rows(temp_db)
    assert {r["target"] for r in rows} == {"wiki.example.org", "Obsidian vault"}
    assert "SECRET" not in str(rows)


def test_csv_neutralises_formulas():
    text = outbound.to_csv([{"ts": "t", "category": "source", "target": "=cmd()", "action": "+x",
                             "items": 1, "bytes_out": 0, "bytes_in": 0, "ok": 1, "detail": "@a"}])
    assert "'=cmd()" in text and "'+x" in text and "'@a" in text


def test_page_and_csv_routes(temp_db):
    from nexus.web.app import app

    outbound.record("virustotal", "virustotal.com", "hash lookup", items=1)
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.get("/security/outbound?category=virustotal&days=7")
    assert r.status_code == 200 and "virustotal.com" in r.text
    r = client.get("/security/outbound?category=<b>zz</b>&days=-5&status=x")
    assert r.status_code == 200 and "<b>zz</b>" not in r.text
    r = client.get("/security/outbound.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "virustotal.com" in r.text
    assert "/security/outbound" in client.get("/security").text
