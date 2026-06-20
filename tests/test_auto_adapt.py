"""Tests for Auto-Adapt — self-healing custom-source field mapping.

Covers the drift bookkeeping (record_custom_source_health), the mapping-only
re-map (apply_custom_source_remap), and the orchestration guardrails in
maybe_adapt (threshold, AI-availability, cooldown, sample requirement). The AI
call is stubbed so tests stay offline and deterministic.

Everything runs against the conftest temp_db connection — no network, no real AI.
"""

from __future__ import annotations

import nexus.sources.auto_adapt as aa
from nexus.storage import (
    apply_custom_source_remap,
    create_custom_source,
    get_custom_source,
    record_custom_source_health,
    update_custom_source,
)


def _make_source(conn, **over):
    cfg = {
        "name": "Drifty API",
        "base_url": "https://api.example.com",
        "endpoint": "/v1/posts",
        "auth_type": "none",
        "items_path": "data",
        "map_content": "text",
        "map_title": "title",
    }
    cfg.update(over)
    return create_custom_source(conn, cfg)


# --------------------------------------------------------- health bookkeeping
def test_responsive_but_empty_increments_and_stores_sample(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn)
        n1 = record_custom_source_health(conn, sid, responded=True, mapped=0,
                                         sample='{"results": []}')
        n2 = record_custom_source_health(conn, sid, responded=True, mapped=0,
                                         sample='{"results": []}')
        assert (n1, n2) == (1, 2)
        row = get_custom_source(conn, sid)
        assert row["last_sample"] == '{"results": []}'


def test_successful_scan_resets_counter(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn)
        record_custom_source_health(conn, sid, responded=True, mapped=0)
        record_custom_source_health(conn, sid, responded=True, mapped=0)
        n = record_custom_source_health(conn, sid, responded=True, mapped=5)
        assert n == 0
        assert get_custom_source(conn, sid)["consecutive_empty"] == 0


def test_network_failure_does_not_increment(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn)
        record_custom_source_health(conn, sid, responded=True, mapped=0)
        # responded=False (transient outage) must leave the counter alone.
        n = record_custom_source_health(conn, sid, responded=False, mapped=0)
        assert n == 1


def test_manual_edit_resets_drift(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn)
        record_custom_source_health(conn, sid, responded=True, mapped=0)
        record_custom_source_health(conn, sid, responded=True, mapped=0)
        update_custom_source(conn, sid, {**get_custom_source(conn, sid),
                                         "name": "Renamed"})
        assert get_custom_source(conn, sid)["consecutive_empty"] == 0


# ----------------------------------------------------------- mapping-only remap
def test_remap_changes_only_mapping_fields(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn, base_url="https://api.example.com",
                           auth_type="bearer", auth_param="Authorization")
        apply_custom_source_remap(conn, sid, {
            "items_path": "response.items",
            "map_content": "body.text",
            # A planner reply may include connection fields — they must be ignored.
            "base_url": "https://evil.example.net",
            "auth_type": "none",
        })
        row = get_custom_source(conn, sid)
        assert row["items_path"] == "response.items"
        assert row["map_content"] == "body.text"
        # Connection/auth preserved despite being present in the remap dict.
        assert row["base_url"] == "https://api.example.com"
        assert row["auth_type"] == "bearer"
        # Drift reset + timestamp stamped.
        assert row["consecutive_empty"] == 0
        assert row["last_adapt_at"]


# --------------------------------------------------------------- maybe_adapt
def test_maybe_adapt_below_threshold_is_noop(temp_db):
    with temp_db() as conn:
        sid = _make_source(conn)
        record_custom_source_health(conn, sid, responded=True, mapped=0,
                                    sample='{"x": 1}')  # count = 1, threshold = 2
        result = aa.maybe_adapt(conn, sid)
        assert result["adapted"] is False
        assert result["reason"] == "below drift threshold"


def test_maybe_adapt_no_provider_is_noop(temp_db, monkeypatch):
    # Force "no AI provider connected" regardless of host config.
    monkeypatch.setattr(aa, "get_provider", lambda settings: None)
    with temp_db() as conn:
        sid = _make_source(conn)
        for _ in range(aa.DRIFT_THRESHOLD):
            record_custom_source_health(conn, sid, responded=True, mapped=0,
                                        sample='{"x": 1}')
        result = aa.maybe_adapt(conn, sid)
        assert result["adapted"] is False
        assert result["reason"] == "no AI provider connected"


def test_maybe_adapt_success_remaps(temp_db, monkeypatch):
    monkeypatch.setattr(aa, "get_provider", lambda settings: object())
    monkeypatch.setattr(
        aa, "plan_source",
        lambda sample=None, hint=None, settings=None: {
            "ok": True,
            "config": {"items_path": "new.path", "map_content": "new.text"},
        },
    )
    with temp_db() as conn:
        sid = _make_source(conn, items_path="old", map_content="old")
        for _ in range(aa.DRIFT_THRESHOLD):
            record_custom_source_health(conn, sid, responded=True, mapped=0,
                                        sample='{"new": {"path": []}}')
        result = aa.maybe_adapt(conn, sid)
        assert result["adapted"] is True
        assert "items_path" in result["changed"]
        row = get_custom_source(conn, sid)
        assert row["items_path"] == "new.path"
        assert row["map_content"] == "new.text"
        assert row["consecutive_empty"] == 0


def test_maybe_adapt_cooldown_blocks_repeat(temp_db, monkeypatch):
    monkeypatch.setattr(aa, "get_provider", lambda settings: object())
    monkeypatch.setattr(
        aa, "plan_source",
        lambda sample=None, hint=None, settings=None: {
            "ok": True, "config": {"items_path": "z"},
        },
    )
    with temp_db() as conn:
        sid = _make_source(conn)
        for _ in range(aa.DRIFT_THRESHOLD):
            record_custom_source_health(conn, sid, responded=True, mapped=0,
                                        sample='{"z": []}')
        first = aa.maybe_adapt(conn, sid)
        assert first["adapted"] is True
        # Drive it back into drift; the cooldown must now block a second adapt.
        for _ in range(aa.DRIFT_THRESHOLD):
            record_custom_source_health(conn, sid, responded=True, mapped=0,
                                        sample='{"z": []}')
        second = aa.maybe_adapt(conn, sid)
        assert second["adapted"] is False
        assert second["reason"] == "cooldown active"
