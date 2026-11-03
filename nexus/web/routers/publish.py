"""Edit -> publish: analyst edits of an item, write-ups composed from pinned
items, and the prepare -> preview -> confirm publish flow (Confluence,
Obsidian vault, knowledge pool)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from nexus.db import get_connection
from nexus.web.common import TEMPLATES

router = APIRouter()

_KINDS = {"item", "writeup"}
_TARGETS = {"confluence", "obsidian", "pool"}
_OPTION_FIELDS = ("source_id", "mode", "space", "page", "title", "vault_id")


def _bad(msg: str, status: int = 400) -> HTMLResponse:
    from markupsafe import escape

    return HTMLResponse(f'<p class="text-xs text-rose-400">{escape(msg)}</p>', status_code=status)


# --------------------------------------------------------------- item edit
@router.post("/items/{item_id}/edit", response_class=HTMLResponse)
def item_edit(request: Request, item_id: int, title: str = Form(default=""),
              summary: str = Form(default=""), note: str = Form(default="")) -> HTMLResponse:
    """Store the analyst's edit (the collected item itself never changes) and
    return the refreshed drawer."""
    from nexus.storage.publishing import save_item_edit
    from nexus.web.routers.items import item_detail

    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
            return _bad("Item not found.", 404)
        save_item_edit(conn, item_id, title=title, summary=summary, note=note)
    return item_detail(request, item_id)


# -------------------------------------------------------------- publish UI
@router.get("/publish/{kind}/{subject_id}/form", response_class=HTMLResponse)
def publish_form(request: Request, kind: str, subject_id: int,
                 target: str = "confluence") -> HTMLResponse:
    from nexus.publish import publish_targets
    from nexus.storage.publishing import list_publications

    if kind not in _KINDS or target not in _TARGETS:
        return _bad("Unknown publish target.")
    with get_connection() as conn:
        targets = publish_targets(conn)
        pubs = list_publications(conn, kind, subject_id)
    return TEMPLATES.TemplateResponse(request, "_publish_form.html", {
        "kind": kind, "subject_id": subject_id, "target": target, "t": targets,
        "pubs": pubs,
    })


@router.get("/publish/confluence/pages", response_class=HTMLResponse)
async def publish_page_search(request: Request, source_id: int = 0, q: str = "",
                              space: str = "") -> HTMLResponse:
    """Find pages (to append to, or to use as the parent of a new page)."""
    from nexus.publish.confluence import load_publisher
    from nexus.sources.confluence import ConfluenceError

    with get_connection() as conn:
        pub = load_publisher(conn, source_id) if source_id else None
    if pub is None:
        return _bad("Choose a Confluence connection first.")
    ok, why = pub.write_allowed()
    if not ok:
        return _bad(why)
    try:
        pages = await run_in_threadpool(pub.search_pages, q, space or None)
    except ConfluenceError as exc:
        return _bad(f"Confluence: {exc}")
    return TEMPLATES.TemplateResponse(request, "_publish_pages.html", {"pages": pages})


@router.post("/publish/{kind}/{subject_id}/preview", response_class=HTMLResponse)
async def publish_preview(request: Request, kind: str, subject_id: int) -> HTMLResponse:
    """Render exactly what would be sent and park it behind a one-time token."""
    from nexus.publish.service import PublishError, prepare

    if kind not in _KINDS:
        return _bad("Unknown subject.")
    form = await request.form()
    target = str(form.get("target") or "")
    opts = {f: str(form.get(f) or "") for f in _OPTION_FIELDS}
    # A chosen page arrives as "id|title": the page to append to, or the parent.
    page_id, _sep, page_title = opts.pop("page", "").partition("|")
    if opts.get("mode") == "append":
        opts["page_id"], opts["page_title"] = page_id, page_title
    elif page_id:
        opts["parent_id"] = page_id
    try:
        with get_connection() as conn:
            p = prepare(conn, kind, subject_id, target, opts, origin="ui")
    except PublishError as exc:
        return _bad(str(exc))
    return TEMPLATES.TemplateResponse(request, "_publish_preview.html", {"p": p})


@router.post("/publish/confirm", response_class=HTMLResponse)
async def publish_confirm(request: Request, token: str = Form(default="")) -> HTMLResponse:
    from nexus.publish.service import confirm, peek
    from nexus.storage.publishing import list_publications

    pending = peek(token)

    def run() -> tuple[dict, list]:
        with get_connection() as conn:
            res = confirm(conn, token)
            pubs = (list_publications(conn, pending.subject_kind, pending.subject_id)
                    if pending else [])
        return res, pubs

    result, pubs = await run_in_threadpool(run)
    return TEMPLATES.TemplateResponse(request, "_publish_result.html",
                                      {"r": result, "pubs": pubs})


@router.post("/publish/confirm.json")
async def publish_confirm_json(token: str = Body(default="", embed=True)) -> JSONResponse:
    """The confirmation card in the Sherlock chat (a click, never the model)."""
    from nexus.publish.service import confirm

    def run() -> dict:
        with get_connection() as conn:
            return confirm(conn, token)

    result = await run_in_threadpool(run)
    url = result.get("url") or ""
    if not (url.startswith("/") or url.lower().startswith(("http://", "https://"))):
        result["url"] = ""
    return JSONResponse(result)


# ---------------------------------------------------------------- write-ups
@router.get("/cases/{case_id}/writeups", response_class=HTMLResponse)
def case_writeups(request: Request, case_id: int) -> HTMLResponse:
    from nexus.storage import case_items, get_case
    from nexus.storage.publishing import edited_item_ids, list_writeups

    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        items = case_items(conn, case_id)
        edited = edited_item_ids(conn, [int(i["id"]) for i in items])
        writeups = list_writeups(conn, case_id)
    return TEMPLATES.TemplateResponse(request, "writeups.html", {
        "case": case, "items": items, "edited": edited, "writeups": writeups,
    })


@router.post("/cases/{case_id}/writeups")
async def case_writeup_create(request: Request, case_id: int):
    from nexus.publish.content import compose_writeup_text
    from nexus.storage import get_case
    from nexus.storage.publishing import create_writeup

    form = await request.form()
    ids = []
    for v in form.getlist("item"):
        if str(v).isdigit():
            ids.append(int(str(v)))
    title = str(form.get("title") or "").strip()
    with get_connection() as conn:
        case = get_case(conn, case_id)
        if case is None:
            return HTMLResponse("Case not found", status_code=404)
        pinned = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM bookmarks WHERE case_id = ?", (case_id,)).fetchall()}
        ids = [i for i in ids if i in pinned]
        if not ids:
            return RedirectResponse(url=f"/cases/{case_id}/writeups?error=1", status_code=303)
        wid = create_writeup(conn, case_id=case_id,
                             title=title or f"{case['name']}: write-up",
                             body=compose_writeup_text(conn, ids), item_ids=ids)
    return RedirectResponse(url=f"/writeups/{wid}", status_code=303)


@router.get("/writeups/{writeup_id}", response_class=HTMLResponse)
def writeup_page(request: Request, writeup_id: int, saved: int = 0) -> HTMLResponse:
    from nexus.publish import publish_targets
    from nexus.publish.content import writeup_doc
    from nexus.storage.publishing import get_writeup, list_publications

    with get_connection() as conn:
        w = get_writeup(conn, writeup_id)
        if w is None:
            return HTMLResponse("Write-up not found", status_code=404)
        doc = writeup_doc(conn, writeup_id)
        pubs = list_publications(conn, "writeup", writeup_id)
        targets = publish_targets(conn)
    return TEMPLATES.TemplateResponse(request, "writeup.html", {
        "w": w, "doc": doc, "pubs": pubs, "t": targets, "saved": bool(saved),
    })


@router.post("/writeups/{writeup_id}")
def writeup_save(writeup_id: int, title: str = Form(default=""),
                 body: str = Form(default="")):
    from nexus.storage.publishing import get_writeup, update_writeup

    with get_connection() as conn:
        if get_writeup(conn, writeup_id) is None:
            return HTMLResponse("Write-up not found", status_code=404)
        update_writeup(conn, writeup_id, title=title, body=body)
    return RedirectResponse(url=f"/writeups/{writeup_id}?saved=1", status_code=303)


@router.get("/writeups/{writeup_id}/delete", response_class=HTMLResponse)
def writeup_delete_confirm(request: Request, writeup_id: int) -> HTMLResponse:
    """The delete dialog: where this write-up was published, with what to remove."""
    from nexus.publish.retract import plan
    from nexus.storage.publishing import get_writeup

    with get_connection() as conn:
        w = get_writeup(conn, writeup_id)
        if w is None:
            return HTMLResponse("Write-up not found", status_code=404)
        rows = plan(conn, "writeup", writeup_id)
    return TEMPLATES.TemplateResponse(request, "writeup_delete.html", {"w": w, "rows": rows})


@router.post("/writeups/{writeup_id}/delete", response_class=HTMLResponse)
async def writeup_delete(request: Request, writeup_id: int):
    """Remove the ticked published copies, then the write-up; show what happened."""
    from nexus.publish.retract import run
    from nexus.storage.publishing import delete_writeup, get_writeup

    form = await request.form()
    selected = {int(str(v)) for v in form.getlist("remove") if str(v).isdigit()}

    def work() -> tuple[dict | None, list[dict]]:
        with get_connection() as conn:
            w = get_writeup(conn, writeup_id)
            if w is None:
                return None, []
            results = run(conn, "writeup", writeup_id, selected)
            delete_writeup(conn, writeup_id)
        return w, results

    w, results = await run_in_threadpool(work)
    if w is None:
        return RedirectResponse(url="/cases", status_code=303)
    back = f"/cases/{w['case_id']}/writeups" if w.get("case_id") else "/cases"
    if not results:
        return RedirectResponse(url=back, status_code=303)
    return TEMPLATES.TemplateResponse(request, "writeup_deleted.html",
                                      {"w": w, "results": results, "back": back})
