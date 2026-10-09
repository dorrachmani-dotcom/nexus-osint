"""Unit tests for the GDELT source — no network.

We monkeypatch ``httpx.get`` to assert the query/language filter we build and to
exercise the JSON->RawItem mapping deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import nexus.sources.gdelt as gdelt_mod
from nexus.config import Settings
from nexus.sources.gdelt import GdeltSource, _parse_seendate

# Every fetch()/is_available() test must run against an empty, throwaway DB:
# investigation_query_targets() unions the env queries with DB subscriptions, so
# without isolation the source would pick up the developer's real topics.
pytestmark = pytest.mark.usefixtures("temp_db")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Zero out the politeness pacing / backoff so tests stay fast."""
    monkeypatch.setattr(gdelt_mod, "_PACING_SECONDS", 0)
    monkeypatch.setattr(gdelt_mod, "_BACKOFF_SECONDS", 0)


class _Resp429:
    status_code = 429

    def raise_for_status(self) -> None:
        raise RuntimeError("429")

    def json(self) -> dict:  # pragma: no cover - never reached on a 429
        return {}


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _settings(queries: str) -> Settings:
    # investigation_query_list reads the env field directly (no DB), so this is a
    # self-contained, network-free way to seed the source's query targets.
    return Settings(investigation_queries=queries)


# ---------------------------------------------------------------- date parsing
def test_parse_seendate():
    assert _parse_seendate("20260603T120000Z") == datetime(
        2026, 6, 3, 12, 0, 0, tzinfo=UTC
    )
    assert _parse_seendate(None) is None
    assert _parse_seendate("garbage") is None


# ----------------------------------------------------------- language filtering
def test_chinese_query_adds_sourcelang_filter(monkeypatch):
    captured: dict = {}

    def fake_get(url, params=None, **kwargs):
        captured["params"] = params
        return _FakeResp(
            {
                "articles": [
                    {
                        "url": "https://example.cn/a",
                        "title": "标题",
                        "seendate": "20260603T120000Z",
                        "domain": "example.cn",
                        "language": "Chinese",
                        "sourcecountry": "China",
                        "socialimage": "https://img/x.jpg",
                    }
                ]
            }
        )

    monkeypatch.setattr(gdelt_mod.httpx, "get", fake_get)

    src = GdeltSource(_settings("内马尔"))
    items = src.fetch()

    # The query must carry the Chinese source-language filter.
    assert "sourcelang:chinese" in captured["params"]["query"]
    assert captured["params"]["mode"] == "ArtList"
    assert captured["params"]["format"] == "json"
    # One mapped item, language stamped from the resolved query language.
    assert len(items) == 1
    it = items[0]
    assert it.source == "gdelt"
    assert it.language == "zh"
    assert it.url == "https://example.cn/a"
    assert it.media_urls == ["https://img/x.jpg"]
    assert it.raw["sourcecountry"] == "China"


def test_latin_query_has_no_language_filter(monkeypatch):
    captured: dict = {}

    def fake_get(url, params=None, **kwargs):
        captured["params"] = params
        return _FakeResp({"articles": []})

    monkeypatch.setattr(gdelt_mod.httpx, "get", fake_get)

    src = GdeltSource(_settings("Neymar"))
    src.fetch()
    assert "sourcelang:" not in captured["params"]["query"]
    assert captured["params"]["query"] == "Neymar"


def test_multiword_query_is_phrase_quoted(monkeypatch):
    captured: dict = {}

    def fake_get(url, params=None, **kwargs):
        captured["params"] = params
        return _FakeResp({"articles": []})

    monkeypatch.setattr(gdelt_mod.httpx, "get", fake_get)

    GdeltSource(_settings("lang:es climate change")).fetch()
    assert captured["params"]["query"] == '"climate change" sourcelang:spanish'


# --------------------------------------------------------------- graceful paths
def test_no_queries_unavailable():
    assert GdeltSource(_settings("")).is_available() is False


def test_network_error_is_swallowed(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(gdelt_mod.httpx, "get", boom)
    # One bad query must not abort the run; we simply get no items.
    assert GdeltSource(_settings("Neymar")).fetch() == []


def test_429_then_success_retries(monkeypatch):
    calls = {"n": 0}

    def flaky_get(url, params=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp429()  # first hit: throttled
        return _FakeResp(
            {"articles": [{"url": "https://x/a", "title": "ok"}]}
        )

    monkeypatch.setattr(gdelt_mod.httpx, "get", flaky_get)
    items = GdeltSource(_settings("Neymar")).fetch()
    assert calls["n"] == 2  # retried once after the 429
    assert len(items) == 1 and items[0].title == "ok"


def test_sustained_429_trips_circuit_breaker(monkeypatch):
    """When GDELT keeps returning 429, we stop after _MAX_CONSECUTIVE_THROTTLE
    sustained hits instead of grinding through every remaining query."""
    calls = {"n": 0}

    def always_429(url, params=None, **kwargs):
        calls["n"] += 1
        return _Resp429()

    monkeypatch.setattr(gdelt_mod.httpx, "get", always_429)
    monkeypatch.setattr(gdelt_mod, "_MAX_CONSECUTIVE_THROTTLE", 2)

    items = GdeltSource(_settings("a,b,c,d,e")).fetch()
    # 2 queries x (1 try + 1 retry) = 4 calls, then the breaker trips and the
    # remaining three queries are skipped entirely.
    assert calls["n"] == 4
    assert items == []


def test_success_resets_the_breaker(monkeypatch):
    """An intermittent throttle that recovers must NOT bail the run — only a
    sustained streak does. A success between throttles resets the counter."""
    def selective(url, params=None, **kwargs):
        # Only the "a" query is throttled; "b" and "c" succeed.
        if params and params.get("query") == "a":
            return _Resp429()
        return _FakeResp({"articles": [{"url": "https://x/ok", "title": "ok"}]})

    monkeypatch.setattr(gdelt_mod.httpx, "get", selective)
    monkeypatch.setattr(gdelt_mod, "_MAX_CONSECUTIVE_THROTTLE", 2)

    items = GdeltSource(_settings("a,b,c")).fetch()
    # "a" throttles (counter=1) but "b" succeeds (reset to 0), so "c" still runs.
    assert len(items) == 2
    assert all(it.title == "ok" for it in items)
