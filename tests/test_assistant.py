"""Tests for the in-dashboard analyst assistant.

The assistant is a grounded chat layer over the provider-agnostic LLM engine. It
must (a) degrade clearly when no model is configured, (b) feed the local data to
the model as context, and (c) carry a hard anonymity guard so it never reveals
who built the software. These tests stub the provider so they need no API key
and make no network calls.
"""

from __future__ import annotations

import json

import nexus.assistant as A
from nexus.assistant import (
    PROJECT_GUIDE,
    _BEHAVIOUR,
    _describe_current,
    _extract_json,
    _keywords,
    _resolve_page,
    act,
    answer,
)
from nexus.models import RawItem
from nexus.storage import (
    case_items,
    create_case,
    list_cases,
    set_active_case,
    upsert_item,
)


class _FakeSettings:
    """Minimal Settings double exposing only what `answer` touches."""

    def __init__(self, provider: str):
        self._p = provider

    def active_provider(self) -> str:
        return self._p


class _FakeProvider:
    """Captures the prompts it was handed and returns a canned reply."""

    def __init__(self, reply: str = "canned reply"):
        self.reply = reply
        self.system_prompt = None
        self.user_prompt = None

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.reply


# --- keyword extraction -----------------------------------------------------


def test_keywords_drop_stopwords_and_short_tokens():
    kw = _keywords("What is the latest news about Neymar in Brazil?")
    assert "neymar" in [k.lower() for k in kw]
    assert "brazil" in [k.lower() for k in kw]
    # stop-words / short tokens removed
    assert "the" not in kw and "is" not in kw and "in" not in kw


def test_keywords_empty_on_pure_stopwords():
    assert _keywords("what is the and or to") == []


# --- anonymity / safety guard ----------------------------------------------


def test_behaviour_contains_anonymity_guard():
    low = _BEHAVIOUR.lower()
    assert "no information about who created" in low
    assert "who made you" in low or "who built this" in low


def test_project_guide_has_no_authorship_or_identity():
    low = (PROJECT_GUIDE + _BEHAVIOUR).lower()
    # Nothing tying the tool to a person, company or origin country.
    for needle in ("developed by", "copyright",
                   "author:", "created by"):
        assert needle not in low


# --- graceful degradation when no provider ---------------------------------


def test_answer_off_provider_is_graceful(temp_db):
    with temp_db() as conn:
        res = answer("hello", conn=conn, settings=_FakeSettings("off"))
    assert res["ok"] is False
    assert res["provider"] == "off"
    assert "Settings" in res["error"]  # points the user at how to fix it


def test_answer_empty_question():
    res = answer("   ", conn=None, settings=_FakeSettings("anthropic"))
    assert res["ok"] is False
    assert "question" in res["error"].lower()


# --- happy path: data grounding + guard reach the model --------------------


def test_answer_grounds_in_local_data(temp_db, monkeypatch):
    fake = _FakeProvider(reply="Here is what I found.")
    monkeypatch.setattr(
        "nexus.analysis.providers.get_provider", lambda settings: fake
    )

    with temp_db() as conn:
        upsert_item(
            conn,
            RawItem(
                source="rss",
                title="Neymar signs new contract",
                content="The player agreed terms with the club today.",
                url="https://example.com/neymar",
            ),
        )
        res = answer(
            "What is the latest on Neymar?",
            conn=conn,
            settings=_FakeSettings("anthropic"),
        )

    assert res["ok"] is True
    assert res["answer"] == "Here is what I found."
    # The model was handed the local item as grounded context...
    assert "Neymar signs new contract" in fake.user_prompt
    # ...and the system prompt carries the product manual + anonymity guard.
    assert "TRANSFER STATION" in fake.system_prompt
    assert "no information about who created" in fake.system_prompt.lower()


def test_answer_handles_provider_exception(temp_db, monkeypatch):
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("model down")

    monkeypatch.setattr(
        "nexus.analysis.providers.get_provider", lambda settings: _Boom()
    )
    with temp_db() as conn:
        res = answer("hi", conn=conn, settings=_FakeSettings("ollama"))
    assert res["ok"] is False
    assert res["answer"] == ""
    assert res["error"]  # a friendly message, not a traceback


def test_answer_provider_unavailable(temp_db, monkeypatch):
    monkeypatch.setattr(
        "nexus.analysis.providers.get_provider", lambda settings: None
    )
    with temp_db() as conn:
        res = answer("hi", conn=conn, settings=_FakeSettings("anthropic"))
    assert res["ok"] is False
    assert "could not be started" in res["error"]


def test_unused_import_guard():
    # Keep the module import referenced (defensive: the module must import clean).
    assert hasattr(A, "answer")
    assert hasattr(A, "act")


# --- agentic "deep" mode: reason -> search -> observe -> answer ------------

class _FakeDeepProvider:
    """Returns queued responses so we can drive the deep research loop:
    gather steps first, then the final answer."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        self.calls += 1
        return self._responses.pop(0) if self._responses else '{"reply":"done","actions":[]}'


def test_deep_mode_runs_a_search_loop_then_answers(temp_db, monkeypatch):
    import json as _json

    from nexus.models import RawItem

    # gather step 1: search 'messi'; step 2: enough; then the final answer.
    fake = _FakeDeepProvider([
        _json.dumps({"search": "messi"}),
        _json.dumps({"enough": True}),
        _json.dumps({"reply": "Messi briefing.", "actions": []}),
    ])
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        conn.execute("INSERT INTO items (content_hash, source, title, content) "
                     "VALUES ('h1','rss','Messi scores','...')")
        res = A.act("what's new on messi?", conn=conn,
                    settings=_FakeSettings("gemini"), deep=True)
    assert res["ok"] is True
    assert res["answer"] == "Messi briefing."
    # The loop made extra calls beyond the single final answer.
    assert fake.calls >= 3


def test_deep_gather_is_bounded_and_safe(temp_db, monkeypatch):
    # A provider that always wants to search must still stop at the step cap.
    import json as _json

    fake = _FakeDeepProvider([_json.dumps({"search": f"q{i}"}) for i in range(10)])
    with temp_db() as conn:
        out = A._deep_gather(fake, "endless", conn, max_steps=3)
    assert fake.calls <= 3  # capped
    assert isinstance(out, str)


def test_normal_mode_skips_the_research_loop(temp_db, monkeypatch):
    # Without deep, only the single final-answer call happens.
    fake = _FakeDeepProvider(['{"reply":"quick","actions":[]}'])
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = A.act("hi", conn=conn, settings=_FakeSettings("gemini"))
    assert res["ok"] is True and fake.calls == 1


# --- agentic layer: Sherlock can take real actions -------------------------


class _FakePlanProvider:
    """Provider double whose `complete` returns a canned JSON action plan."""

    def __init__(self, plan: dict):
        self._plan = plan
        self.system_prompt = None

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        self.system_prompt = system_prompt
        return json.dumps(self._plan)


def test_extract_json_tolerates_fences_and_prose():
    assert _extract_json('{"reply": "hi", "actions": []}') == {"reply": "hi", "actions": []}
    assert _extract_json('```json\n{"reply": "x"}\n```') == {"reply": "x"}
    assert _extract_json('Sure!\n{"reply": "y", "actions": []}\nthanks') == {
        "reply": "y", "actions": [],
    }
    assert _extract_json("no json here at all") is None


def test_act_plain_answer_runs_no_actions(temp_db, monkeypatch):
    fake = _FakePlanProvider({"reply": "Here's how to scan.", "actions": []})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("how do I scan?", conn=conn, settings=_FakeSettings("gemini"))
    assert res["ok"] is True
    assert res["answer"] == "Here's how to scan."
    assert res["actions"] == []
    # The action protocol (with the anonymity guard) reached the model.
    assert "AVAILABLE ACTION OBJECTS" in fake.system_prompt
    assert "no information about who created" in fake.system_prompt.lower()


def test_act_builds_case_adds_items_and_reports(temp_db, monkeypatch):
    plan = {
        "reply": "Built the Neymar case, added the matching items and a PDF.",
        "actions": [
            {"tool": "create_case", "name": "Neymar & World Cup", "priority": "high"},
            {"tool": "add_items_to_case", "case": "last", "query": "Neymar World Cup", "max": 50},
            {"tool": "generate_report", "case": "last", "format": "pdf"},
        ],
    }
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)

    with temp_db() as conn:
        upsert_item(conn, RawItem(
            source="rss", title="Neymar shines at the World Cup",
            content="The forward starred for Brazil in the tournament.",
            url="https://example.com/neymar-wc"))
        upsert_item(conn, RawItem(
            source="rss", title="Unrelated market news",
            content="Stocks rose today on tech earnings.",
            url="https://example.com/markets"))

        res = act("Open a case on Neymar & the World Cup, fill it, make me a PDF.",
                  conn=conn, settings=_FakeSettings("gemini"))

        assert res["ok"] is True
        types = [a["type"] for a in res["actions"]]
        assert types == ["case_created", "items_added", "report"]

        # A case was actually created and the relevant item pinned into it.
        cases = list_cases(conn)
        assert any(c["name"] == "Neymar & World Cup" for c in cases)
        cid = res["actions"][0]["case_id"]
        pinned = case_items(conn, cid)
        assert len(pinned) == 1
        assert "Neymar" in pinned[0]["title"]

        # The report action exposes a downloadable URL for that case.
        report = res["actions"][2]
        assert report["url"] == f"/cases/{cid}/report?format=pdf"
        assert report.get("open") is True


def test_act_rejects_unknown_tools(temp_db, monkeypatch):
    plan = {"reply": "ok", "actions": [{"tool": "delete_everything"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("wipe it all", conn=conn, settings=_FakeSettings("gemini"))
    assert res["ok"] is True
    assert res["actions"] == []  # unknown tool silently ignored, nothing destroyed


# --- navigation (open_page): Sherlock can take the analyst to a page ---------


def test_resolve_page_exact_alias_substring_and_unknown():
    # Exact key.
    assert _resolve_page("guide")[0] == "/guide"
    assert _resolve_page("settings")[0] == "/settings"
    # Alias.
    assert _resolve_page("home")[0] == "/"
    assert _resolve_page("getting started")[0] == "/guide"
    assert _resolve_page("api keys")[0] == "/settings"
    # Loose substring ("settings page", "the graph view").
    assert _resolve_page("settings page")[0] == "/settings"
    assert _resolve_page("the graph view")[0] == "/graph"
    # Unknown / empty -> None so the caller falls back to the Guide.
    assert _resolve_page("") is None
    assert _resolve_page("zzz-not-a-page") is None


def test_act_open_page_navigates_to_guide(temp_db, monkeypatch):
    plan = {
        "reply": "I'll take you to the Guide — it walks you through the first steps.",
        "actions": [{"tool": "open_page", "page": "guide"}],
    }
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("how do I start?", conn=conn, settings=_FakeSettings("gemini"))
    assert res["ok"] is True
    assert len(res["actions"]) == 1
    nav = res["actions"][0]
    assert nav["type"] == "navigate"
    assert nav["navigate"] is True
    assert nav["url"] == "/guide"
    assert "guide" in nav["label"].lower()


def test_act_open_page_unknown_falls_back_to_guide(temp_db, monkeypatch):
    plan = {"reply": "Here.", "actions": [{"tool": "open_page", "page": "wat"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("take me somewhere", conn=conn, settings=_FakeSettings("gemini"))
    nav = res["actions"][0]
    assert nav["type"] == "navigate"
    assert nav["url"] == "/guide"  # never a dead end


def test_act_open_page_protocol_reaches_model(temp_db, monkeypatch):
    fake = _FakePlanProvider({"reply": "ok", "actions": []})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        act("hello", conn=conn, settings=_FakeSettings("gemini"))
    # The open_page action + the proactive-guidance instructions reach the model.
    assert "open_page" in fake.system_prompt
    assert "GUIDING THE ANALYST TO A PAGE" in fake.system_prompt


# --- expanded capabilities: scan, search, watchlist, PIR, note, evidence -----


def test_act_run_scan_collects(temp_db, monkeypatch):
    class _FakeCollector:
        def __init__(self, settings=None):
            pass

        def scan(self):
            return {"_update": {"new": 3}, "rss": {"fetched": 5, "new": 3}}

    monkeypatch.setattr("nexus.collector.Collector", _FakeCollector)
    plan = {"reply": "Collecting fresh intel now.", "actions": [{"tool": "run_scan"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("get me the latest", conn=conn, settings=_FakeSettings("gemini"))
    assert res["ok"] is True
    act_out = res["actions"][0]
    assert act_out["type"] == "scan"
    assert "3" in act_out["label"]
    assert act_out["url"] == "/"


def test_act_run_scan_no_sources_is_graceful(temp_db, monkeypatch):
    class _FakeCollector:
        def __init__(self, settings=None):
            pass

        def scan(self):
            return {"_note": "no sources available"}

    monkeypatch.setattr("nexus.collector.Collector", _FakeCollector)
    fake = _FakePlanProvider({"reply": "Scanning.", "actions": [{"tool": "run_scan"}]})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("scan now", conn=conn, settings=_FakeSettings("gemini"))
    out = res["actions"][0]
    assert out["type"] == "scan"
    assert "no sources" in out["label"].lower()


def test_act_search_feed_surfaces_items(temp_db, monkeypatch):
    plan = {"reply": "Here's what I found.",
            "actions": [{"tool": "search_feed", "query": "Neymar"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        upsert_item(conn, RawItem(
            source="rss", title="Neymar returns to training",
            content="The forward trained with the squad.",
            url="https://example.com/neymar-train"))
        upsert_item(conn, RawItem(
            source="rss", title="Unrelated weather report",
            content="Rain expected tomorrow.", url="https://example.com/weather"))
        res = act("find me everything on Neymar", conn=conn,
                  settings=_FakeSettings("gemini"))
    out = res["actions"][0]
    assert out["type"] == "search"
    assert out["url"].startswith("/?q=")
    assert len(out["items"]) == 1
    assert "Neymar" in out["items"][0]["title"]
    # Only real http(s) links are handed to the UI.
    assert out["items"][0]["url"].startswith("https://")


def test_act_search_feed_no_hits_is_graceful(temp_db, monkeypatch):
    plan = {"reply": "Searching.",
            "actions": [{"tool": "search_feed", "query": "zzzznotfound"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("look for zzzznotfound", conn=conn, settings=_FakeSettings("gemini"))
    out = res["actions"][0]
    assert out["type"] == "search"
    assert out["items"] == []
    assert "no items" in out["label"].lower()


def test_act_add_watchlist(temp_db, monkeypatch):
    from nexus.storage import list_watchlists

    plan = {"reply": "I'll watch for that.",
            "actions": [{"tool": "add_watchlist", "term": "ransomware", "kind": "keyword"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("alert me whenever ransomware shows up", conn=conn,
                  settings=_FakeSettings("gemini"))
        out = res["actions"][0]
        assert out["type"] == "watchlist"
        wls = list_watchlists(conn)
        assert any(w["pattern"] == "ransomware" for w in wls)


def test_act_add_watchlist_bad_kind_defaults_keyword(temp_db, monkeypatch):
    from nexus.storage import list_watchlists

    plan = {"reply": "ok",
            "actions": [{"tool": "add_watchlist", "term": "BTC1xyz", "kind": "nonsense"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        act("watch BTC1xyz", conn=conn, settings=_FakeSettings("gemini"))
        wls = list_watchlists(conn)
        assert wls and wls[0]["kind"] == "keyword"


def test_act_add_requirement(temp_db, monkeypatch):
    from nexus.storage import list_requirements

    plan = {"reply": "Tracking that question.",
            "actions": [{"tool": "add_requirement",
                         "question": "Is the suspect leaving the country?",
                         "priority": "high"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("my priority is whether the suspect is leaving the country",
                  conn=conn, settings=_FakeSettings("gemini"))
        out = res["actions"][0]
        assert out["type"] == "requirement"
        reqs = list_requirements(conn)
        assert any("leaving the country" in r["question"] for r in reqs)
        # "high" maps to priority 1.
        assert any(r["priority"] == 1 for r in reqs)


def test_act_add_note_to_case(temp_db, monkeypatch):
    from nexus.storage import case_notes

    plan = {
        "reply": "Noted.",
        "actions": [
            {"tool": "create_case", "name": "Op Falcon"},
            {"tool": "add_note", "text": "Check the second wallet address.", "case": "last"},
        ],
    }
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("open case Op Falcon and note to check the second wallet",
                  conn=conn, settings=_FakeSettings("gemini"))
        cid = res["actions"][0]["case_id"]
        note_out = res["actions"][1]
        assert note_out["type"] == "note"
        notes = case_notes(conn, cid)
        assert any("second wallet" in n["body"] for n in notes)


def test_act_capture_evidence(temp_db, monkeypatch):
    captured = {}

    def _fake_capture(item_id, url, settings=None):
        captured["item_id"] = item_id
        captured["url"] = url
        return {"ok": True, "screenshot": "evidence/x.png", "sha256": "abc"}

    monkeypatch.setattr("nexus.evidence.capture_evidence", _fake_capture)
    plan = {"reply": "Saving proof.",
            "actions": [{"tool": "capture_evidence", "query": "Neymar"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        upsert_item(conn, RawItem(
            source="rss", title="Neymar transfer confirmed",
            content="Deal done.", url="https://example.com/neymar-deal"))
        res = act("save evidence of the Neymar story", conn=conn,
                  settings=_FakeSettings("gemini"))
        out = res["actions"][0]
        assert out["type"] == "evidence"
        assert captured["url"] == "https://example.com/neymar-deal"
        assert out["url"].startswith("/?q=")


def test_act_capture_evidence_no_match_is_graceful(temp_db, monkeypatch):
    def _fake_capture(item_id, url, settings=None):
        raise AssertionError("should not be called when there is no matching item")

    monkeypatch.setattr("nexus.evidence.capture_evidence", _fake_capture)
    plan = {"reply": "ok",
            "actions": [{"tool": "capture_evidence", "query": "nothinghere"}]}
    fake = _FakePlanProvider(plan)
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        res = act("capture proof of nothinghere", conn=conn,
                  settings=_FakeSettings("gemini"))
    out = res["actions"][0]
    assert out["type"] == "error"
    assert "couldn't find" in out["label"].lower()


def test_expanded_actions_protocol_reaches_model(temp_db, monkeypatch):
    fake = _FakePlanProvider({"reply": "ok", "actions": []})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        act("hi", conn=conn, settings=_FakeSettings("gemini"))
    sp = fake.system_prompt
    for tool in ("run_scan", "search_feed", "add_watchlist", "add_requirement",
                 "add_note", "capture_evidence"):
        assert tool in sp
    # The expanded scope text is present and still forbids destructive power.
    assert "can NOT delete anything" in sp


# --- context-awareness: Sherlock knows the page and the active case ----------


class _CaptureProvider:
    """Provider double that records the prompts it was given."""

    def __init__(self, plan: dict):
        self._plan = plan
        self.system_prompt = None
        self.user_prompt = None

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return json.dumps(self._plan)


def test_describe_current_resolves_page_and_active_case(temp_db):
    with temp_db() as conn:
        cid = create_case(conn, "Falcon Watch")
        set_active_case(conn, cid)
        # A case sub-page resolves to the Cases label by prefix.
        text = _describe_current(conn, f"/cases/{cid}?tab=items")
    assert f"/cases/{cid}" in text
    assert "Cases" in text                 # the friendly page label
    assert "Falcon Watch" in text          # the active case is surfaced
    assert "this case" in text.lower()     # guidance to resolve "this case"


def test_describe_current_maps_root_to_feed(temp_db):
    with temp_db() as conn:
        text = _describe_current(conn, "/")
    assert "Feed" in text


def test_act_injects_current_context_into_prompt(temp_db, monkeypatch):
    fake = _CaptureProvider({"reply": "Noted.", "actions": []})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        cid = create_case(conn, "Harbor Case")
        set_active_case(conn, cid)
        act("make a note here that this matters", conn=conn,
            settings=_FakeSettings("gemini"), page=f"/cases/{cid}")
    # The page and active case reached the model as CURRENT CONTEXT.
    assert "CURRENT CONTEXT" in fake.user_prompt
    assert f"/cases/{cid}" in fake.user_prompt
    assert "Harbor Case" in fake.user_prompt


def test_act_without_page_has_no_context_block(temp_db, monkeypatch):
    fake = _CaptureProvider({"reply": "ok", "actions": []})
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda settings: fake)
    with temp_db() as conn:
        act("hello", conn=conn, settings=_FakeSettings("gemini"))
    # No page and no active case -> the optional context block is omitted.
    assert "CURRENT CONTEXT" not in fake.user_prompt
