"""The optional app password. Stdlib hashing, in-memory sessions, off by default."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nexus.envstore import read_env_value, update_env
from nexus.security import apppass
from nexus.web.routers.login import safe_next

PASSWORD = "harbor-lantern-7"


@pytest.fixture
def client(temp_db, monkeypatch):
    from nexus.web.app import app

    monkeypatch.setattr(apppass, "ITERATIONS", 1000)
    monkeypatch.setattr(apppass, "THROTTLE", apppass.UnlockThrottle())
    apppass.end_all_sessions()
    update_env({apppass.ENV_KEY: ""}, protected=frozenset({apppass.ENV_KEY}))
    yield TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)
    update_env({apppass.ENV_KEY: ""}, protected=frozenset({apppass.ENV_KEY}))
    apppass.end_all_sessions()


def _lock(password: str = PASSWORD) -> None:
    assert apppass.set_password(password)


def _sign_in(client, password: str = PASSWORD, **data):
    return client.post("/login", data={"password": password, **data})


def test_off_by_default_changes_nothing(client):
    assert not apppass.is_enabled()
    assert client.get("/").status_code == 200
    assert client.get("/login?next=/cases").headers["location"] == "/cases"


def test_hash_is_salted_and_verifies():
    one, two = apppass.hash_password(PASSWORD), apppass.hash_password(PASSWORD)
    assert one != two and PASSWORD not in one
    assert one.startswith(f"pbkdf2-sha256:{apppass.ITERATIONS}:")
    assert apppass.verify(PASSWORD, one) and not apppass.verify("wrong-password", one)


@pytest.mark.parametrize("stored", ["", "junk", "a:b:c:d", "pbkdf2-sha256:x:00:00",
                                    "pbkdf2-sha256:1000:zz:00", "md5:1000:00:00"])
def test_a_malformed_hash_matches_nothing(stored):
    assert not apppass.verify(PASSWORD, stored)


def test_an_overlong_password_is_not_hashed():
    assert not apppass.verify("x" * (apppass.MAX_LENGTH + 1), apppass.hash_password("x" * 2000))


def test_pages_redirect_and_keep_where_you_were_going(client):
    _lock()
    response = client.get("/cases?tab=open")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/cases%3Ftab%3Dopen"


def test_htmx_and_posts_get_401_not_a_page(client):
    _lock()
    partial = client.get("/feed", headers={"HX-Request": "true"})
    assert partial.status_code == 401 and partial.headers["hx-redirect"] == "/login"
    assert client.post("/cases", data={"name": "x"}).status_code == 401


def test_static_health_and_login_stay_reachable(client):
    _lock()
    assert client.get("/health").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    page = client.get("/login")
    assert page.status_code == 200 and "Sign in" in page.text


def test_sign_in_sets_a_strict_http_only_cookie(client):
    _lock()
    response = _sign_in(client, next="/cases")
    assert response.status_code == 303 and response.headers["location"] == "/cases"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/cases").status_code == 200


def test_wrong_password_is_refused(client):
    _lock()
    response = _sign_in(client, "wrong-password")
    assert response.status_code == 401 and "not correct" in response.text
    assert "set-cookie" not in response.headers


def test_wrong_passwords_back_off_even_for_the_right_one(client):
    _lock()
    for _ in range(apppass.THROTTLE.free):
        _sign_in(client, "wrong-password")
    response = _sign_in(client)
    assert response.status_code == 429 and "Too many wrong attempts" in response.text


def test_sign_out_ends_the_session(client):
    _lock()
    _sign_in(client)
    token = client.cookies[apppass.COOKIE]
    assert client.post("/logout").headers["location"] == "/login"
    assert not apppass.session_valid(token)
    client.cookies.set(apppass.COOKIE, token)
    assert client.get("/cases").status_code == 303


def test_a_forged_or_expired_session_is_refused(client, monkeypatch):
    _lock()
    client.cookies.set(apppass.COOKIE, "made-up")
    assert client.get("/cases").status_code == 303
    token = apppass.new_session()
    monkeypatch.setattr(apppass, "SESSION_SECONDS", -1)
    expired = apppass.new_session()
    assert apppass.session_valid(token) and not apppass.session_valid(expired)


def test_only_a_hash_of_the_token_is_kept(client):
    token = apppass.new_session()
    assert token not in apppass._sessions


@pytest.mark.parametrize("target, expected", [
    ("/cases?tab=open", "/cases?tab=open"), ("//evil.example", "/"), ("/\\evil.example", "/"),
    ("https://evil.example", "/"), ("evil.example", "/"), ("", "/"), ("/a\nb", "/"),
])
def test_only_local_paths_are_followed(target, expected):
    assert safe_next(target) == expected


def test_sign_in_never_redirects_off_the_app(client):
    _lock()
    assert _sign_in(client, next="//evil.example").headers["location"] == "/"


def test_setting_a_password_keeps_this_browser_signed_in(client):
    data = {"password": PASSWORD, "confirm": PASSWORD}
    assert "App password saved" in client.post("/settings/password", data=data).text
    assert apppass.is_enabled() and PASSWORD not in (read_env_value(apppass.ENV_KEY) or "")
    assert client.get("/cases").status_code == 200


@pytest.mark.parametrize("password, confirm, message", [
    ("short", "short", "at least"), (PASSWORD, "different-one", "differ"),
])
def test_bad_new_passwords_are_refused(client, password, confirm, message):
    response = client.post("/settings/password", data={"password": password, "confirm": confirm})
    assert message in response.text and not apppass.is_enabled()


def test_changing_takes_the_current_password_and_ends_other_sessions(client):
    _lock()
    _sign_in(client)
    other = apppass.new_session()
    new = "second-password-9"
    data = {"password": new, "confirm": new}
    assert "not correct" in client.post("/settings/password", data={**data, "current": "no"}).text
    assert apppass.verify(PASSWORD, read_env_value(apppass.ENV_KEY))
    response = client.post("/settings/password", data={**data, "current": PASSWORD})
    assert "saved" in response.text and apppass.verify(new, read_env_value(apppass.ENV_KEY))
    assert not apppass.session_valid(other)
    assert client.get("/cases").status_code == 200


def test_removing_takes_the_current_password(client):
    _lock()
    _sign_in(client)
    assert "not correct" in client.post("/settings/password/remove", data={"current": "no"}).text
    assert apppass.is_enabled()
    client.post("/settings/password/remove", data={"current": PASSWORD})
    assert not apppass.is_enabled()


def test_the_settings_keys_form_cannot_touch_the_password(client):
    from nexus import envstore

    assert apppass.ENV_KEY not in envstore.EDITABLE_KEYS
    update_env({apppass.ENV_KEY: "written-by-a-form"})
    assert not apppass.is_enabled()


def test_recovery_is_deleting_the_line(client):
    _lock()
    assert client.get("/cases").status_code == 303
    update_env({apppass.ENV_KEY: ""}, protected=frozenset({apppass.ENV_KEY}))
    assert client.get("/cases").status_code == 200


def test_the_oauth_return_is_not_blocked(client):
    _lock()  # a SameSite=Strict cookie is not sent when Google redirects back
    response = client.get("/settings/email/google/callback?error=access_denied")
    assert response.status_code == 303 and response.headers["location"].startswith("/settings")


def test_locked_data_still_lets_you_sign_in(client, monkeypatch):
    from nexus.security import atrest

    _lock()
    monkeypatch.setattr(atrest, "status", lambda: "locked")
    assert client.get("/login").status_code == 200
    assert client.get("/unlock").status_code == 303  # sign in first
    _sign_in(client)
    assert client.get("/unlock").status_code == 200


def test_the_hash_is_masked_in_logs(client):
    from nexus import logging_safe

    _lock()
    stored = read_env_value(apppass.ENV_KEY)
    assert stored in logging_safe._secret_values()
