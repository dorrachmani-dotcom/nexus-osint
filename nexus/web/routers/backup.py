"""Settings > Backup & restore: one archive with everything Nexus stores."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.datastructures import UploadFile

from nexus import backup
from nexus.web.common import TEMPLATES

logger = logging.getLogger("nexus")
router = APIRouter()

_MAX_UPLOAD = 64 * 1024**3


def _panel(request: Request, **extra) -> HTMLResponse:
    from nexus.security.atrest import MIN_PASSPHRASE

    ctx = {"backups": backup.list_backups(), "created": None, "restored": None,
           "error": None, "min_passphrase": MIN_PASSPHRASE, **extra}
    return TEMPLATES.TemplateResponse(request, "_backup_settings.html", ctx)


@router.get("/settings/backup", response_class=HTMLResponse)
def backup_panel(request: Request) -> HTMLResponse:
    return _panel(request)


@router.post("/settings/backup", response_class=HTMLResponse)
async def backup_create(request: Request) -> HTMLResponse:
    form = await request.form()
    passphrase = str(form.get("passphrase") or "")
    if passphrase != str(form.get("confirm") or ""):
        return _panel(request, error="The two passphrases do not match.")
    try:
        result = await run_in_threadpool(backup.create_backup, passphrase)
    except backup.BackupError as exc:
        return _panel(request, error=str(exc))
    except OSError:
        logger.exception("Backup failed")
        return _panel(request, error="The backup could not be written (disk full or no permission?).")
    return _panel(request, created=result)


@router.get("/backups/{name}")
def backup_download(name: str) -> Response:
    path = backup.backup_path(name)
    if path is None:
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(
        path, media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(path.name)}"},
    )


async def _save_upload(upload: UploadFile) -> Path:
    fd, tmp = tempfile.mkstemp(dir=backup.backups_dir(), prefix=".incoming-", suffix=".part")
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD:
                    raise backup.BackupError("the uploaded backup is too large")
                fh.write(chunk)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


@router.post("/settings/backup/restore", response_class=HTMLResponse)
async def backup_restore(request: Request) -> HTMLResponse:
    form = await request.form()
    if not form.get("understand"):
        return _panel(request, error="Tick the box to confirm that restoring replaces the current data.")
    passphrase = str(form.get("passphrase") or "")
    upload = form.get("archive")
    temp: Path | None = None
    try:
        if isinstance(upload, UploadFile) and upload.filename:
            temp = await _save_upload(upload)
            source = temp
        else:
            chosen = backup.backup_path(str(form.get("existing") or ""))
            if chosen is None:
                return _panel(request, error="Choose a backup file or one of the saved backups.")
            source = chosen
        report = await run_in_threadpool(
            backup.restore_backup, source, passphrase,
            restore_settings=bool(form.get("restore_settings")),
        )
    except backup.BackupError as exc:
        return _panel(request, error=f"Nothing was changed: {exc}")
    except OSError:
        logger.exception("Restore failed")
        return _panel(request, error="The restore could not complete; see the log. "
                                     "The pre-restore backup (if made) is in the list below.")
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return _panel(request, restored=report)
