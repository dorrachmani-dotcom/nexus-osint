"""Unit tests for the Twitter/X source — no network.

We monkeypatch ``httpx.get`` to assert the author-expansion request we build and
to exercise the JSON->RawItem mapping, especially resolving the numeric
``author_id`` to a human ``@handle`` (and a clean profile URL) when the API
returns the user expansion — and falling back gracefully when it does not.
"""

from __future__ import annotations

import pytest

import nexus.sources.twitter as twitter_mod
from nexus.config import Settings
from nexus.sources.twitter import TwitterSource

# fetch()/is_available() union the env queries with DB subscriptions, so without
# an isolated, empty DB the source would pick up the developer's real topics.
pytestmark = pytest.mark.usefixtures("temp_db")


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _settings() -> Settings:
    # A bearer token + one query is all is_available()/fetch() need. Pin
    # investigation_queries empty so only our single twitter query is targeted.
    return Settings(
        twitter_bearer_token="TEST", twitter_queries="neymar", investigation_queries=""
    )


def test_unavailable_without_token():
    assert (
        TwitterSource(
            Settings(twitter_queries="x", investigation_queries="")
        ).is_available()
        is False
    )


def test_unavailable_without_query():
    assert (
        TwitterSource(
            Settings(twitter_bearer_token="T", investigation_queries="")
        ).is_available()
        is False
    )


def test_author_id_resolves_to_handle(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None, params=None, **kwargs):
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp(
            {
                "data": [
                    {
                        "id": "1850",
                        "text": "hello world",
                        "author_id": "42",
                        "created_at": "2026-06-03T12:00:00.000Z",
                        "lang": "en",
                    }
                ],
                "includes": {"users": [{"id": "42", "username": "jack", "name": "Jack"}]},
            }
        )

    monkeypatch.setattr(twitter_mod.httpx, "get", fake_get)
    items = TwitterSource(_settings()).fetch()

    # The request must ask for the author expansion + username field.
    assert captured["params"]["expansions"] == "author_id"
    assert "username" in captured["params"]["user.fields"]
    assert captured["headers"]["Authorization"] == "Bearer TEST"

    assert len(items) == 1
    it = items[0]
    assert it.author == "@jack"  # numeric id resolved to a handle
    assert it.url == "https://twitter.com/jack/status/1850"  # clean profile URL
    assert it.language == "en"
    assert it.content == "hello world"


def test_missing_expansion_falls_back_to_id(monkeypatch):
    def fake_get(url, headers=None, params=None, **kwargs):
        # No "includes" block — the handle can't be resolved.
        return _FakeResp(
            {"data": [{"id": "9", "text": "t", "author_id": "77", "lang": "en"}]}
        )

    monkeypatch.setattr(twitter_mod.httpx, "get", fake_get)
    items = TwitterSource(_settings()).fetch()

    assert len(items) == 1
    it = items[0]
    # Author is never blank: fall back to the numeric id and the id-permalink.
    assert it.author == "77"
    assert it.url == "https://twitter.com/i/web/status/9"


def test_network_error_is_swallowed(monkeypatch):
    def boom(url, headers=None, params=None, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(twitter_mod.httpx, "get", boom)
    # A per-query failure must not abort the run; we simply get no items.
    assert TwitterSource(_settings()).fetch() == []
