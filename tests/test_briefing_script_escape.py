"""Regression: collected text must not break out of the AI-briefing <script>.

Item titles are attacker-controlled (they come from scraped sources). The case
briefing embeds them in an inline script, so a title containing ``</script>``
must be escaped by ``|tojson`` rather than closing the script tag.
"""

from nexus.web.app import TEMPLATES

PAYLOAD = "</script><script>alert(1)</script>"


def test_briefing_escapes_script_breakout() -> None:
    html = TEMPLATES.env.get_template("_case_briefing.html").render(
        case={"id": 1},
        briefing="See Item 1.",
        count=1,
        items_json=[{"id": 1, "title": PAYLOAD}],
    )
    # Exactly one closing tag: the template's own. The payload's is escaped.
    assert html.count("</script>") == 1
    assert "alert(1)" in html  # the text survives, just inert
    assert "\\u003c/script\\u003e" in html
