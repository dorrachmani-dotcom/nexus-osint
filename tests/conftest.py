"""Shared pytest fixtures.

Tests must never touch the real ``data/`` database or the repo ``.env``. The
``temp_db`` fixture points the app's settings at a throwaway SQLite file in a
tmp dir; envstore tests monkeypatch ``_env_path`` to a tmp file directly.
"""

from __future__ import annotations

import pytest


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
