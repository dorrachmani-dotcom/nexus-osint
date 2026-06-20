"""Tests for the topic-scoped, paginated feed (nexus.web.app._paged_feed).

Locks in the feed-scoping rule and the pagination math that the home page, the
htmx partial, scan, bulk actions and load-more all share. A "topic" (capsule) is
a source='query' subscription; with topics defined the default feed shows only
matching items, while an explicit search or scope='all' opens the firehose.

Everything runs against the conftest temp_db connection — no network, no AI.
Storage-level invariants (count_matching_items vs search_items) and the bulk
existence guards (get_list / get_case returning None for bogus ids) are covered
directly, matching the existing suite's call-the-function style (no TestClient).
"""

from __future__ import annotations

from nexus.models import RawItem
from nexus.storage import (
    add_subscription,
    count_matching_items,
    get_case,
    get_list,
    search_items,
    upsert_item,
)
from nexus.web.app import FEED_PAGE_SIZE, _paged_feed, _topic_terms


def _add(conn, *, title, content="body", source="rss"):
    """Insert a stored item and return its id (mirrors test_entity_graph)."""
    item_id, _ = upsert_item(
        conn, RawItem(source=source, title=title, content=content)
    )
    return item_id


def _add_topic(conn, term, label="Topic"):
    """Register a tracked-topic capsule term (a source='query' subscription)."""
    add_subscription(conn, "query", term, label)


# --------------------------------------------------------------- topic scoping
def test_no_topics_falls_back_to_everything(temp_db):
    with temp_db() as conn:
        _add(conn, title="alpha report", content="one")
        _add(conn, title="beta report", content="two")

        # No capsules defined -> _topic_terms is empty -> not scoped.
        assert _topic_terms(conn) == []
        page = _paged_feed(conn)

    assert page["topic_scoped"] is False
    assert page["matched"] == 2
    assert len(page["items"]) == 2


def test_topic_scopes_default_feed_to_matching_items(temp_db):
    with temp_db() as conn:
        _add(conn, title="ukraine drone strike", content="frontline")
        _add(conn, title="ukraine aid package", content="budget")
        _add(conn, title="weather forecast", content="sunny skies")
        _add_topic(conn, "ukraine")

        page = _paged_feed(conn)

    # Only the two "ukraine" items survive the default topic scope.
    assert page["topic_scoped"] is True
    assert page["matched"] == 2
    titles = {it["title"] for it in page["items"]}
    assert titles == {"ukraine drone strike", "ukraine aid package"}


def test_explicit_query_overrides_topic_scoping(temp_db):
    with temp_db() as conn:
        _add(conn, title="ukraine drone strike", content="frontline")
        _add(conn, title="weather forecast", content="sunny skies")
        _add_topic(conn, "ukraine")

        # An explicit search must ignore the capsule terms entirely.
        page = _paged_feed(conn, q="weather")

    assert page["topic_scoped"] is False
    assert page["matched"] == 1
    assert page["items"][0]["title"] == "weather forecast"


def test_scope_all_ignores_topics(temp_db):
    with temp_db() as conn:
        _add(conn, title="ukraine drone strike", content="frontline")
        _add(conn, title="weather forecast", content="sunny skies")
        _add(conn, title="market open", content="stocks rally")
        _add_topic(conn, "ukraine")

        page = _paged_feed(conn, scope="all")

    # scope=all opens the firehose: every item, no scoping.
    assert page["topic_scoped"] is False
    assert page["matched"] == 3
    assert len(page["items"]) == 3


# ------------------------------------------------------------ pagination math
def test_pagination_full_page_has_more(temp_db):
    extra = 7
    total = FEED_PAGE_SIZE + extra
    with temp_db() as conn:
        for n in range(total):
            _add(conn, title=f"item {n}", content=f"content {n}")
        page = _paged_feed(conn)

    assert page["matched"] == total
    assert len(page["items"]) == FEED_PAGE_SIZE
    assert page["has_more"] is True
    assert page["next_offset"] == FEED_PAGE_SIZE
    assert page["remaining"] == total - FEED_PAGE_SIZE


def test_pagination_partial_page_no_more(temp_db):
    count = 5
    with temp_db() as conn:
        for n in range(count):
            _add(conn, title=f"item {n}", content=f"content {n}")
        page = _paged_feed(conn)

    assert page["matched"] == count
    assert len(page["items"]) == count
    assert page["has_more"] is False
    assert page["next_offset"] == count
    assert page["remaining"] == 0


def test_pagination_offset_walks_second_page(temp_db):
    extra = 3
    total = FEED_PAGE_SIZE + extra
    with temp_db() as conn:
        for n in range(total):
            _add(conn, title=f"item {n}", content=f"content {n}")
        page2 = _paged_feed(conn, offset=FEED_PAGE_SIZE)

    # The tail page holds only the overflow and reports no further pages.
    assert len(page2["items"]) == extra
    assert page2["has_more"] is False
    assert page2["remaining"] == 0


# ------------------------------------------- count agrees with search listing
def test_count_matching_items_agrees_with_search(temp_db):
    with temp_db() as conn:
        _add(conn, title="signal one", content="x", source="rss")
        _add(conn, title="signal two", content="y", source="rss")
        _add(conn, title="signal three", content="z", source="reddit")

        # Unfiltered.
        assert count_matching_items(conn) == len(
            search_items(conn, limit=10_000)
        )
        # Facet filter (source) must stay in lock-step.
        assert count_matching_items(conn, source="rss") == len(
            search_items(conn, source="rss", limit=10_000)
        )
        # Free-text filter must stay in lock-step.
        assert count_matching_items(conn, q="signal") == len(
            search_items(conn, q="signal", limit=10_000)
        )


# -------------------------------------------- bulk routes existence guard
def test_bulk_guard_returns_none_for_missing_list_and_case(temp_db):
    # The bulk routes only add when get_list / get_case is not None, so a bogus
    # id is a graceful no-op rather than a FOREIGN KEY crash.
    with temp_db() as conn:
        assert get_list(conn, 999999) is None
        assert get_case(conn, 999999) is None
