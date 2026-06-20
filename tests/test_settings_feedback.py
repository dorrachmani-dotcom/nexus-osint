"""Guard test: a saved setting confirms itself.

base.html surfaces *failures* globally (test_action_feedback.py). The other
half of "a click never looks like it did nothing" is the success path: saving
an API key or the translation URL swaps in a partial that must visibly say so.
Unlike a feed action — where the swapped result is self-evidently the proof —
a settings save returns the same form, so without an explicit confirmation a
non-technical operator can't tell the save took. This locks those inline
confirmations in so they can't be silently dropped.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = (
    Path(__file__).resolve().parent.parent / "nexus" / "web" / "templates"
)


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )


def test_secrets_form_confirms_only_after_a_save():
    tmpl = _env().get_template("_secrets_form.html")
    # No fields needed: the confirmation is independent of the key list.
    ctx = {"secret_fields": [], "secret_status": {}}

    saved = tmpl.render(saved=True, **ctx)
    assert "saved &amp; reloaded" in saved, "no confirmation after a save"

    untouched = tmpl.render(saved=False, **ctx)
    assert "saved &amp; reloaded" not in untouched, (
        "confirmation must not show when nothing was saved"
    )


def test_translation_form_surfaces_its_result_message():
    # The translation save reports success OR a refusal reason via the same slot,
    # colour-coded by translation_saved. Lock the message slot in.
    html = (TEMPLATES / "_settings_left.html").read_text(encoding="utf-8")
    assert "translation_msg" in html, "translation save has no result message slot"
    # Success is green, a refusal is red — both must be possible.
    assert "translation_saved" in html, "result message is not success/failure aware"
