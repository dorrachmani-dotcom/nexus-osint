"""Tests for the air-gap transfer bundle (export -> import round-trip).

The transfer feature moves collected intelligence from an online machine to an
offline one via a portable ``.nexusbundle`` (a ZIP). Items are keyed on the
natural ``content_hash`` so importing is idempotent and never collides with the
receiving machine's own AUTOINCREMENT ids. Bundles must carry items, analyses,
question scores and evidence — but never secrets — and importing twice must be a
no-op the second time.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from nexus.config import get_settings
from nexus.models import Analysis, RawItem, ThreatLevel
from nexus.storage import save_analysis, upsert_item
from nexus.transfer import BUNDLE_MAGIC, BUNDLE_VERSION, export_bundle, import_bundle


def _seed_one(conn, *, title="Breaking development", source="rss",
              url="https://example.com/story") -> int:
    iid, _ = upsert_item(
        conn,
        RawItem(
            source=source,
            title=title,
            content="Something significant happened in the region today.",
            url=url,
        ),
    )
    save_analysis(
        conn,
        iid,
        Analysis(
            threat_level=ThreatLevel.HIGH,
            summary="Concise AI summary of the event.",
            translation="English translation of the item.",
            entity_groups={"people": ["Alice"], "organizations": ["Acme"]},
            entities=[],
        ),
    )
    return iid


def test_export_produces_valid_zip_bundle(temp_db):
    with temp_db() as conn:
        _seed_one(conn)
        blob, summary = export_bundle(conn, scope="all")

    assert summary["item_count"] == 1
    assert summary["scope"] == "all"
    # A bundle is a real ZIP carrying a manifest + data payload.
    import io

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "data.json" in names


def _wipe_items(conn) -> None:
    """Simulate a fresh receiving machine: drop the collected rows.

    (The shared ``temp_db`` fixture points every connection at the same file, so
    we clear the tables to stand in for the empty offline database.)
    """
    # Deleting items cascades to its children (analyses, evidence, bookmarks,
    # requirement_hits, ...). Clusters are referenced *by* items with no cascade,
    # so they go afterwards.
    conn.execute("DELETE FROM items")
    conn.execute("DELETE FROM clusters")


def test_roundtrip_imports_items_and_analysis(temp_db):
    # Build a bundle from a populated DB, then clear it (the offline machine).
    with temp_db() as conn:
        _seed_one(conn)
        blob, _ = export_bundle(conn, scope="all")
        _wipe_items(conn)

        result = import_bundle(conn, blob)
        assert result["new_items"] == 1
        assert result["analyses_added"] == 1
        rows = conn.execute(
            "SELECT title FROM items WHERE title = ?", ("Breaking development",)
        ).fetchall()
        assert len(rows) == 1
        an = conn.execute("SELECT summary FROM analyses").fetchone()
        assert an["summary"] == "Concise AI summary of the event."


def test_import_is_idempotent(temp_db):
    with temp_db() as conn:
        _seed_one(conn)
        blob, _ = export_bundle(conn, scope="all")
        _wipe_items(conn)

        first = import_bundle(conn, blob)
        assert first["new_items"] == 1
        second = import_bundle(conn, blob)
        assert second["new_items"] == 0
        assert second["skipped_items"] == 1
        # Still exactly one copy of the item.
        n = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        assert n == 1


def test_only_new_export_advances_watermark(temp_db):
    with temp_db() as conn:
        _seed_one(conn, title="First", url="https://example.com/1")
        blob1, sum1 = export_bundle(conn, scope="all", only_new=True)
        assert sum1["item_count"] == 1  # first run carries everything

        # A second only_new export with nothing added carries zero items.
        blob2, sum2 = export_bundle(conn, scope="all", only_new=True)
        assert sum2["item_count"] == 0


def test_bundle_contains_no_secrets(temp_db):
    with temp_db() as conn:
        _seed_one(conn)
        blob, _ = export_bundle(conn, scope="all")

    lowered = blob  # bytes; scan for obvious secret-bearing keys
    for needle in (b"SERPAPI_KEY", b"GEMINI_API_KEY", b"api_key", b"ANTHROPIC"):
        assert needle not in lowered


def test_import_rejects_non_bundle(temp_db):
    with temp_db() as conn:
        with pytest.raises(ValueError):
            import_bundle(conn, b"this is not a zip file")


# --------------------------------------------------------------------------- #
# Evidence files, path-traversal defence, scores, case envelope, versioning   #
# --------------------------------------------------------------------------- #
def _add_evidence(conn, item_id, *, filename="shot.png", body=b"PNGDATA"):
    """Write a real evidence file under data/evidence and link a DB row."""
    ev_dir = get_settings().data_path / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / filename).write_bytes(body)
    conn.execute(
        "INSERT INTO evidence (item_id, screenshot, sha256, captured_at, ocr_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, f"evidence/{filename}", "deadbeef", "2026-01-01T00:00:00Z", "seen text"),
    )


def _make_raw_bundle(items, *, version=BUNDLE_VERSION, evidence_files=None, case=None):
    """Hand-build a bundle ZIP (so we can craft malicious / odd inputs)."""
    manifest = {
        "magic": BUNDLE_MAGIC,
        "version": version,
        "created_at": "2026-01-01T00:00:00Z",
        "scope": "all",
        "item_count": len(items),
    }
    payload = {"manifest": manifest, "items": items}
    if case is not None:
        payload["case"] = case
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("data.json", json.dumps(payload))
        for arc, data in (evidence_files or {}).items():
            zf.writestr(arc, data)
    return buf.getvalue()


def test_evidence_file_roundtrip(temp_db):
    with temp_db() as conn:
        iid = _seed_one(conn)
        _add_evidence(conn, iid, filename="shot.png", body=b"REALPNGBYTES")
        blob, summary = export_bundle(conn, scope="all")
        assert summary["evidence_count"] == 1

        # Simulate the offline machine: clear rows AND remove the on-disk file.
        _wipe_items(conn)
        (get_settings().data_path / "evidence" / "shot.png").unlink()

        result = import_bundle(conn, blob)
        assert result["evidence_added"] == 1
        # The screenshot file is rewritten and the DB row restored.
        written = get_settings().data_path / "evidence" / "shot.png"
        assert written.is_file()
        assert written.read_bytes() == b"REALPNGBYTES"
        row = conn.execute("SELECT screenshot, sha256 FROM evidence").fetchone()
        assert row["screenshot"] == "evidence/shot.png"


def test_import_writes_evidence_basename_only(temp_db):
    # A malicious bundle names its evidence with a traversal path. Import must
    # strip it to a basename and never write outside data/evidence.
    with temp_db() as conn:
        data_path = get_settings().data_path
        rec = {
            "content_hash": "h-evil",
            "source": "rss",
            "evidence": [
                {"filename": "../escape.png", "sha256": "x", "captured_at": "t"}
            ],
        }
        # The zip member is keyed on the basename, matching what import looks up.
        blob = _make_raw_bundle([rec], evidence_files={"evidence/escape.png": b"X"})
        import_bundle(conn, blob)

        # Written safely inside evidence/, NOT one level up at data_path/.
        assert (data_path / "evidence" / "escape.png").is_file()
        assert not (data_path / "escape.png").exists()
        row = conn.execute("SELECT screenshot FROM evidence").fetchone()
        assert row["screenshot"] == "evidence/escape.png"


def test_requirement_scores_roundtrip(temp_db):
    with temp_db() as conn:
        iid = _seed_one(conn)
        conn.execute(
            "INSERT INTO requirements (question, priority, enabled) VALUES (?, ?, 1)",
            ("Any movement near the border?", 1),
        )
        rid = conn.execute("SELECT id FROM requirements").fetchone()["id"]
        conn.execute(
            "INSERT INTO requirement_hits (item_id, requirement_id, score, rationale) "
            "VALUES (?, ?, ?, ?)",
            (iid, rid, 88, "Mentions troop movement."),
        )
        blob, _ = export_bundle(conn, scope="all")

        _wipe_items(conn)
        conn.execute("DELETE FROM requirement_hits")
        conn.execute("DELETE FROM requirements")

        result = import_bundle(conn, blob)
        assert result["scores_added"] == 1
        hit = conn.execute(
            "SELECT h.score, rq.question FROM requirement_hits h "
            "JOIN requirements rq ON rq.id = h.requirement_id"
        ).fetchone()
        assert hit["score"] == 88
        assert hit["question"] == "Any movement near the border?"


def test_case_envelope_roundtrip(temp_db):
    with temp_db() as conn:
        iid = _seed_one(conn)
        cur = conn.execute(
            "INSERT INTO cases (name, description) VALUES (?, ?)",
            ("Operation Lighthouse", "Tracking a developing situation."),
        )
        case_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO bookmarks (item_id, case_id) VALUES (?, ?)", (iid, case_id)
        )
        conn.execute(
            "INSERT INTO notes (case_id, body, created_at) VALUES (?, ?, ?)",
            (case_id, "Analyst observation worth keeping.", "2026-01-02T00:00:00Z"),
        )
        blob, summary = export_bundle(conn, scope="case", case_id=case_id)
        assert summary["has_case"] is True

        # Fresh machine: drop everything case-related plus the items.
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM bookmarks")
        conn.execute("DELETE FROM cases")
        _wipe_items(conn)

        import_bundle(conn, blob)
        crow = conn.execute(
            "SELECT id, description FROM cases WHERE name = ?", ("Operation Lighthouse",)
        ).fetchone()
        assert crow is not None
        assert crow["description"] == "Tracking a developing situation."
        note = conn.execute("SELECT body FROM notes WHERE case_id = ?", (crow["id"],)).fetchone()
        assert note["body"] == "Analyst observation worth keeping."
        # The exported item is re-linked to the rebuilt case.
        linked = conn.execute(
            "SELECT COUNT(*) AS c FROM bookmarks WHERE case_id = ?", (crow["id"],)
        ).fetchone()["c"]
        assert linked == 1


def test_import_rejects_newer_version(temp_db):
    with temp_db() as conn:
        blob = _make_raw_bundle([], version=BUNDLE_VERSION + 1)
        with pytest.raises(ValueError, match="newer version"):
            import_bundle(conn, blob)


def _rebuild_zip_replacing(blob, member, new_bytes):
    """Return a new bundle ZIP with one member's bytes swapped out."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            data = new_bytes if info.filename == member else src.read(info.filename)
            dst.writestr(info.filename, data)
    return out.getvalue()


def test_bundle_carries_a_data_checksum(temp_db):
    with temp_db() as conn:
        _seed_one(conn)
        blob, _ = export_bundle(conn, scope="all")

    import hashlib

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        data_raw = zf.read("data.json")
    assert "data_sha256" in manifest
    # The checksum really covers the exact data.json bytes.
    assert manifest["data_sha256"] == hashlib.sha256(data_raw).hexdigest()


def test_import_rejects_corrupted_payload(temp_db):
    # Tamper with data.json (still valid JSON + correct magic) but leave the
    # original manifest checksum in place: the mismatch must be caught.
    with temp_db() as conn:
        _seed_one(conn)
        blob, _ = export_bundle(conn, scope="all")

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            payload = json.loads(zf.read("data.json").decode("utf-8"))
        payload["items"][0]["title"] = "Tampered headline"  # changes the bytes
        corrupted = _rebuild_zip_replacing(
            blob, "data.json", json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

        with pytest.raises(ValueError, match="corrupted"):
            import_bundle(conn, corrupted)


def test_import_still_accepts_bundle_without_checksum(temp_db):
    # Backward compatibility: a bundle that predates the checksum (no
    # data_sha256 in its manifest) must still import cleanly.
    with temp_db() as conn:
        rec = {"content_hash": "h-old", "source": "rss", "title": "Legacy item"}
        blob = _make_raw_bundle([rec])  # _make_raw_bundle writes no data_sha256
        result = import_bundle(conn, blob)
        assert result["new_items"] == 1
