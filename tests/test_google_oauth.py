"""'Connect Gmail' (OAuth, send-only): PKCE, state checks, token exchange,
sending through the Gmail API, and the Settings round trip. No real network."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus import google_oauth as g

REDIRECT = "http://127.0.0.1:8000/settings/email/google/callback"


def _id_token(email: str) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc({'email': email, 'email_verified': True})}.sig"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_uses_pkce_s256_and_send_only_scope():
    url = g.start("cid.apps.googleusercontent.com", REDIRECT)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(g.AUTH_URL)
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["scope"].split() == list(g.SCOPES)
    assert "gmail.readonly" not in q["scope"] and "mail.google.com" not in q["scope"]
    assert q["access_type"] == "offline" and q["redirect_uri"] == REDIRECT


def test_finish_rejects_unknown_state():
    res = g.finish("cid", "secret", "forged-state", "code",
                   client=_client(lambda r: httpx.Response(500)))
    assert not res.ok and "expired or was not started here" in res.message


def test_finish_exchanges_code_with_verifier():
    url = g.start("cid", REDIRECT)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    seen = {}

    def handler(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.update(form)
        return httpx.Response(200, json={
            "refresh_token": "1//refresh", "access_token": "ya29.x", "expires_in": 3599,
            "scope": "https://www.googleapis.com/auth/gmail.send openid "
                     "https://www.googleapis.com/auth/userinfo.email",
            "id_token": _id_token("analyst@example.com"),
        })

    res = g.finish("cid", "secret", q["state"], "the-code", client=_client(handler))
    assert res.ok and res.refresh_token == "1//refresh" and res.email == "analyst@example.com"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(seen["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"] and seen["grant_type"] == "authorization_code"
    # The state is single-use.
    again = g.finish("cid", "secret", q["state"], "the-code", client=_client(handler))
    assert not again.ok


def test_finish_requires_the_send_scope():
    q = {k: v[0] for k, v in parse_qs(urlsplit(g.start("cid", REDIRECT)).query).items()}
    res = g.finish("cid", "secret", q["state"], "c", client=_client(
        lambda r: httpx.Response(200, json={"refresh_token": "t", "scope": "openid email"})))
    assert not res.ok and "send-only permission was not granted" in res.message


def test_send_raw_refreshes_then_sends_base64url():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3599})
        assert request.headers["authorization"] == "Bearer ya29.fresh"
        raw = json.loads(request.content)["raw"]
        assert "+" not in raw and "/" not in raw and "=" not in raw  # base64url, unpadded
        return httpx.Response(200, json={"id": "m1"})

    ok, msg = g.send_raw("cid", "secret", "1//refresh-send", b"Subject: hi\r\n\r\nbody",
                         client=_client(handler))
    assert ok, msg
    assert calls[0] == g.TOKEN_URL and calls[1] == g.SEND_URL


def test_expired_refresh_token_explains_testing_mode():
    ok, msg = g.send_raw("cid", "secret", "1//expired", b"x", client=_client(
        lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    assert not ok and "7 days" in msg


def test_loopback_only():
    assert g.is_loopback_redirect(REDIRECT)
    assert not g.is_loopback_redirect("https://evil.example/callback")


# ------------------------------------------------------------- web round trip


@pytest.fixture
def web(temp_db, tmp_path, monkeypatch):
    from nexus.config import get_settings

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    # Both the .env writer and the settings loader must use the temp file, never
    # the developer's real .env.
    monkeypatch.setattr("nexus.envstore._env_path", lambda: env)
    from nexus.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    get_settings.cache_clear()
    from nexus.web.app import app

    yield TestClient(app, base_url="http://127.0.0.1:8000"), env
    get_settings.cache_clear()


def test_connect_requires_client_credentials(web):
    client, _env = web
    r = client.get("/settings/email/google/connect", follow_redirects=False)
    assert r.status_code == 200 and "Save your Google OAuth client ID and secret first" in r.text


def test_full_connect_flow_stores_token_in_env_only(web, monkeypatch):
    from nexus.config import get_settings
    from nexus.envstore import update_env

    client, env = web
    update_env({"GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
                "GOOGLE_OAUTH_CLIENT_SECRET": "s3cret"})
    get_settings.cache_clear()

    r = client.get("/settings/email/google/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(g.AUTH_URL)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]

    monkeypatch.setattr(g, "finish", lambda cid, sec, st, code, **kw: g.OAuthResult(
        st == state, "ok", refresh_token="1//stored", email="analyst@example.com"))
    r = client.get(f"/settings/email/google/callback?state={state}&code=c", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings")
    assert "GOOGLE_OAUTH_REFRESH_TOKEN=" in env.read_text(encoding="utf-8")

    page = client.get("/settings").text
    assert "connected as analyst@example.com" in page
    assert "1//stored" not in page and "s3cret" not in page  # secrets never rendered

    monkeypatch.setattr(g, "revoke", lambda token, **kw: None)
    r = client.post("/settings/email/google/disconnect")
    assert "Gmail disconnected" in r.text
    assert "1//stored" not in env.read_text(encoding="utf-8")


def test_callback_error_is_reported(web):
    client, _env = web
    r = client.get("/settings/email/google/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 303
    assert "access_denied" in client.get("/settings").text
