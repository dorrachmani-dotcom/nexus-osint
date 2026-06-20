"""Tests for custom-source persistence (CRUD) and input normalisation."""

from __future__ import annotations

import json

from nexus.storage import (
    _clean_custom,
    create_custom_source,
    delete_custom_source,
    get_custom_source,
    list_custom_sources,
    toggle_custom_source,
    update_custom_source,
)


def test_clean_custom_normalises():
    c = _clean_custom(
        {
            "name": "  My Source  ",
            "http_method": "weird",
            "auth_type": "NOPE",
            "extra_params": "{bad json",
        }
    )
    assert c["name"] == "My Source"
    assert c["http_method"] == "GET"
    assert c["auth_type"] == "none"
    assert c["extra_params"] == "{}"
    assert c["enabled"] == 1


def test_clean_custom_accepts_dict_extra_params_and_valid_auth():
    c = _clean_custom(
        {
            "name": "X",
            "http_method": "post",
            "auth_type": "bearer",
            "extra_params": {"limit": 5},
        }
    )
    assert c["http_method"] == "POST"
    assert c["auth_type"] == "bearer"
    assert json.loads(c["extra_params"]) == {"limit": 5}


def test_clean_custom_blank_name_defaults():
    assert _clean_custom({})["name"] == "Custom source"


def test_crud_roundtrip(temp_db):
    with temp_db() as conn:
        sid = create_custom_source(
            conn,
            {
                "name": "S1",
                "base_url": "https://a.test",
                "endpoint": "/v1/items",
                "auth_type": "bearer",
                "http_method": "post",
                "extra_params": {"limit": 5},
                "map_content": "text",
            },
        )

    with temp_db() as conn:
        row = get_custom_source(conn, sid)
    assert row["name"] == "S1"
    assert row["http_method"] == "POST"
    assert row["auth_type"] == "bearer"
    assert row["map_content"] == "text"
    assert json.loads(row["extra_params"]) == {"limit": 5}
    assert row["enabled"] == 1


def test_update_and_toggle_and_delete(temp_db):
    with temp_db() as conn:
        sid = create_custom_source(conn, {"name": "S", "base_url": "https://a.test"})

    with temp_db() as conn:
        update_custom_source(conn, sid, {"name": "Renamed", "base_url": "https://b.test"})
    with temp_db() as conn:
        assert get_custom_source(conn, sid)["name"] == "Renamed"

    with temp_db() as conn:
        toggle_custom_source(conn, sid)
    with temp_db() as conn:
        assert get_custom_source(conn, sid)["enabled"] == 0

    with temp_db() as conn:
        delete_custom_source(conn, sid)
    with temp_db() as conn:
        assert get_custom_source(conn, sid) is None


def test_list_enabled_only(temp_db):
    with temp_db() as conn:
        a = create_custom_source(conn, {"name": "A", "base_url": "https://a.test"})
        b = create_custom_source(conn, {"name": "B", "base_url": "https://b.test"})
        toggle_custom_source(conn, b)  # disable B

    with temp_db() as conn:
        all_rows = list_custom_sources(conn)
        enabled = list_custom_sources(conn, enabled_only=True)
    assert {r["id"] for r in all_rows} == {a, b}
    assert {r["id"] for r in enabled} == {a}
