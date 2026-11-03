"""The packaged launcher picks its data home: NEXUS_HOME (if set and
non-empty) first, else the per-user default (LOCALAPPDATA on Windows)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parent.parent / "installer" / "run_nexus.py"


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("nexus_run_launcher", _LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("NEXUS_HOME", "DATA_DIR", "DATABASE_PATH", "NEXUS_ENV_FILE",
                 "PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "local"))
    return tmp_path


def test_nexus_home_takes_precedence(launcher, clean_env, monkeypatch):
    custom = clean_env / "portable home"
    monkeypatch.setenv("NEXUS_HOME", str(custom))
    home = launcher._configure_environment()
    assert home == custom and custom.is_dir()
    assert os.environ["NEXUS_HOME"] == str(custom)
    assert os.environ["DATA_DIR"] == str(custom / "data")
    assert os.environ["DATABASE_PATH"] == str(custom / "data" / "nexus.db")
    assert os.environ["NEXUS_ENV_FILE"] == str(custom / ".env")
    assert not (clean_env / "local" / "Nexus").exists()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_or_unset_nexus_home_uses_the_default(launcher, clean_env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("NEXUS_HOME", value)
    home = launcher._configure_environment()
    # LOCALAPPDATA (Windows) and XDG_DATA_HOME (elsewhere) both point here.
    expected = clean_env / "local" / "Nexus"
    assert home == expected
    assert os.environ["NEXUS_HOME"] == str(expected)
    assert os.environ["DATA_DIR"] == str(expected / "data")
