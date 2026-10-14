"""Shared pytest fixtures.

Tests must never touch the real ``data/`` database or the repo ``.env``. The
``temp_db`` fixture points the app's settings at a throwaway SQLite file in a
tmp dir; envstore tests monkeypatch ``_env_path`` to a tmp file directly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path, monkeypatch):
    """Every test reads and writes an EMPTY temp .env, never the developer's.

    Without this, a test run on a machine with a real .env (API keys, a
    connected Gmail account) would use those credentials, e.g. actually send
    an email. CI has no .env, so only a local run would notice.
    """
    from nexus.config import Settings, get_settings

    env = tmp_path / "isolated.env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    monkeypatch.setattr("nexus.envstore._env_path", lambda: env)
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "XAI_API_KEY", "SERPAPI_KEY", "GOOGLE_OAUTH_REFRESH_TOKEN", "SMTP_PASSWORD",
                "RESEND_API_KEY", "SENDGRID_API_KEY", "DIGEST_TO", "AI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    # Same for data: a test that forgets temp_db gets an empty tmp database,
    # never the developer's real data/nexus.db.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "isolated.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Initialise a fresh SQLite schema in a tmp dir and yield get_connection.

    Env vars override the Settings storage paths; the settings cache is cleared
    so the override takes effect, and again on teardown so later tests/process
    reuse the real config.
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PATH", str(db_file))

    from nexus.config import get_settings

    get_settings.cache_clear()
    from nexus.db import get_connection, init_db

    init_db()
    try:
        yield get_connection
    finally:
        get_settings.cache_clear()
