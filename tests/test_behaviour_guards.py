"""Behaviour of the largest functions, pinned where other tests are thin.

Sherlock's tool dispatch, the order in which allow-list labels win, and the shape
of a bundle import. All names are fictional.
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from nexus import assistant as A
from nexus.security import egress
from nexus.transfer import BUNDLE_MAGIC, BUNDLE_VERSION, import_bundle

SETTINGS = SimpleNamespace(active_provider=lambda: "gemini")
PLAIN = {"run_scan": "_do_scan", "search_feed": "_do_search", "add_watchlist": "_do_add_watchlist",
         "add_requirement": "_do_add_requirement", "capture_evidence": "_do_capture_evidence",
         "schedule_daily_brief": "_do_schedule_daily_brief"}
WITH_CONTEXT = {"create_case": "_do_create_case", "add_items_to_case": "_do_add_items",
                "generate_report": "_do_generate_report", "open_page": "_do_open_page",
                "add_note": "_do_add_note", "add_case_term": "_do_add_case_term",
                "add_case_question": "_do_add_case_question", "create_subcase": "_do_create_subcase"}


class Model:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system, user, max_tokens=0):
        return self.complete(system, user, max_tokens)

    def complete(self, system, user, max_tokens=0, **_kwargs):
        self.prompts.append((system, user))
        return self.replies.pop(0) if self.replies else ""


def _act(temp_db, monkeypatch, model, **kwargs):
    monkeypatch.setattr("nexus.analysis.providers.get_provider", lambda s: model)
    with temp_db() as conn:
        return A.act(kwargs.pop("question", "please look into the depot"), conn=conn,
                     settings=SETTINGS, **kwargs)


def _with_actions(*actions, reply="On it.") -> str:
    return json.dumps({"reply": reply, "actions": list(actions)})


def _record_calls(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    for name in (*PLAIN.values(), *WITH_CONTEXT.values(), "_do_prepare_publish"):
        def fake(*args, _name=name):
            calls.append((_name, args))
            return {"type": _name}

        monkeypatch.setattr(A, name, fake)
    return calls


def test_every_allowed_tool_is_covered_here():
    assert {*PLAIN, *WITH_CONTEXT, *A._PUBLISH_TOOLS} == A._ALLOWED_TOOLS
    assert set(A._TOOLS) == A._ALLOWED_TOOLS


@pytest.mark.parametrize("tool", sorted(WITH_CONTEXT))
def test_context_tools_get_the_connection_context_and_spec(temp_db, monkeypatch, tool):
    calls = _record_calls(monkeypatch)
    spec = {"tool": tool, "name": "Harbor"}
    res = _act(temp_db, monkeypatch, Model(_with_actions(spec)))
    assert res["actions"] == [{"type": WITH_CONTEXT[tool]}]
    name, args = calls[0]
    assert name == WITH_CONTEXT[tool] and args[2] == spec
    assert args[1]["last_case_id"] is None and "by_name" in args[1]


@pytest.mark.parametrize("tool, name, shape", [
    ("run_scan", "_do_scan", ("settings",)),
    ("search_feed", "_do_search", ("conn", "spec")),
    ("add_watchlist", "_do_add_watchlist", ("conn", "spec")),
    ("add_requirement", "_do_add_requirement", ("conn", "spec")),
    ("capture_evidence", "_do_capture_evidence", ("conn", "spec", "settings")),
    ("schedule_daily_brief", "_do_schedule_daily_brief", ("conn", "spec", "settings")),
])
def test_other_tools_get_exactly_their_arguments(temp_db, monkeypatch, tool, name, shape):
    calls = _record_calls(monkeypatch)
    spec = {"tool": tool}
    _act(temp_db, monkeypatch, Model(_with_actions(spec)))
    assert calls[0][0] == name and len(calls[0][1]) == len(shape)
    for value, kind in zip(calls[0][1], shape, strict=True):
        if kind == "spec":
            assert value == spec
        elif kind == "settings":
            assert value is SETTINGS


@pytest.mark.parametrize("tool", sorted(A._PUBLISH_TOOLS))
def test_publish_tools_only_prepare_a_card(temp_db, monkeypatch, tool):
    calls = _record_calls(monkeypatch)
    spec = {"tool": tool}
    _act(temp_db, monkeypatch, Model(_with_actions(spec)), question="save the depot notes")
    name, args = calls[0]
    assert name == "_do_prepare_publish"
    assert args[1:] == ("save the depot notes", tool, spec)


def test_a_failing_step_is_reported_and_the_rest_still_run(temp_db, monkeypatch):
    calls = _record_calls(monkeypatch)

    def boom(*_args):
        raise RuntimeError("no")

    monkeypatch.setattr(A, "_do_scan", boom)
    res = _act(temp_db, monkeypatch, Model(_with_actions({"tool": "run_scan"}, {"tool": "open_page"})))
    assert res["actions"] == [{"type": "error", "label": "One step couldn't be completed."},
                              {"type": "_do_open_page"}]
    assert [c[0] for c in calls] == ["_do_open_page"]


def test_junk_and_disallowed_actions_are_skipped_and_the_count_is_capped(temp_db, monkeypatch):
    _record_calls(monkeypatch)
    many = [{"tool": "open_page"}] * (A._MAX_ACTIONS + 3)
    res = _act(temp_db, monkeypatch, Model(_with_actions("junk", {"tool": "rm_rf"}, *many)))
    assert len(res["actions"]) == A._MAX_ACTIONS - 2  # the two bad ones used up slots


def test_actions_can_find_a_case_by_name(temp_db, monkeypatch):
    from nexus.storage import create_case

    calls = _record_calls(monkeypatch)
    monkeypatch.setattr("nexus.analysis.providers.get_provider",
                        lambda s: Model(_with_actions({"tool": "add_note"})))
    with temp_db() as conn:
        cid = create_case(conn, "Harbor Lantern")
        A.act("please look into the depot", conn=conn, settings=SETTINGS)
    assert calls[0][1][1]["by_name"] == {"harbor lantern": cid}


def test_empty_reply_says_done_only_when_something_ran(temp_db, monkeypatch):
    _record_calls(monkeypatch)
    ran = _act(temp_db, monkeypatch, Model(_with_actions({"tool": "open_page"}, reply="")))
    assert ran["answer"] == "Done."
    idle = _act(temp_db, monkeypatch, Model(_with_actions(reply="")))
    assert idle["answer"].startswith("I'm not sure how to help")


def test_prose_reply_is_used_as_the_answer(temp_db, monkeypatch):
    res = _act(temp_db, monkeypatch, Model("The depot closed in spring."))
    assert res["ok"] and res["answer"] == "The depot closed in spring." and res["actions"] == []
    assert "items" in res


def test_empty_reply_falls_back_to_plain_text_once(temp_db, monkeypatch):
    model = Model("", "A plain answer.")
    res = _act(temp_db, monkeypatch, model)
    assert res["ok"] and res["answer"] == "A plain answer." and len(model.prompts) == 2


def test_nothing_at_all_is_an_error(temp_db, monkeypatch):
    res = _act(temp_db, monkeypatch, Model("", ""))
    assert not res["ok"] and res["error"] == "The model returned an empty response. Please rephrase."
    assert "items" not in res


def test_the_prompts_carry_each_section(temp_db, monkeypatch):
    monkeypatch.setattr(A, "_describe_current", lambda conn, page: f"viewing {page}")
    monkeypatch.setattr(A, "_format_history", lambda h: "EARLIER: hello")
    monkeypatch.setattr(A, "_deep_gather", lambda *_a: "found more")
    monkeypatch.setattr(A, "_graph_brief", lambda conn: "two hubs")
    model = Model(_with_actions())
    _act(temp_db, monkeypatch, model, page="/cases", deep=True, cite=True,
         history=[{"role": "user", "content": "hello"}])
    system, user = model.prompts[0]
    assert A._CITE_GUIDE in system
    order = ["DATA AVAILABLE TO YOU", "RELATIONSHIP SUMMARY:\ntwo hubs",
             "ADDITIONAL RESEARCH (you ran these searches):\nfound more",
             "CURRENT CONTEXT", "viewing /cases", "APP STATUS:\nEmail for the daily brief: NOT",
             "EARLIER: hello", "Analyst's request: please look into the depot"]
    places = [user.index(text) for text in order]
    assert places == sorted(places)


def test_the_plain_prompt_leaves_optional_sections_out(temp_db, monkeypatch):
    monkeypatch.setattr(A, "_describe_current", lambda conn, page: "")
    model = Model(_with_actions())
    _act(temp_db, monkeypatch, model, cite=False)
    system, user = model.prompts[0]
    assert A._CITE_GUIDE not in system
    assert "CURRENT CONTEXT" not in user and "RELATIONSHIP SUMMARY" not in user


def test_failed_deep_research_does_not_stop_the_answer(temp_db, monkeypatch):
    def boom(*_a):
        raise RuntimeError("no")

    monkeypatch.setattr(A, "_deep_gather", boom)
    assert _act(temp_db, monkeypatch, Model(_with_actions()), deep=True)["ok"]


# ---------------------------------------------------------------- egress labels
def _settings(**extra) -> SimpleNamespace:
    base = {"rss_feed_list": [], "ollama_base_url": "", "telemetry_base_url": "",
            "local_llm_base_url": ""}
    return SimpleNamespace(**{**base, **extra})


def _allow(monkeypatch, settings) -> dict[str, str]:
    monkeypatch.setattr("nexus.config.get_settings", lambda: settings)
    egress._allow_cache = None
    try:
        return egress._configured_allow()
    finally:
        egress._allow_cache = None


def test_settings_hosts_get_their_labels(temp_db, monkeypatch):
    allow = _allow(monkeypatch, _settings(
        rss_feed_list=["https://feed.example/rss"], ollama_base_url="http://gpu.example:11434",
        telemetry_base_url="https://tele.example", local_llm_base_url="http://llm.example:1234"))
    assert allow["feed.example"] == "Source (your RSS feed)"
    assert allow["gpu.example"] == "AI provider (Ollama)"
    assert allow["tele.example"] == "Source (Telegram / Telemetry)"
    assert allow["llm.example"] == "AI provider (local OpenAI-compatible server)"


def test_a_broken_feed_list_falls_back_to_the_raw_setting(temp_db, monkeypatch):
    class Broken(SimpleNamespace):
        @property
        def rss_feed_list(self):
            raise ValueError("no")

    broken = Broken(rss_feeds="https://a.example/x, https://b.example/y", ollama_base_url="",
                    telemetry_base_url="", local_llm_base_url="")
    allow = _allow(monkeypatch, broken)
    assert allow["a.example"] == allow["b.example"] == "Source (your RSS feed)"


def test_later_kinds_of_source_win_for_the_same_host(temp_db, monkeypatch):
    from nexus.storage.confluence_sources import create_confluence_source
    from nexus.storage.custom_sources import create_custom_source
    from nexus.storage.web_sources import create_web_source

    url = "https://shared.example"
    settings = _settings(rss_feed_list=[url], ollama_base_url=url, telemetry_base_url=url,
                         local_llm_base_url=url)
    labels = []
    with temp_db() as conn:
        labels.append(_allow(monkeypatch, settings)["shared.example"])
        create_custom_source(conn, {"name": "Api", "base_url": url})
        conn.commit()
        labels.append(_allow(monkeypatch, settings)["shared.example"])
        create_web_source(conn, {"name": "Web", "url": url})
        conn.commit()
        labels.append(_allow(monkeypatch, settings)["shared.example"])
        create_confluence_source(conn, {"name": "Wiki", "deployment": "cloud", "base_url": url,
                                        "email": "a@example.org"})
        conn.commit()
        labels.append(_allow(monkeypatch, settings)["shared.example"])
    assert labels == ["AI provider (local OpenAI-compatible server)", "Source (your custom API)",
                      "Source (your watched web page)", "Source (your Confluence)"]


def test_disabled_sources_are_not_expected(temp_db, monkeypatch):
    from nexus.storage.custom_sources import create_custom_source

    with temp_db() as conn:
        create_custom_source(conn, {"name": "Api", "base_url": "https://off.example", "enabled": 0})
        conn.commit()
        assert "off.example" not in _allow(monkeypatch, _settings())


def test_a_database_problem_keeps_the_settings_hosts(temp_db, monkeypatch):
    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr("nexus.db.get_connection", boom)
    allow = _allow(monkeypatch, _settings(ollama_base_url="http://gpu.example:11434"))
    assert allow == {"gpu.example": "AI provider (Ollama)"}


def test_unreadable_settings_keep_the_database_hosts(temp_db, monkeypatch):
    from nexus.storage.web_sources import create_web_source

    with temp_db() as conn:
        create_web_source(conn, {"name": "Web", "url": "https://docs.example/x"})
    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr("nexus.config.get_settings", boom)
    egress._allow_cache = None
    try:
        assert egress._configured_allow() == {"docs.example": "Source (your watched web page)"}
    finally:
        egress._allow_cache = None


# ---------------------------------------------------------------- bundle import
def _bundle(items: list[dict], *, case: dict | None = None, files: dict | None = None) -> bytes:
    payload = {"manifest": {"magic": BUNDLE_MAGIC, "version": BUNDLE_VERSION}, "items": items}
    if case:
        payload["case"] = case
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("data.json", json.dumps(payload))
        for name, data in (files or {}).items():
            zf.writestr(name, data)
    return out.getvalue()


def _record(chash: str = "h1", **extra) -> dict:
    return {"content_hash": chash, "title": "Depot", "content": "Body", "source": "rss",
            "track": "news", **extra}


ANALYSIS = {"threat_level": "low", "summary": "A depot."}
SCORE = {"question": "Who owns the depot?", "priority": 1, "score": 80,
         "scored_at": "2026-10-01T10:00:00Z"}
EVIDENCE = {"filename": "../../shot.png", "sha256": "abc", "ocr_text": "text"}


def test_the_summary_keeps_its_shape_and_counts_every_kind(temp_db):
    blob = _bundle([_record(analysis=ANALYSIS, requirements=[SCORE, {"question": " "}],
                            evidence=[EVIDENCE, {"filename": ""}]),
                    {"title": "no hash"}],
                   files={"evidence/shot.png": b"png"})
    with temp_db() as conn:
        summary = import_bundle(conn, blob)
        again = import_bundle(conn, blob)
    assert list(summary.items()) == [
        ("new_items", 1), ("skipped_items", 0), ("analyses_added", 1), ("scores_added", 1),
        ("evidence_added", 1), ("total_in_bundle", 2)]
    assert list(again.items()) == [
        ("new_items", 0), ("skipped_items", 1), ("analyses_added", 0), ("scores_added", 0),
        ("evidence_added", 0), ("total_in_bundle", 2)]


def test_a_score_without_a_timestamp_is_kept(temp_db):
    # scored_at is NOT NULL: inserting NULL with OR IGNORE lost the row silently.
    undated = {"question": "Who owns the depot?", "priority": 1, "score": 55}
    blank = {"question": "Who runs the depot?", "score": 40, "scored_at": ""}
    with temp_db() as conn:
        summary = import_bundle(conn, _bundle([_record(requirements=[undated, blank])]))
        rows = conn.execute(
            "SELECT score, scored_at FROM requirement_hits ORDER BY score").fetchall()
    assert summary["scores_added"] == 2
    assert [r["score"] for r in rows] == [40, 55]
    assert all(r["scored_at"] for r in rows)


def test_an_existing_item_gains_what_it_lacks(temp_db):
    with temp_db() as conn:
        import_bundle(conn, _bundle([_record()]))
        summary = import_bundle(conn, _bundle([_record(analysis=ANALYSIS, requirements=[SCORE])]))
        row = conn.execute("SELECT summary FROM items").fetchone()
    assert summary["skipped_items"] == 1 and summary["analyses_added"] == 1
    assert summary["scores_added"] == 1 and row["summary"] == "A depot."


def test_evidence_is_stored_under_its_base_name_even_without_its_file(temp_db):
    with temp_db() as conn:
        import_bundle(conn, _bundle([_record(evidence=[EVIDENCE])]))
        row = conn.execute("SELECT screenshot, sha256, ocr_text FROM evidence").fetchone()
    assert tuple(row) == ("evidence/shot.png", "abc", "text")


def test_a_missing_fetched_time_is_filled_in(temp_db):
    with temp_db() as conn:
        import_bundle(conn, _bundle([_record()]))
        assert conn.execute("SELECT fetched_at FROM items").fetchone()[0]


def test_the_case_is_rebuilt_around_its_items(temp_db):
    case = {"name": "Harbor Lantern", "description": "Port work"}
    with temp_db() as conn:
        import_bundle(conn, _bundle([_record()], case=case))
        assert conn.execute("SELECT description FROM cases").fetchone()[0] == "Port work"


@pytest.mark.parametrize("blob, message", [
    (b"not a zip", "not a ZIP"),
    (_bundle([]).replace(b"data.json", b"data.jsox"), "no data.json"),
])
def test_bad_files_are_refused(temp_db, blob, message):
    with temp_db() as conn, pytest.raises(ValueError, match=message):
        import_bundle(conn, blob)


def _zip_with(data: bytes, manifest: bytes | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("data.json", data)
        if manifest:
            zf.writestr("manifest.json", manifest)
    return out.getvalue()


@pytest.mark.parametrize("data, manifest, message", [
    (b"\xff", None, "corrupted or unreadable"),
    (b'{"manifest": {"magic": "other"}}', None, "not a Nexus intelligence bundle"),
    (json.dumps({"manifest": {"magic": BUNDLE_MAGIC, "version": BUNDLE_VERSION + 1}}).encode(),
     None, "newer version"),
    (json.dumps({"manifest": {"magic": BUNDLE_MAGIC, "version": BUNDLE_VERSION}}).encode(),
     b'{"data_sha256": "wrong"}', "appears to be corrupted"),
])
def test_bad_contents_are_refused(temp_db, data, manifest, message):
    with temp_db() as conn, pytest.raises(ValueError, match=message):
        import_bundle(conn, _zip_with(data, manifest))
