"""Email sending for the daily brief: SMTP (SSL / STARTTLS) and email APIs.

smtplib and httpx are replaced by fakes — nothing is ever sent. Also checks
that the password / API key never reaches the logs or a returned message.
"""

from __future__ import annotations

import logging
import smtplib
from typing import ClassVar

import pytest

from nexus import mailer
from nexus.config import Settings

PASSWORD = "abcd efgh ijkl mnop"
API_KEY = "re_test_key_0123456789abcdef"


@pytest.fixture(autouse=True)
def no_meta(monkeypatch):
    monkeypatch.setattr("nexus.storage.get_meta_value", lambda k, d=None: d)
    for env in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
                "DIGEST_TO", "RESEND_API_KEY", "SENDGRID_API_KEY", "EMAIL_PROVIDER"):
        monkeypatch.delenv(env, raising=False)


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    starttls_supported = True
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent = None
        self.login_args = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return self.starttls_supported and name == "starttls"

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, pw):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
        self.login_args = (user, pw)
        self.calls.append("login")

    def send_message(self, msg):
        self.sent = msg
        self.calls.append("send")

    def quit(self):
        self.calls.append("quit")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.starttls_supported = True
    FakeSMTP.fail_login = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _gmail(**kw):
    base = {"email_provider": "gmail", "smtp_username": "analyst@example.com",
            "smtp_password": PASSWORD, "digest_to": "analyst@example.com, team@example.org"}
    base.update(kw)
    return _s(**base)


# --- config ------------------------------------------------------------------


def test_resolve_gmail_preset_and_missing_list():
    cfg = mailer.resolve_email_config(_gmail())
    assert (cfg.host, cfg.port, cfg.kind) == ("smtp.gmail.com", 587, "smtp")
    assert cfg.sender == "analyst@example.com"  # defaults to the account
    assert cfg.password == PASSWORD.replace(" ", "")
    assert cfg.recipients == ["analyst@example.com", "team@example.org"]
    assert cfg.ready
    incomplete = mailer.resolve_email_config(_s(email_provider="gmail"))
    assert not incomplete.ready
    assert any("SMTP_PASSWORD" in m for m in incomplete.missing)
    assert any("DIGEST_TO" in m for m in incomplete.missing)


def test_nothing_configured_is_not_ready():
    cfg = mailer.resolve_email_config(_s())
    assert cfg.provider == "" and not cfg.ready
    assert _s().email_enabled is False


def test_provider_inferred_from_keys():
    assert mailer.resolve_email_config(_s(smtp_host="mail.example.com")).provider == "smtp"
    assert mailer.resolve_email_config(_s(resend_api_key=API_KEY)).provider == "resend"
    assert mailer.resolve_email_config(_s(sendgrid_api_key="SG.x")).provider == "sendgrid"


def test_bad_or_empty_port_falls_back_safely():
    assert mailer.resolve_email_config(_s(email_provider="smtp", smtp_host="h.example.com", smtp_port="")).port == 587
    assert mailer.resolve_email_config(_s(email_provider="smtp", smtp_host="h.example.com", smtp_port="abc")).port == 587
    assert mailer.resolve_email_config(_s(email_provider="smtp", smtp_host="h.example.com", smtp_port="465")).port == 465


def test_fingerprint_has_no_secret_and_tracks_changes():
    a = mailer.resolve_email_config(_gmail())
    b = mailer.resolve_email_config(_gmail(smtp_password="another-password"))
    assert PASSWORD.replace(" ", "") not in a.fingerprint()
    assert a.fingerprint() == b.fingerprint()  # only presence of a secret counts
    c = mailer.resolve_email_config(_gmail(digest_to="someone@example.net"))
    assert a.fingerprint() != c.fingerprint()


# --- SMTP --------------------------------------------------------------------


def test_starttls_send_multipart(fake_smtp, caplog):
    caplog.set_level(logging.DEBUG)
    r = mailer.send_email(_gmail(), "Subject\r\nBcc: evil@example.com", "plain body", "<p>html body</p>")
    assert r.ok, r.message
    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.calls[:4] == ["ehlo", "starttls", "ehlo", "login"]
    assert smtp.login_args == ("analyst@example.com", PASSWORD.replace(" ", ""))
    msg = smtp.sent
    assert msg["Subject"] == "Subject Bcc: evil@example.com"  # no header injection
    assert msg["Bcc"] is None
    assert msg.is_multipart()
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in types and "text/html" in types
    assert PASSWORD not in caplog.text and PASSWORD.replace(" ", "") not in caplog.text
    assert "plain body" not in caplog.text


def test_ssl_port_465(fake_smtp):
    s = _s(email_provider="smtp", smtp_host="mail.example.com", smtp_port="465",
           smtp_username="u@example.com", smtp_password="pw-123456", smtp_from="u@example.com",
           digest_to="u@example.com")
    assert mailer.send_email(s, "s", "t").ok
    smtp = fake_smtp.instances[0]
    assert smtp.port == 465 and "starttls" not in smtp.calls


def test_refuses_password_without_encryption(fake_smtp):
    fake_smtp.starttls_supported = False
    r = mailer.send_email(_gmail(), "s", "t")
    assert not r.ok and "STARTTLS" in r.message
    assert "login" not in fake_smtp.instances[0].calls


def test_auth_error_is_plain_english_with_gmail_hint(fake_smtp):
    fake_smtp.fail_login = True
    r = mailer.send_email(_gmail(), "s", "t")
    assert not r.ok and "App Password" in r.message
    assert PASSWORD not in r.message


def test_connection_error_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    r = mailer.send_email(_gmail(), "s", "t")
    assert not r.ok and "smtp.gmail.com:587" in r.message


def test_not_ready_returns_message():
    r = mailer.send_email(_s(email_provider="gmail"), "s", "t")
    assert not r.ok and "missing" in r.message


def test_oversized_attachment_is_left_out(fake_smtp, monkeypatch):
    monkeypatch.setattr(mailer, "MAX_ATTACHMENT_BYTES", 10)
    r = mailer.send_email(_gmail(), "s", "t", attachments=[("a.pdf", b"x" * 50, "application/pdf")])
    assert r.ok and "too large" in r.message


# --- email APIs --------------------------------------------------------------


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


def test_resend_payload(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return _Resp(200, {"id": "x"})

    monkeypatch.setattr("httpx.post", fake_post)
    s = _s(email_provider="resend", resend_api_key=API_KEY, smtp_from="brief@example.com",
           digest_to="analyst@example.com")
    r = mailer.send_email(s, "Hi", "text", "<b>html</b>", [("r.pdf", b"%PDF", "application/pdf")])
    assert r.ok
    assert seen["url"] == "https://api.resend.com/emails"
    assert seen["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert seen["json"]["to"] == ["analyst@example.com"]
    assert seen["json"]["html"] == "<b>html</b>" and seen["json"]["text"] == "text"
    assert seen["json"]["attachments"][0]["filename"] == "r.pdf"


def test_sendgrid_payload_and_errors(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json)
        return _Resp(202)

    monkeypatch.setattr("httpx.post", fake_post)
    s = _s(email_provider="sendgrid", sendgrid_api_key="SG.secret-key-value", smtp_from="brief@example.com",
           digest_to="analyst@example.com")
    assert mailer.send_email(s, "Hi", "text", "<b>h</b>").ok
    assert seen["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert seen["json"]["personalizations"][0]["to"] == [{"email": "analyst@example.com"}]
    assert [c["type"] for c in seen["json"]["content"]] == ["text/plain", "text/html"]

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(401))
    r = mailer.send_email(s, "Hi", "text")
    assert not r.ok and "rejected the API key" in r.message
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(
        403, {"errors": [{"message": "sender SG.secret-key-value not verified"}]}))
    r = mailer.send_email(s, "Hi", "text")
    assert not r.ok and "SG.secret-key-value" not in r.message


def test_api_network_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("dns")

    monkeypatch.setattr("httpx.post", boom)
    s = _s(email_provider="resend", resend_api_key=API_KEY, smtp_from="brief@example.com",
           digest_to="analyst@example.com")
    r = mailer.send_email(s, "Hi", "text")
    assert not r.ok and "Could not reach Resend" in r.message


def test_egress_allows_configured_email_host(monkeypatch):
    from nexus.security import egress

    monkeypatch.setattr("nexus.config.get_settings", _gmail)
    egress._allow_cache = None
    try:
        assert egress._configured_allow().get("smtp.gmail.com") == "Email (your SMTP server)"
        monkeypatch.setattr("nexus.config.get_settings", lambda: _s(
            email_provider="sendgrid", sendgrid_api_key="SG.x"))
        egress._allow_cache = None
        assert egress._configured_allow().get("api.sendgrid.com") == "Email (your SendGrid API)"
    finally:
        egress._allow_cache = None
