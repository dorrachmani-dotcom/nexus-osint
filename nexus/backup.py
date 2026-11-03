"""One-file backup and restore of everything Nexus stores locally.

A backup is a ZIP with a ``manifest.json`` (format, app version, SHA-256 and
size of every member), the database (a consistent copy made with SQLite's
online backup API), uploaded originals, evidence, saved reports, the
encryption keystore (if encryption at rest is on; it holds only *wrapped*
keys) and ``settings.env``: the non-secret part of ``.env``. API keys,
tokens and passwords are never written to a backup.

Optionally the whole archive is encrypted with a passphrase (``NXB1``:
scrypt salt + parameters, then the chunked AES-256-GCM format of
:mod:`nexus.security.atrest`), so a copy on a USB stick or a file share is
useless without it.

Restore never trusts the archive: it is decrypted to a temp file, every name
is checked against the expected layout (no absolute paths, ``..``, links or
unknown folders: zip-slip safe), sizes are capped, every member's hash must
match the manifest, the backup must not come from a newer Nexus, and the
database must pass ``PRAGMA integrity_check``. Only then is a pre-restore
backup of the current state taken, and the data is swapped in: the database
through the backup API (safe while the app runs), files atomically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from nexus import __version__
from nexus.config import get_settings

logger = logging.getLogger("nexus.backup")

FORMAT = "nexus-backup"
FORMAT_VERSION = 1
NXB_MAGIC = b"NXB1"
_FILE_DIRS = ("uploads", "evidence", "reports")
_KEYSTORE_FILES = ("keystore.json", "keystore.json.bak")
_MAX_MEMBERS = 200_000
_MAX_TOTAL = 64 * 1024**3        # 64 GiB uncompressed
_MAX_RATIO = 200                  # per-member compression ratio (zip bomb)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,200}$")

# Never copied into, or restored from, a backup's settings.env.
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PASS|SESSION|COOKIE|"
                          r"HEADER|AUTH|CREDENTIAL|SESSIONID)", re.I)
_NOT_RESTORED = {"DATA_DIR", "DATABASE_PATH", "NEXUS_ENV_FILE", "NEXUS_HOME"}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,80}$")


class BackupError(Exception):
    """A backup could not be created or does not pass validation."""


def backups_dir() -> Path:
    path = get_settings().data_path / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_secret_name(name: str) -> bool:
    from nexus.envstore import is_per_source_secret

    return bool(_SECRET_NAME.search(name)) or is_per_source_secret(name)


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key, raw = key.strip(), raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        out[key] = raw
    return out


def safe_settings_text() -> str:
    """The non-secret lines of ``.env`` (secrets dropped by name)."""
    from nexus.envstore import _env_path

    try:
        text = _env_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    kept = [f"{k}={v}" for k, v in _parse_env(text).items()
            if _ENV_NAME.match(k) and not _is_secret_name(k) and k not in _NOT_RESTORED]
    return "# Non-secret Nexus settings (API keys and passwords are never backed up)\n" + \
        "\n".join(kept) + "\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db_snapshot(target: Path) -> None:
    """A consistent copy of the live database via SQLite's online backup API."""
    src = sqlite3.connect(get_settings().db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


@dataclass
class BackupResult:
    path: Path
    encrypted: bool
    files: int
    size: int


def create_backup(passphrase: str = "", *, label: str = "backup") -> BackupResult:
    """Write a backup archive into ``data/backups`` and return its path."""
    from nexus.security.atrest import MIN_PASSPHRASE, crypto_available

    if passphrase and len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"use a passphrase of at least {MIN_PASSPHRASE} characters")
    if passphrase and not crypto_available():
        raise BackupError("encrypted backups need the 'cryptography' package")
    data = get_settings().data_path
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^a-z-]", "", label.lower()) or "backup"
    out_dir = backups_dir()
    final = out_dir / f"nexus-{label}-{stamp}.{'nxb' if passphrase else 'zip'}"
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".work-") as work:
        work_dir = Path(work)
        db_copy = work_dir / "nexus.db"
        _db_snapshot(db_copy)
        members: list[tuple[str, Path]] = [("nexus.db", db_copy)]
        for folder in _FILE_DIRS:
            base = data / folder
            if not base.is_dir():
                continue
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    path = Path(root) / name
                    if name.startswith(".") or path.is_symlink():
                        continue
                    rel = path.relative_to(data).as_posix()
                    members.append((rel, path))
        for name in _KEYSTORE_FILES:
            if (data / name).is_file():
                members.append((name, data / name))
        settings_file = work_dir / "settings.env"
        settings_file.write_text(safe_settings_text(), encoding="utf-8")
        members.append(("settings.env", settings_file))

        manifest: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app_version": __version__,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "files": {rel: {"sha256": _sha256_file(p), "size": p.stat().st_size}
                      for rel, p in members},
        }
        zip_path = work_dir / "archive.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for rel, path in members:
                zf.write(path, rel)
        tmp_final = work_dir / "final.part"
        if passphrase:
            with open(zip_path, "rb") as src, open(tmp_final, "wb") as dst:
                _encrypt_archive(src, dst, passphrase)
        else:
            shutil.copyfile(zip_path, tmp_final)
        os.replace(tmp_final, final)
    logger.info("Backup written: %s (%d files)", final.name, len(members))
    return BackupResult(final, bool(passphrase), len(members), final.stat().st_size)


# ------------------------------------------------------------ encrypted form
def _encrypt_archive(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    from nexus.security import atrest

    salt = secrets.token_bytes(16)
    params = dict(atrest._SCRYPT)
    header = json.dumps({"kdf": "scrypt", **params, "salt": salt.hex()}).encode("ascii")
    dst.write(NXB_MAGIC + len(header).to_bytes(2, "big") + header)
    atrest.encrypt_stream(src, dst, master=atrest.passphrase_key(passphrase, salt, params))


def _decrypt_archive(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    from nexus.security import atrest

    if src.read(4) != NXB_MAGIC:
        raise BackupError("not an encrypted Nexus backup")
    try:
        header = json.loads(src.read(int.from_bytes(src.read(2), "big")))
        params = {k: int(header[k]) for k in ("n", "r", "p")}
        salt = bytes.fromhex(header["salt"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError("the backup header is damaged") from exc
    if not (2**10 <= params["n"] <= 2**20 and params["r"] <= 32 and params["p"] <= 16):
        raise BackupError("the backup uses unsupported key-derivation settings")
    try:
        atrest.decrypt_stream(src, dst, master=atrest.passphrase_key(passphrase, salt, params))
    except atrest.EncryptionError as exc:
        raise BackupError("wrong passphrase, or the backup file is damaged") from exc


def is_encrypted_backup(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == NXB_MAGIC
    except OSError:
        return False


# ------------------------------------------------------------------- restore
@dataclass
class RestoreReport:
    ok: bool
    message: str
    restored_files: int = 0
    pre_restore_backup: str | None = None
    warnings: list[str] = field(default_factory=list)


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v or "")[:3]
    return tuple(int(p) for p in parts) or (0,)


def _check_name(name: str) -> str:
    """Validate one archive member name; return its kind or raise."""
    if name in ("manifest.json", "nexus.db", "settings.env", *_KEYSTORE_FILES):
        return name
    parts = name.split("/")
    if (len(parts) < 2 or parts[0] not in _FILE_DIRS or "\\" in name or ":" in name
            or any(not _NAME_RE.match(p) or p in (".", "..") for p in parts[1:])):
        raise BackupError(f"unexpected file in backup: {name[:80]!r}")
    return parts[0]


def _validate_zip(zf: zipfile.ZipFile) -> dict[str, Any]:
    infos = zf.infolist()
    if len(infos) > _MAX_MEMBERS:
        raise BackupError("the backup has too many files")
    total = 0
    for info in infos:
        _check_name(info.filename)
        if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise BackupError("the backup contains a folder entry or a link")
        total += info.file_size
        if info.compress_size and info.file_size / info.compress_size > _MAX_RATIO \
                and info.file_size > 10 * 1024 * 1024:
            raise BackupError("the backup looks like a ZIP bomb")
    if total > _MAX_TOTAL:
        raise BackupError("the backup is too large")
    try:
        manifest = json.loads(zf.read("manifest.json"))
    except (KeyError, ValueError) as exc:
        raise BackupError("this is not a Nexus backup (no manifest)") from exc
    if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("this is not a Nexus backup, or its format is not supported")
    if _version_tuple(str(manifest.get("app_version"))) > _version_tuple(__version__):
        raise BackupError(
            f"this backup was made by Nexus {manifest.get('app_version')}; "
            f"update Nexus (now {__version__}) before restoring it"
        )
    listed = set(manifest.get("files") or {})
    present = {i.filename for i in infos} - {"manifest.json"}
    if listed != present or "nexus.db" not in present:
        raise BackupError("the backup's file list does not match its manifest")
    return manifest


def _extract_verified(zf: zipfile.ZipFile, name: str, expected: dict, target: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    with zf.open(name) as src, open(target, "wb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            size += len(chunk)
            if size > int(expected.get("size", 0)):
                raise BackupError(f"{name} is larger than the manifest says")
            digest.update(chunk)
            dst.write(chunk)
    if digest.hexdigest() != expected.get("sha256") or size != int(expected.get("size", -1)):
        raise BackupError(f"{name} does not match its checksum")


def _check_db(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError("the database in the backup cannot be opened") from exc
    if ok != "ok":
        raise BackupError("the database in the backup failed its integrity check")
    if not {"items", "cases", "sources"} <= tables:
        raise BackupError("the database in the backup is not a Nexus database")


def restore_backup(archive: Path, passphrase: str = "", *, restore_settings: bool = True) -> RestoreReport:
    """Validate ``archive`` fully, back up the current state, then restore."""
    data = get_settings().data_path
    with tempfile.TemporaryDirectory(dir=backups_dir(), prefix=".restore-") as work:
        work_dir = Path(work)
        zip_path = archive
        if is_encrypted_backup(archive):
            if not passphrase:
                raise BackupError("this backup is encrypted: enter its passphrase")
            zip_path = work_dir / "archive.zip"
            with open(archive, "rb") as src, open(zip_path, "wb") as dst:
                _decrypt_archive(src, dst, passphrase)
        if not zipfile.is_zipfile(zip_path):
            raise BackupError("this is not a Nexus backup file")
        staged = work_dir / "staged"
        staged.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            manifest = _validate_zip(zf)
            files = manifest["files"]
            for name in sorted(files):
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                _extract_verified(zf, name, files[name], target)
        _check_db(staged / "nexus.db")

        # Everything checked: keep the current state before changing anything.
        pre = create_backup(label="pre-restore")
        report = RestoreReport(True, "Backup restored.", pre_restore_backup=pre.path.name)

        live = sqlite3.connect(get_settings().db_path)
        try:
            restored_db = sqlite3.connect(staged / "nexus.db")
            try:
                restored_db.backup(live)
            finally:
                restored_db.close()
        finally:
            live.close()

        for folder in _FILE_DIRS:
            wanted = {n for n in files if n.startswith(folder + "/")}
            base = data / folder
            if base.is_dir():
                for root, _dirs, names in os.walk(base, followlinks=False):
                    for n in names:
                        p = Path(root) / n
                        if not n.startswith(".") and p.relative_to(data).as_posix() not in wanted:
                            p.unlink(missing_ok=True)
            for name in wanted:
                dest = data / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged / name, dest)
                report.restored_files += 1

        from nexus.security import atrest

        if "keystore.json" in files:
            for name in _KEYSTORE_FILES:
                if (staged / name).is_file():
                    os.replace(staged / name, data / name)
            atrest.lock()
            if not atrest.try_auto_unlock():
                report.warnings.append("Encrypted files were restored: unlock Nexus with that backup's passphrase.")
        elif atrest.load_keystore() is not None:
            report.warnings.append("This backup has no encryption keystore; the current one was kept.")

        if restore_settings:
            _restore_settings((staged / "settings.env").read_text(encoding="utf-8"))
        atrest.start_background_conversion()
    logger.info("Backup restored (%d files); previous state saved as %s",
                report.restored_files, report.pre_restore_backup)
    return report


def _restore_settings(text: str) -> None:
    """Write the backup's non-secret settings into ``.env`` (secrets untouched)."""
    from nexus.config import Settings
    from nexus.envstore import _env_path, _format_value, reload_settings

    known = {name.upper() for name in Settings.model_fields}
    updates = {k: v.replace("\r", "").replace("\n", "") for k, v in _parse_env(text).items()
               if k in known and _ENV_NAME.match(k) and not _is_secret_name(k)
               and k not in _NOT_RESTORED}
    if not updates:
        return
    path = _env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            out.append(f"{key}={_format_value(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={_format_value(v)}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    reload_settings()


def list_backups() -> list[dict[str, Any]]:
    out = []
    for p in sorted(backups_dir().glob("nexus-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix in (".zip", ".nxb"):
            out.append({"name": p.name, "size": p.stat().st_size,
                        "encrypted": p.suffix == ".nxb",
                        "modified": datetime.fromtimestamp(p.stat().st_mtime, UTC)
                        .strftime("%Y-%m-%d %H:%M UTC")})
    return out


def backup_path(name: str) -> Path | None:
    """The file for a listed backup name, or None (no traversal possible)."""
    for entry in list_backups():
        if entry["name"] == name:
            return backups_dir() / entry["name"]
    return None
