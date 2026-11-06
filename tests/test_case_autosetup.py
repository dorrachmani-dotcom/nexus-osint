"""Opening a case auto-creates its tracking words (the capsule), so a new case
has a working live feed with no separate step — in the form and via Sherlock."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.analysis.providers import LLMProvider
from nexus.casesetup import _split_name, auto_setup_case
from nexus.config import get_settings


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def test_split_name_into_search_terms():
    assert _split_name("Messi & World Cup") == ["Messi", "World Cup", "Messi & World Cup"]
    assert "injury" in _split_name("Neymar transfer and injury")
    assert _split_name("") == []


def test_auto_setup_seeds_terms_without_ai(temp_db):
    # No AI provider in the test env -> at least the name-derived words are added.
    with temp_db() as conn:
        cid = s.create_case(conn, "Messi & World Cup")
        summary = auto_setup_case(conn, cid, "Messi & World Cup")
        terms = {t["term"] for t in s.case_terms(conn, cid)}
        assert "Messi" in terms and "World Cup" in terms
        assert summary["terms"] >= 2


def test_creating_a_case_via_form_auto_links_words(temp_db):
    with temp_db():
        _client().post("/cases", data={"name": "Mbappe", "priority": "medium"})
    with temp_db() as conn:
        cid = next(c["id"] for c in s.list_cases(conn) if c["name"] == "Mbappe")
        assert any(t["term"] == "Mbappe" for t in s.case_terms(conn, cid))


def test_sherlock_create_case_auto_links_words(temp_db):
    from nexus.assistant import _do_create_case

    with temp_db() as conn:
        r = _do_create_case(conn, {"last_case_id": None, "by_name": {}},
                            {"name": "Ronaldo"})
        assert r["type"] == "case_created"
        cid = r["case_id"]
        assert any(t["term"] == "Ronaldo" for t in s.case_terms(conn, cid))


# ------------------------------------------------- AI expansion and the write lock
class _RefusedProvider(LLMProvider):
    """A model that cannot be reached, logged like every real backend."""

    name = "ollama"

    def complete(self, system_prompt, user_prompt, max_tokens=1024):
        raise ConnectionError("refused")


class _PlanProvider(LLMProvider):
    name = "ollama"

    def __init__(self, gate=None):
        super().__init__("test-model")
        self.gate = gate

    def complete(self, system_prompt, user_prompt, max_tokens=1024):
        if self.gate is not None:
            assert self.gate.wait(10)
        return '{"queries": ["Acme subsidiary"], "requirements": ["Who owns Acme?"]}'


def _use_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr("nexus.analysis.query_builder.get_provider", lambda settings: provider)
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    get_settings.cache_clear()


def _wait_for(check) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_case_form_is_instant_while_the_model_is_unreachable(temp_db, monkeypatch):
    _use_provider(monkeypatch, _RefusedProvider("test-model"))
    with temp_db():
        started = time.monotonic()
        resp = _client().post("/cases", data={"name": "Acme"})
        elapsed = time.monotonic() - started
    assert resp.status_code == 200 and elapsed < 3
    with temp_db() as conn:
        cid = next(c["id"] for c in s.list_cases(conn) if c["name"] == "Acme")
        assert [t["term"] for t in s.case_terms(conn, cid)] == ["Acme"]

    def logged() -> bool:
        with temp_db() as conn:
            return conn.execute(
                "SELECT 1 FROM outbound_log WHERE category = 'ai' AND ok = 0"
            ).fetchone() is not None

    assert _wait_for(logged), "the failed AI call must reach the outbound log"


def test_case_form_returns_before_the_model_answers(temp_db, monkeypatch):
    gate = threading.Event()
    _use_provider(monkeypatch, _PlanProvider(gate))
    with temp_db():
        assert _client().post("/cases", data={"name": "Acme"}).status_code == 200
    with temp_db() as conn:
        cid = next(c["id"] for c in s.list_cases(conn) if c["name"] == "Acme")
        assert {t["term"] for t in s.case_terms(conn, cid)} == {"Acme"}
    gate.set()

    def expanded() -> bool:
        with temp_db() as conn:
            return (any(t["term"] == "Acme subsidiary" for t in s.case_terms(conn, cid))
                    and any(r["question"] == "Who owns Acme?"
                            for r in s.list_requirements(conn, case_id=cid)))

    assert _wait_for(expanded)


def test_case_deleted_during_expansion_is_left_alone(temp_db, monkeypatch):
    gate = threading.Event()
    _use_provider(monkeypatch, _PlanProvider(gate))
    with temp_db():
        _client().post("/cases", data={"name": "Acme"})
    with temp_db() as conn:
        cid = next(c["id"] for c in s.list_cases(conn) if c["name"] == "Acme")
        s.delete_case(conn, cid)
    gate.set()
    time.sleep(0.3)
    with temp_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM case_terms").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 0


def test_case_setup_does_not_hold_the_write_lock_during_the_ai_call(temp_db, monkeypatch):
    # A provider logs to the outbound table on its own connection; a write
    # transaction left open here stalled for busy_timeout and lost that row.
    _use_provider(monkeypatch, _PlanProvider())
    with temp_db() as conn:
        cid = s.create_case(conn, "Acme")
        started = time.monotonic()
        summary = auto_setup_case(conn, cid, "Acme")
        elapsed = time.monotonic() - started
        assert summary == {"terms": 2, "questions": 1}
        assert elapsed < 3
        assert conn.execute(
            "SELECT 1 FROM outbound_log WHERE category = 'ai' AND ok = 1"
        ).fetchone() is not None
