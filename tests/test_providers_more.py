"""Grok (xAI), local OpenAI-compatible servers and the "auto" provider.

Everything is offline: settings are built in-memory (no .env), the DB-meta
override is monkeypatched, and httpx is replaced by fakes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nexus.analysis import providers as prov
from nexus.config import Settings

_KEY_ENVS = (
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
    "XAI_API_KEY", "LOCAL_LLM_API_KEY", "LOCAL_LLM_MODEL", "AI_PROVIDER",
)


@pytest.fixture
def meta(monkeypatch):
    """A fake DB-meta store for chosen_provider / model overrides."""
    store: dict[str, str] = {}
    monkeypatch.setattr("nexus.storage.get_meta_value", lambda k, d=None: store.get(k, d))
    for env in _KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    return store


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --- resolution --------------------------------------------------------------


def test_default_is_auto_and_off_without_any_backend(meta):
    s = _settings()
    assert s.chosen_provider() == "auto"
    assert s.active_provider() == "off"
    assert s.analysis_enabled is False


def test_auto_order_prefers_gemini_then_openai_anthropic_grok(meta):
    assert _settings(gemini_api_key="g", openai_api_key="o", xai_api_key="x").active_provider() == "gemini"
    assert _settings(openai_api_key="o", anthropic_api_key="a").active_provider() == "openai"
    assert _settings(anthropic_api_key="a", xai_api_key="x").active_provider() == "anthropic"
    assert _settings(xai_api_key="x").active_provider() == "grok"


def test_auto_uses_local_server_only_with_a_model(meta):
    assert _settings().active_provider() == "off"  # base URL set by default, no model
    meta["local_llm_model"] = "qwen2.5-7b-instruct"
    s = _settings()
    assert s.active_provider() == "local_openai"
    assert s.analysis_model == "qwen2.5-7b-instruct"


def test_auto_never_picks_ollama(meta):
    s = _settings()
    assert s.ollama_enabled  # its defaults always look configured...
    assert s.active_provider() == "off"  # ...so auto must not route to it


def test_explicit_choices_still_work_and_never_switch_vendor(meta):
    meta["ai_provider"] = "ollama"
    assert _settings().active_provider() == "ollama"
    meta["ai_provider"] = "grok"
    assert _settings(gemini_api_key="g").active_provider() == "off"  # no silent switch
    s = _settings(xai_api_key="x")
    assert s.active_provider() == "grok"
    assert s.analysis_model == s.grok_model
    meta["ai_provider"] = "off"
    assert _settings(gemini_api_key="g").active_provider() == "off"


def test_unknown_choice_falls_back_to_auto(meta):
    meta["ai_provider"] = "some-old-value"
    s = _settings(openai_api_key="o")
    assert s.chosen_provider() == "auto"
    assert s.active_provider() == "openai"


def test_availability_labels(meta):
    assert "grok" in _settings(xai_api_key="x").availability_report()
    meta["local_llm_model"] = "m"
    assert _settings().availability_report().get("local") == "on"


def test_get_provider_builds_openai_compatible_backends(meta):
    p = prov.get_provider(_settings(xai_api_key="xai-test"))
    assert isinstance(p, prov.OpenAICompatibleProvider)
    assert p.name == "grok" and p._base_url == "https://api.x.ai/v1" and p._timeout == 60.0
    meta["ai_provider"] = "local_openai"
    meta["local_llm_model"] = "llama-3.1-8b"
    p = prov.get_provider(_settings())
    assert p.name == "local_openai" and p._timeout == 180.0 and p._api_key is None
    assert p.model == "llama-3.1-8b"


# --- OpenAICompatibleProvider ------------------------------------------------


class _Resp:
    def __init__(self, status=200, data=None, bad_json=False):
        self.status_code = status
        self._data = data
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_complete_posts_chat_completions(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Resp(data={"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr("httpx.post", fake_post)
    p = prov.OpenAICompatibleProvider("https://api.x.ai/v1/", "grok-test", "xai-secret", name="grok")
    assert p.complete("SYS", "USER", max_tokens=321) == '{"ok": true}'
    call = calls[0]
    assert call["url"] == "https://api.x.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer xai-secret"
    assert call["json"]["messages"] == [
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"},
    ]
    assert call["json"]["max_tokens"] == 321
    assert "response_format" not in call["json"]
    assert call["timeout"] == 60.0
    # chat() reuses complete() (no forced JSON mode on this backend).
    assert p.chat("SYS", "USER") == '{"ok": true}'


def test_complete_without_key_sends_no_auth_and_tolerates_bad_shapes(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["headers"] = headers
        return _Resp(data={"choices": []})

    monkeypatch.setattr("httpx.post", fake_post)
    p = prov.OpenAICompatibleProvider("http://localhost:1234/v1", "m", None, timeout=180.0)
    assert p.complete("s", "u") == ""
    assert "Authorization" not in seen["headers"]
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(bad_json=True))
    assert p.complete("s", "u") == ""
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(data={"choices": [{"message": {"content": None}}]}))
    assert p.complete("s", "u") == ""


def test_complete_raises_on_http_error_for_the_pipeline_to_handle(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(status=500))
    p = prov.OpenAICompatibleProvider("http://localhost:1234/v1", "m")
    with pytest.raises(RuntimeError):
        p.complete("s", "u")


# --- local server test connection -------------------------------------------


def test_check_lists_models_and_finds_the_chosen_one(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, headers=None, timeout=None: _Resp(
        data={"data": [{"id": "qwen2.5-7b-instruct"}, {"id": "llama-3.1-8b"}]}))
    r = prov.check_openai_compatible("http://localhost:1234/v1", "llama-3.1-8b")
    assert r["server_up"] and r["model_present"] and r["error"] == ""
    assert r["models"] == ["qwen2.5-7b-instruct", "llama-3.1-8b"]
    r = prov.check_openai_compatible("http://localhost:1234/v1", "")
    assert r["server_up"] and not r["model_present"] and "pick one" in r["error"]
    r = prov.check_openai_compatible("http://localhost:1234/v1", "missing-model")
    assert not r["model_present"] and "missing-model" in r["error"]


def test_check_never_raises_and_explains(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    r = prov.check_openai_compatible("http://localhost:1234/v1", "m")
    assert not r["server_up"] and "Could not reach" in r["error"]
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(status=401))
    assert "API key" in prov.check_openai_compatible("http://localhost:1234/v1", "m")["error"]
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(status=404))
    assert "/v1" in prov.check_openai_compatible("http://localhost:1234/v1", "m")["error"]
    assert "No server address" in prov.check_openai_compatible("", "m")["error"]


# --- settings routes ---------------------------------------------------------


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_local_llm_settings_route_saves_model_and_rejects_junk(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("nexus.envstore._env_path", lambda: tmp_path / ".env")
    from nexus.storage import get_meta

    c = _client()
    r = c.post("/settings/local-llm", data={"base_url": "http://localhost:1234/v1", "model": "qwen2.5-7b-instruct"})
    assert r.status_code == 200 and "Saved" in r.text
    with temp_db() as conn:
        assert get_meta(conn, "local_llm_model") == "qwen2.5-7b-instruct"
    r = c.post("/settings/local-llm", data={"base_url": "http://localhost:1234/v1", "model": "bad model; rm"})
    assert "Not saved" in r.text
    r = c.post("/settings/local-llm", data={"base_url": "ftp://somewhere", "model": ""})
    assert "Not saved" in r.text


def test_local_llm_check_route_renders(temp_db, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(data={"data": [{"id": "m1"}]}))
    r = _client().post("/settings/local-llm/check")
    assert r.status_code == 200 and "m1" in r.text


def test_provider_select_offers_new_choices(temp_db):
    r = _client().post("/settings/provider", data={"provider": "grok"})
    assert r.status_code == 200
    assert "xAI (Grok)" in r.text and "XAI_API_KEY" in r.text
    r = _client().post("/settings/provider", data={"provider": "auto"})
    assert "Automatic" in r.text


# --- egress ------------------------------------------------------------------


def test_egress_knows_grok_and_local_server(monkeypatch):
    from nexus.security import egress

    assert egress.classify("api.x.ai", 443) == ("AI provider (xAI Grok)", True)
    fake = _settings(local_llm_base_url="http://gpu-box.example.lan:8000/v1")
    monkeypatch.setattr("nexus.config.get_settings", lambda: fake)
    monkeypatch.setattr("nexus.storage.get_meta_value", lambda k, d=None: d)
    egress._allow_cache = None
    try:
        allow = egress._configured_allow()
        assert allow.get("gpu-box.example.lan") == "AI provider (local OpenAI-compatible server)"
    finally:
        egress._allow_cache = None
