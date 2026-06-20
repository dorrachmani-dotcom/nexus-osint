"""Tests for the shared source-content cleaner (nexus.textclean).

These lock in the behaviour the feed depends on: HTML/Markdown/garbled source
text is reduced to a single readable plain-text line, common code-like tokens
(``C#``, ``A*``) survive, and — critically — a malformed fragment can never
swallow the legitimate text that follows it.
"""

from __future__ import annotations

from nexus.textclean import clean_text


def test_empty_and_none_are_empty_string():
    assert clean_text(None) == ""
    assert clean_text("") == ""


def test_plain_text_is_unchanged():
    assert clean_text("Plain English text.") == "Plain English text."


def test_html_entities_are_decoded():
    assert clean_text("Cybersecurity &amp; Infrastructure") == "Cybersecurity & Infrastructure"
    assert clean_text("a &#038; b") == "a & b"
    assert clean_text("x &nbsp;&nbsp; y") == "x y"


def test_double_escaped_html_is_decoded():
    # &amp;lt; -> &lt; -> <  (then the resulting tag is stripped).
    assert clean_text("keep &amp;lt;b&amp;gt;this") == "keep this"


def test_html_tags_are_stripped():
    assert clean_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_markdown_image_is_removed():
    out = clean_text("![alt caption](https://ex.com/a.jpg) Real sentence here")
    assert out == "Real sentence here"


def test_markdown_link_keeps_text_drops_url():
    assert clean_text("See [the report](https://ex.com/x) now") == "See the report now"


def test_blockquote_and_heading_markers_removed():
    assert clean_text("> quoted line") == "quoted line"
    assert clean_text("## Heading") == "Heading"


def test_reddit_style_garbage_is_cleaned():
    # Faithful to the real feed: a markdown image whose alt text carries
    # escaped HTML, followed by the actual post body.
    raw = (
        "![Nvidia Spark | Lab Report &gt; &lt;/a&gt; &lt;div style="
        "](https://i0.wp.com/i4.ytimg.com/vi/x/hqdefault.jpg?w=640&amp;ssl=1) "
        "Nvidia just announced the RTX Spark, a brand new product."
    )
    assert clean_text(raw) == "Nvidia just announced the RTX Spark, a brand new product."


def test_malformed_fragment_never_eats_following_text():
    # A broken, unclosed tag must not consume the real sentence after it.
    out = clean_text("oops <div style= the signal survives here")
    assert "the signal survives here" in out


def test_code_like_tokens_survive():
    assert clean_text("Coding in C# and A* search") == "Coding in C# and A* search"


def test_whitespace_is_collapsed_to_single_line():
    assert clean_text("line one\n\n   line two\t\tend") == "line one line two end"


def test_never_raises_on_weird_input():
    # Unbalanced brackets / stray markers must still return a string.
    for bad in ("![](", "[x](", "<<<>>>", "&&&;;;", ">>>> ## ["):
        assert isinstance(clean_text(bad), str)
