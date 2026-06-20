"""Tests for the AI query-builder plan parser.

The parser must be robust to the two failure modes real providers exhibit:
a clean complete JSON object (happy path) and a reply truncated mid-array
because the model hit its token ceiling (very common with local/verbose
models). The truncated case used to return an empty plan and surface
"No queries were generated" in the UI even though the model produced good
queries — these tests lock in the salvage behaviour that fixes that.
"""

from nexus.analysis.query_builder import _parse_plan, _salvage_array


def test_parse_complete_object():
    text = (
        '{"queries": ["a b", "c d"], '
        '"requirements": ["What is X?", "What is Y?"]}'
    )
    plan = _parse_plan(text)
    assert plan["queries"] == ["a b", "c d"]
    assert plan["requirements"] == ["What is X?", "What is Y?"]


def test_parse_object_with_surrounding_prose():
    text = 'Sure! Here is the plan:\n{"queries": ["x"], "requirements": []}\nDone.'
    plan = _parse_plan(text)
    assert plan["queries"] == ["x"]


def test_parse_truncated_reply_salvages_complete_elements():
    # Reply cut off mid-requirements with no closing ] or } — json.loads fails,
    # but the elements emitted so far are good and must be recovered.
    text = (
        '{\n  "queries": [\n'
        '    "Neymar World Cup performance",\n'
        '    "Neymar injury status update",\n'
        '    "Neymar girlfriend Bruna Biancardi"\n'
        '  ],\n  "requirements": [\n'
        '    "What was his World Cup performance?",\n'
        '    "What is the status of his injurie'  # truncated, no closing quote
    )
    plan = _parse_plan(text)
    assert plan["queries"] == [
        "Neymar World Cup performance",
        "Neymar injury status update",
        "Neymar girlfriend Bruna Biancardi",
    ]
    # The half-written final requirement (no closing quote) is dropped.
    assert plan["requirements"] == ["What was his World Cup performance?"]


def test_salvage_array_handles_escaped_quotes():
    text = '"queries": ["say \\"hi\\" now", "second"]'
    assert _salvage_array(text, "queries") == ['say "hi" now', "second"]


def test_parse_empty_on_garbage():
    assert _parse_plan("not json at all")["queries"] == []
    assert _parse_plan("")["requirements"] == []


def test_parse_caps_query_count():
    items = ", ".join(f'"q{i}"' for i in range(20))
    plan = _parse_plan('{"queries": [' + items + '], "requirements": []}')
    assert len(plan["queries"]) == 8  # _MAX_QUERIES
