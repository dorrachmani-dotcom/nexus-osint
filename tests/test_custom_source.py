"""Unit tests for the generic, config-driven custom API source.

These cover the parsing/mapping helpers and the source's behaviour without any
network: every test that would otherwise make a request monkeypatches
``_request`` so the suite is fast and deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nexus.sources.custom import (
    CustomApiSource,
    _as_text,
    _dig,
    _parse_date,
)


def _make(**overrides) -> CustomApiSource:
    cfg = {
        "id": 1,
        "name": "Test",
        "enabled": 1,
        "base_url": "https://api.example.com",
        "endpoint": "",
        "http_method": "GET",
        "auth_type": "none",
        "auth_param": None,
        "query_param": None,
        "extra_params": "{}",
        "items_path": "",
        "map_title": None,
        "map_content": None,
        "map_url": None,
        "map_author": None,
        "map_published": None,
    }
    cfg.update(overrides)
    return CustomApiSource(cfg)


# --------------------------------------------------------------------- helpers
def test_dig_walks_dicts_and_list_indices():
    obj = {"a": {"b": [{"c": 10}, {"c": 20}]}}
    assert _dig(obj, "a.b.1.c") == 20


def test_dig_returns_none_on_miss():
    assert _dig({"a": 1}, "a.x.y") is None
    assert _dig({"a": 1}, "missing") is None


def test_dig_empty_path_returns_object():
    obj = {"a": 1}
    assert _dig(obj, "") is obj
    assert _dig(obj, None) is obj


def test_parse_date_epoch_and_iso():
    assert _parse_date(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert _parse_date("1700000000") is not None
    assert _parse_date("2024-01-02T03:04:05Z") == datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )


def test_parse_date_garbage_is_none():
    assert _parse_date("not a date") is None
    assert _parse_date("") is None
    assert _parse_date(None) is None


def test_as_text_serialises_containers():
    assert _as_text("  hi  ") == "hi"
    assert _as_text(5) == "5"
    assert _as_text(None) == ""
    assert _as_text({"k": "v"}) == '{"k": "v"}'


# ---------------------------------------------------------------- availability
def test_is_available_none_auth_needs_no_key():
    assert _make(auth_type="none").is_available() is True


def test_is_available_keyed_auth_requires_key(monkeypatch):
    src = _make(auth_type="header")
    monkeypatch.setattr(src, "_api_key", lambda: None)
    assert src.is_available() is False
    monkeypatch.setattr(src, "_api_key", lambda: "secret")
    assert src.is_available() is True


def test_is_available_disabled_or_no_base_url():
    assert _make(enabled=0).is_available() is False
    assert _make(base_url="").is_available() is False


# ------------------------------------------------------------------ url build
def test_build_url_relative_and_leading_slash():
    assert _make(endpoint="/v1/posts")._build_url(None) == (
        "https://api.example.com/v1/posts"
    )
    assert _make(endpoint="v1/posts")._build_url(None) == (
        "https://api.example.com/v1/posts"
    )


def test_build_url_query_token():
    assert _make(endpoint="/search/{query}")._build_url("ukraine") == (
        "https://api.example.com/search/ukraine"
    )


def test_build_url_absolute_endpoint_wins():
    assert _make(endpoint="https://other.test/x")._build_url(None) == (
        "https://other.test/x"
    )


# -------------------------------------------------------------------- mapping
def test_map_item_maps_all_fields():
    src = _make(
        map_content="text",
        map_title="head",
        map_url="link",
        map_author="user.name",
    )
    raw = {"text": "hello", "head": "Hi", "link": "http://e/1", "user": {"name": "bob"}}
    item = src._map_item(raw, None)
    assert item is not None
    assert item.content == "hello"
    assert item.title == "Hi"
    assert item.url == "http://e/1"
    assert item.author == "bob"
    assert item.external_id == "http://e/1"


def test_map_item_requires_content_or_title():
    src = _make(map_content="text", map_title="head")
    assert src._map_item({"other": "x"}, None) is None


# -------------------------------------------------------------------- extract
def test_extract_list_tolerates_single_object():
    src = _make(items_path="data")
    assert src._extract_list({"data": {"text": "x"}}) == [{"text": "x"}]
    assert src._extract_list({"data": "not a list"}) == []
    assert src._extract_list({"data": [1, 2]}) == [1, 2]


# ---------------------------------------------------------------------- fetch
def test_fetch_graceful_on_request_failure(monkeypatch):
    src = _make(items_path="data", map_content="text")
    monkeypatch.setattr(src, "_request", lambda q: None)
    assert src.fetch() == []


def test_fetch_maps_and_skips_empty(monkeypatch):
    src = _make(items_path="data", map_content="text", map_url="url")
    monkeypatch.setattr(
        src,
        "_request",
        lambda q: {"data": [{"text": "a", "url": "u1"}, {"text": "b", "url": "u2"}, {"nope": 1}]},
    )
    items = src.fetch()
    assert len(items) == 2
    assert items[0].content == "a"
    assert items[1].url == "u2"
