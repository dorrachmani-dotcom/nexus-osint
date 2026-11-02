"""Uploaded documents end to end: storage, dedup, case/pool attachment, the
"no AI" option, deletion, the web endpoints and Sherlock grounding."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from nexus import storage as s
from nexus.ingest.upload import ingest_upload, split_text, uploads_dir
from nexus.storage.documents import (
    ai_excluded_ids,
    document_items,
    get_document,
    list_documents,
    search_document_chunks,
)


def _client() -> TestClient:
    from nexus.web.app import app

    return TestClient(app, base_url="http://127.0.0.1")


def _text_file(n_sections: int = 2) -> bytes:
    parts = []
    for i in range(n_sections):
        parts.append(f"# Section {i}\n" + f"Warehouse audit finding number {i}. " * 200)
    return "\n".join(parts).encode()


def test_split_text_respects_limit():
    chunks = split_text("para one.\n\n" + "x " * 3000, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0].startswith("para one.")
    assert sum(c.count("x") for c in chunks) == 3000
    # Prefers a paragraph break when one falls in the second half of the window.
    chunks = split_text("a" * 600 + "\n\n" + "b" * 600, 1000)
    assert chunks == ["a" * 600, "b" * 600]


def test_ingest_into_case_pins_items_and_dedups(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Supplier review")
    res = ingest_upload(_text_file(), "audit.md", case_id=cid)
    assert res["ok"] and res["chunks"] >= 2 and res["new"] == res["chunks"]
    with temp_db() as conn:
        doc = get_document(conn, res["document_id"])
        assert doc["case_id"] == cid and not doc["in_pool"]
        rows = document_items(conn, res["document_id"])
        assert rows[0]["title"].startswith("audit.md — Section 0")
        pinned = {r["id"] for r in s.case_items(conn, cid)}
        assert {r["id"] for r in rows} <= pinned
        assert all(r["source"] == "upload" for r in s.case_items(conn, cid))
    # Original kept locally, named by hash, never by the user's filename.
    stored = uploads_dir() / f"{res['sha256']}.bin"
    assert stored.is_file()
    # Same file again: nothing new.
    again = ingest_upload(_text_file(), "audit.md", case_id=cid)
    assert again["ok"] and again["new"] == 0 and again["chunks"] == res["chunks"]


def test_pool_upload_and_sherlock_search(temp_db):
    res = ingest_upload(b"The cold-storage facility inventory policy says rotate stock weekly.",
                        "policy.txt", in_pool=True)
    assert res["ok"]
    with temp_db() as conn:
        assert list_documents(conn, pool_only=True)[0]["filename"] == "policy.txt"
        hits = search_document_chunks(conn, ["inventory"])
        assert hits and hits[0]["filename"] == "policy.txt"


def test_upload_defaults_to_pool_when_no_destination(temp_db):
    res = ingest_upload(b"plain text body", "a.txt")
    with temp_db() as conn:
        assert get_document(conn, res["document_id"])["in_pool"] == 1


def test_no_ai_items_skip_enrichment_queues(temp_db):
    res = ingest_upload(b"Confidential partner contract terms.", "contract.txt",
                        in_pool=True, no_ai=True)
    ok = ingest_upload(b"Public brochure text for the fair.", "brochure.txt", in_pool=True)
    with temp_db() as conn:
        pending = {r["id"] for r in s.pending_analysis(conn)}
        secret_ids = {r["id"] for r in document_items(conn, res["document_id"])}
        public_ids = {r["id"] for r in document_items(conn, ok["document_id"])}
        assert secret_ids and not (secret_ids & pending)
        assert public_ids <= pending
        assert not (secret_ids & {r["id"] for r in s.pending_keyless_translation(conn)})
        assert ai_excluded_ids(conn, list(secret_ids | public_ids)) == secret_ids


def test_delete_removes_items_and_original(temp_db):
    res = ingest_upload(_text_file(1), "gone.md", in_pool=True)
    from nexus.ingest.upload import remove_original
    from nexus.storage.documents import delete_document

    with temp_db() as conn:
        ids = [r["id"] for r in document_items(conn, res["document_id"])]
        orphan = delete_document(conn, res["document_id"])
        assert orphan == f"uploads/{res['sha256']}.bin"
        remaining = {r["id"] for r in conn.execute("SELECT id FROM items").fetchall()}
        assert not set(ids) & remaining
    remove_original(orphan)
    assert not (uploads_dir() / f"{res['sha256']}.bin").exists()


def test_remove_original_refuses_outside_uploads(tmp_path):
    victim = uploads_dir().parent / "keep.txt"
    victim.write_text("x")
    from nexus.ingest.upload import remove_original

    remove_original("keep.txt")
    remove_original("uploads/../keep.txt")
    assert victim.exists()


def test_refused_zip_bomb_stores_nothing(temp_db):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("z.txt", b"\x00" * (20 * 1024 * 1024))
    res = ingest_upload(buf.getvalue(), "bomb.zip", in_pool=True)
    assert not res["ok"] and "safety" in res["error"]
    with temp_db() as conn:
        assert list_documents(conn) == []


def test_storage_failure_is_reported_not_raised(temp_db, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("nexus.ingest.upload._persist", boom)
    res = ingest_upload(b"some text", "x.txt", in_pool=True)
    assert not res["ok"] and "could not store" in res["error"]
    assert not (uploads_dir() / f"{res.get('sha256', 'none')}.bin").exists()
    assert not list(uploads_dir().glob("*.bin"))


def test_no_ai_uploads_never_reach_digest_or_case_brief(temp_db, monkeypatch):
    from nexus.digest import collect_digest_data

    with temp_db() as conn:
        cid = s.create_case(conn, "Ferry")
        s.add_case_term(conn, cid, "ferry")
    res = ingest_upload(b"ferry timetable secret annex", "annex.txt", case_id=cid, no_ai=True)
    with temp_db() as conn:
        data = collect_digest_data(conn, "2000-01-01 00:00:00")
        flat = str(data)
        assert "secret annex" not in flat
        ids = {r["id"] for r in document_items(conn, res["document_id"])}
        assert ids

    seen: dict = {}

    def fake_brief(case, items, settings):
        seen["items"] = items
        return "ok"

    monkeypatch.setattr("nexus.web.routers.cases._generate_case_briefing", fake_brief)
    monkeypatch.setattr("nexus.ingest.upload.cloud_ai_active", lambda settings=None: True)
    monkeypatch.setattr("nexus.config.Settings.analysis_enabled", property(lambda self: True))
    _client().post(f"/cases/{cid}/brief")
    assert not ({i["id"] for i in seen.get("items", [])} & ids)


# ------------------------------------------------------------------- web
def test_upload_endpoint_multi_file_into_case(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Port activity")
    files = [
        ("files", ("one.txt", b"Harbor crane maintenance schedule", "text/plain")),
        ("files", ("two.csv", b"ship,berth\nAurora,4\n", "text/csv")),
    ]
    r = _client().post("/documents/upload", data={"destination": str(cid), "allow_ai": "1"},
                       files=files)
    assert r.status_code == 200
    assert "one.txt" in r.text and "two.csv" in r.text
    assert r.headers.get("HX-Trigger") == "documents-changed"
    with temp_db() as conn:
        docs = list_documents(conn, case_id=cid)
        assert {d["filename"] for d in docs} == {"one.txt", "two.csv"}
        assert all(not d["no_ai"] for d in docs)


def test_upload_without_allow_ai_is_no_ai(temp_db):
    r = _client().post("/documents/upload", data={"destination": "pool"},
                       files=[("files", ("x.txt", b"some words here", "text/plain"))])
    assert r.status_code == 200
    with temp_db() as conn:
        assert list_documents(conn)[0]["no_ai"] == 1


def test_cross_origin_multipart_upload_rejected(temp_db):
    r = _client().post(
        "/documents/upload", data={"destination": "pool"},
        files=[("files", ("x.txt", b"evil", "text/plain"))],
        headers={"Origin": "https://attacker.example"},
    )
    assert r.status_code == 403


def test_pages_render(temp_db):
    with temp_db() as conn:
        cid = s.create_case(conn, "Docs case")
    c = _client()
    page = c.get("/sources/documents")
    assert page.status_code == 200 and "Add documents" in page.text
    tab = c.get(f"/cases/{cid}/documents")
    assert tab.status_code == 200 and "Documents in this case" in tab.text
    assert c.get(f"/cases/{cid}?tab=documents").status_code == 200


def test_original_download_is_attachment_and_data_mount_blocked(temp_db):
    res = ingest_upload(b"<html><script>alert(1)</script>hi</html>", "x.html", in_pool=True)
    c = _client()
    r = c.get(f"/documents/{res['document_id']}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["content-disposition"].startswith("attachment")
    assert c.get(f"/data/uploads/{res['sha256']}.bin").status_code == 404


def test_delete_endpoint(temp_db):
    res = ingest_upload(b"temporary text", "t.txt", in_pool=True)
    r = _client().post(f"/documents/{res['document_id']}/delete")
    assert r.status_code == 200
    with temp_db() as conn:
        assert get_document(conn, res["document_id"]) is None


# --------------------------------------------------------------- Sherlock
def test_sherlock_context_includes_pool_documents(temp_db, monkeypatch):
    from nexus import assistant as A

    ingest_upload(b"Evacuation procedure: assemble at gate seven.", "procedure.txt",
                  in_pool=True, no_ai=False)
    monkeypatch.setattr(A, "_cloud_ai", lambda: True)
    with temp_db() as conn:
        text, items = A._data_context(conn, "what is the evacuation procedure")
    assert "procedure.txt" in text and "gate seven" in text
    assert any(i["source"] == "upload" for i in items)


def test_sherlock_hides_no_ai_documents_from_cloud(temp_db, monkeypatch):
    from nexus import assistant as A

    ingest_upload(b"Secret evacuation code word is lantern.", "secret.txt",
                  in_pool=True, no_ai=True)
    with temp_db() as conn:
        monkeypatch.setattr(A, "_cloud_ai", lambda: True)
        text, _ = A._data_context(conn, "evacuation code word")
        assert "lantern" not in text
        monkeypatch.setattr(A, "_cloud_ai", lambda: False)
        text, _ = A._data_context(conn, "evacuation code word")
        assert "lantern" in text
