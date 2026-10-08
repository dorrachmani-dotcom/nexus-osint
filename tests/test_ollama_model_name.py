"""The local-model field accepts any Ollama tag but rejects junk and cloud variants."""

from fastapi.testclient import TestClient

from nexus.web.app import app


def _pull(model: str) -> str:
    return TestClient(app, base_url="http://127.0.0.1").post("/settings/ollama/pull", data={"model": model}).text


def test_rejects_cloud_variant(temp_db) -> None:
    assert "Cloud variants are not allowed" in _pull("kimi-k3:cloud")


def test_rejects_malformed_name(temp_db) -> None:
    assert "look like a local Ollama model name" in _pull("gemma4; rm -rf /")
