"""Obsidian: import vault — parsing, a generated temp vault, incremental
re-scans, filters, symlink escape, zipped-vault upload, and the UI routes.

Every vault is built in a tmp dir; names are fictional. Non-ASCII content is
built with chr() so no such text lives in this file.
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from nexus.ingest.obsidian_notes import parse_frontmatter, parse_note, split_sections
from nexus.sources import obsidian as O

NOTE_A = """---
title: Alpha Project
tags: [osint, "people/analyst"]
author: Robin Example
date: 2026-10-01
---
# Alpha

Works with [[Bravo]] and [[Charlie|the C team]] on #fieldwork.
Embedded report: ![[report.pdf]] and image ![[diagram.png]].
A note embed: ![[Bravo]].

```python
# not a #tag inside code, nor [[NotALink]]
```
Issue #123 is not a tag. `#inline-code` neither.
"""

NOTE_B = """# Bravo

Plain note with #osint tag. Back to [[Alpha]].
"""


def _pdf_bytes(text: str) -> bytes:
    pytest.importorskip("pypdf")
    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = canvas_mod.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------- parsing
def test_frontmatter_uses_safe_load_and_ignores_bad_yaml():
    fm, body = parse_frontmatter("---\nkey: !!python/object/apply:os.system [echo]\n---\nbody")
    assert fm == {} and body == "body"
    fm, body = parse_frontmatter("---\ntags: a, b\ncount: 3\n---\ntext")
    assert fm == {"tags": "a, b", "count": 3} and body == "text"
    assert parse_frontmatter("no frontmatter")[0] == {}


def test_parse_note_tags_links_embeds():
    note = parse_note(NOTE_A, "alpha")
    assert note.title == "Alpha Project"
    assert note.frontmatter["author"] == "Robin Example"
    assert note.frontmatter["date"] == "2026-10-01"
    assert note.tags == ["osint", "people/analyst", "fieldwork"]
    assert note.links == ["Bravo", "Charlie"]
    assert note.embeds == ["report.pdf", "diagram.png", "Bravo"]
    assert "NotALink" not in note.links and "inline-code" not in note.tags
    assert "the C team" in note.text and "[[" not in note.text


def test_long_note_splits_at_headings_outside_code():
    body = "intro\n\n" + "\n\n".join(
        f"## Part {i}\n" + ("words " * 400) for i in range(4)) + "\n```\n## not a heading\n```\n"
    sections = split_sections("t", body, max_chars=3000)
    labels = [s[0] for s in sections]
    assert labels == ["", "Part 0", "Part 1", "Part 2", "Part 3"]
    assert "## not a heading" in sections[-1][1]


# ------------------------------------------------------------ the vault
@pytest.fixture
def vault(tmp_path) -> Path:
    root = tmp_path / "Research vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (root / ".trash").mkdir()
    (root / ".trash" / "old.md").write_text("deleted note", encoding="utf-8")
    (root / "Projects").mkdir()
    (root / "Templates").mkdir()
    (root / "assets").mkdir()
    (root / "Projects" / "Alpha.md").write_text(NOTE_A, encoding="utf-8")
    (root / "Bravo.md").write_text(NOTE_B, encoding="utf-8")
    (root / "Templates" / "Daily.md").write_text("# Template {{date}}", encoding="utf-8")
    (root / "assets" / "report.pdf").write_bytes(_pdf_bytes("Quarterly findings summary"))
    intl = "".join(chr(c) for c in (0x5E9, 0x5DC, 0x5D5, 0x5DD))
    (root / "Intl.md").write_text(f"# Intl\n\n{intl} #global\n", encoding="utf-8")
    return root


def _add(temp_db, path, **cfg):
    from nexus.storage.obsidian_vaults import create_obsidian_vault, get_obsidian_vault

    with temp_db() as conn:
        vid = create_obsidian_vault(conn, {"path": str(path), "name": "Research", **cfg})
        return O.ObsidianSource(get_obsidian_vault(conn, vid))


def _reload(temp_db, src):
    from nexus.storage.obsidian_vaults import get_obsidian_vault

    with temp_db() as conn:
        return O.ObsidianSource(get_obsidian_vault(conn, src.id))


def _docs(temp_db):
    with temp_db() as conn:
        return {r["filename"]: dict(r) for r in conn.execute(
            "SELECT * FROM documents WHERE kind = 'obsidian'")}


def _items(temp_db):
    with temp_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT title, content, raw, author FROM items WHERE source = 'Research' ORDER BY id")]


def _touch(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def test_import_vault_notes_metadata_and_embeds(temp_db, vault):
    src = _add(temp_db, vault)
    assert src.is_available()
    res = src.ingest()
    assert "4 note(s) checked, 4 new" in res["status"]
    docs = _docs(temp_db)
    assert set(docs) == {"Alpha Project", "Bravo", "Daily", "Intl"}  # no .trash / .obsidian
    assert all(d["in_pool"] == 1 and d["stored_path"] == "" for d in docs.values())
    items = _items(temp_db)
    alpha = [i for i in items if i["title"].startswith("Alpha Project")]
    raw = json.loads(alpha[0]["raw"])
    assert raw["tags"] == ["osint", "people/analyst", "fieldwork"]
    assert raw["links"] == ["Bravo", "Charlie"] and raw["path"] == "Projects/Alpha.md"
    assert raw["folder"] == "Projects" and alpha[0]["author"] == "Robin Example"
    text = " ".join(i["content"] for i in alpha)
    assert "Quarterly findings summary" in text  # embedded PDF read by the upload parser
    assert "Linked notes: Bravo, Charlie" in text
    assert any(chr(0x5E9) in i["content"] for i in items)


def test_incremental_unchanged_changed_deleted(temp_db, vault):
    src = _add(temp_db, vault)
    src.ingest()
    before = len(_items(temp_db))
    res = _reload(temp_db, src).ingest()
    assert res["new"] == 0 and "0 new, 0 updated" in res["status"]
    assert len(_items(temp_db)) == before

    _touch(vault / "Bravo.md", NOTE_B + "\nNew line about [[Delta]].\n")
    res = _reload(temp_db, src).ingest()
    assert res["changed"] == 1
    updated = [i for i in _items(temp_db) if i["title"] == "Bravo — updated"]
    assert len(updated) == 1 and "+ New line about Delta." in updated[0]["content"]

    # Touched but identical content: no new items.
    _touch(vault / "Intl.md", (vault / "Intl.md").read_text(encoding="utf-8"))
    assert _reload(temp_db, src).ingest()["new"] == 0

    (vault / "Templates" / "Daily.md").unlink()
    res = _reload(temp_db, src).ingest()
    assert res["removed"] == 1 and "Daily" not in _docs(temp_db)
    assert not [i for i in _items(temp_db) if i["title"].startswith("Daily")]


def test_filters_folders_and_required_tag(temp_db, vault):
    src = _add(temp_db, vault, exclude_folders="Templates", required_tag="#osint")
    src.ingest()
    assert set(_docs(temp_db)) == {"Alpha Project", "Bravo"}
    src2 = _add(temp_db, vault, name="Projects only", include_folders="projects")
    src2.ingest()
    with temp_db() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT filename FROM documents WHERE origin = 'Projects only'")]
    assert names == ["Alpha Project"]


def test_symlink_escape_is_blocked(temp_db, vault, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "private.md").write_text("TOP SECRET outside the vault", encoding="utf-8")
    try:
        os.symlink(outside / "private.md", vault / "linked.md")
        os.symlink(outside, vault / "linked_dir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this system")
    src = _add(temp_db, vault)
    res = src.ingest()
    assert "outside the vault were skipped" in res["status"]
    assert not any("TOP SECRET" in i["content"] for i in _items(temp_db))


def test_check_vault_validation(tmp_path):
    assert O.check_vault("")[0] is None
    assert "does not exist" in O.check_vault(str(tmp_path / "nope"))[1]
    f = tmp_path / "file.md"
    f.write_text("x", encoding="utf-8")
    assert "not a folder" in O.check_vault(str(f))[1]
    assert O.check_vault(str(tmp_path))[0] == tmp_path.resolve()


def test_size_limit_skips_huge_note(temp_db, vault, monkeypatch):
    monkeypatch.setattr(O, "MAX_NOTE_BYTES", 50)
    res = _add(temp_db, vault).ingest()
    assert "skipped (too large or unreadable)" in res["status"]


def test_case_target_and_no_ai(temp_db, vault):
    from nexus.storage import create_case

    with temp_db() as conn:
        case_id = create_case(conn, "Field study")
    _add(temp_db, vault, case_id=str(case_id), no_ai="1").ingest()
    docs = _docs(temp_db)
    assert all(d["case_id"] == case_id and d["no_ai"] == 1 and d["in_pool"] == 0
               for d in docs.values())
    with temp_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bookmarks WHERE case_id = ?",
                            (case_id,)).fetchone()[0] >= 4


def test_collector_runs_vault(temp_db, vault):
    from nexus.collector import Collector

    _add(temp_db, vault)
    stats = Collector().scan()
    assert stats["Research"]["new"] >= 4


# --------------------------------------------------------- zipped vault
def test_zipped_vault_upload_uses_note_parser(temp_db, vault):
    from nexus.ingest.upload import ingest_upload

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in vault.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(vault.parent).as_posix())
    res = ingest_upload(buf.getvalue(), "vault.zip", in_pool=True)
    assert res["ok"], res
    assert any("Obsidian vault" in n for n in res["notes"])
    with temp_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT title, content, raw FROM items WHERE source = 'upload'")]
    titles = " | ".join(r["title"] for r in rows)
    assert "Alpha Project" in titles and "old.md" not in titles and "app.json" not in titles
    alpha = next(r for r in rows if "Alpha Project" in r["title"])
    assert json.loads(alpha["raw"])["tags"] == ["osint", "people/analyst", "fieldwork"]
    assert "---" not in alpha["content"].splitlines()[0]  # frontmatter stripped
    assert "Quarterly findings summary" in " ".join(r["content"] for r in rows)


# ------------------------------------------------------------------- UI
def test_ui_add_test_scan_delete(temp_db, vault, tmp_path):
    from fastapi.testclient import TestClient

    from nexus.web.app import app

    c = TestClient(app, base_url="http://127.0.0.1")
    page = c.get("/sources/obsidian")
    assert page.status_code == 200 and "Obsidian: import vault" in page.text
    bad = c.post("/sources/obsidian", data={"path": str(tmp_path / "missing")})
    assert "Vault not added" in bad.text
    r = c.post("/sources/obsidian", data={"path": str(vault), "destination": "pool",
                                          "allow_ai": "1"})
    assert "Research vault" in r.text
    with temp_db() as conn:
        vid = conn.execute("SELECT id FROM obsidian_vaults").fetchone()[0]
    t = c.post(f"/sources/obsidian/{vid}/test")
    assert "Readable: 4 notes" in t.text
    s = c.post(f"/sources/obsidian/{vid}/scan")
    assert "Scanned" in s.text and "4 new" in s.text
    c.post(f"/sources/obsidian/{vid}/delete")
    with temp_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM obsidian_vaults").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 4  # kept
    assert (vault / "Bravo.md").exists()  # the vault itself is never touched


def test_case_export_naming(temp_db):
    from fastapi.testclient import TestClient

    from nexus.storage import create_case
    from nexus.web.app import app

    with temp_db() as conn:
        cid = create_case(conn, "Naming check")
    page = TestClient(app, base_url="http://127.0.0.1").get(f"/cases/{cid}")
    assert "Obsidian: export case (.zip)" in page.text
