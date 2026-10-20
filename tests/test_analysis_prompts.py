"""The item-analysis prompt asks for direct, news-style summaries (no live LLM)."""

from __future__ import annotations

from nexus.analysis.prompts import build_system_prompt, build_user_prompt


def test_summary_instruction_is_direct_and_forbids_meta_commentary():
    sp = " ".join(build_system_prompt("English").split())  # ignore line wraps
    assert "ONE neutral sentence" in sp
    assert "states the news" in sp
    for phrase in ("The text claims", "The headline discusses", "The content is a tutorial"):
        assert phrase in sp  # quoted as forbidden examples
    assert "Never describe the item instead of the news" in sp
    assert "When only a headline is available" in sp
    assert "never invent details" in sp


def test_headline_only_items_get_an_explicit_note():
    note = "Only the headline is available"
    assert note in build_user_prompt("Acme recalls routers", "", "en")
    assert note in build_user_prompt("Acme recalls routers", "Acme recalls routers", "en")
    assert note not in build_user_prompt("Acme recalls routers", "Acme said on Monday...", "en")
    assert note not in build_user_prompt(None, None, None)


def test_user_prompt_still_carries_the_item_json():
    up = build_user_prompt("Title", "Body text", "en")
    assert '"title": "Title"' in up and '"content": "Body text"' in up
