"""Google Drive (read-only): chosen folders, through a service account.

A service account sees only what is shared with its e-mail address, and its
token asks for the read-only Drive scope alone. "Test connection" offers the
folders shared with it. Each scan walks the chosen folders, so a file that is
gone from them is removed here too. Google Docs and Slides are exported as text
and Sheets as CSV; other files go through the same readers as an upload. The
JSON key is stored base64-encoded in ``.env`` (one safe line); the JWT is signed
with ``cryptography``, and the token endpoint is fixed here rather than taken
from the pasted key.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime
from functools import partial

from nexus.ingest.connector import ConnectorDoc
from nexus.ingest.safety import DEFAULT_LIMITS
from nexus.sources.cloud import CloudAuthError, CloudError, CloudSource, Entry, parse_when

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,size,webViewLink)"
_ID = re.compile(r"^[\w-]{1,200}$")
_ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
_MAX_FILES = 20_000


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class GoogleDriveSource(CloudSource):
    KIND = "gdrive"
    LABEL = "Google Drive"
    HOSTS = ("www.googleapis.com", "oauth2.googleapis.com")
    INTRO = ("Reads chosen folders through a service account. Share each folder with the "
             "account's e-mail address, as Viewer.")
    KEY_LABEL = "Service account JSON key"
    KEY_HINT = "Paste the whole key file. Only its e-mail and private key are kept."
    SCOPE_NOUN = "folders"
    NEEDS_SCOPE = True
    FULL_LISTING = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._access = ""
        self._expires = 0.0

    @classmethod
    def normalize_key(cls, raw: str) -> str:
        try:
            info = json.loads(raw)
            email, key = info["client_email"], info["private_key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("That is not a service account JSON key.") from exc
        if not (isinstance(email, str) and isinstance(key, str) and "PRIVATE KEY" in key):
            raise ValueError("That is not a service account JSON key.")
        kept = json.dumps({"client_email": email, "private_key": key}).encode()
        return base64.b64encode(kept).decode("ascii")

    def _key(self) -> dict:
        try:
            return json.loads(base64.b64decode(self.token() or ""))
        except (ValueError, TypeError) as exc:
            raise CloudAuthError("the stored key is not readable: connect again") from exc

    def _assertion(self, key: dict, now: int) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise CloudError("needs the 'cryptography' package") from exc
        try:
            private = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
        except (ValueError, TypeError, KeyError) as exc:
            raise CloudAuthError("the private key in this JSON key is not valid") from exc
        if not isinstance(private, rsa.RSAPrivateKey):
            raise CloudAuthError("the private key in this JSON key is not an RSA key")
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64url(json.dumps({"iss": key.get("client_email"), "scope": READ_ONLY_SCOPE,
                                   "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).encode())
        signed = f"{head}.{body}"
        return f"{signed}.{_b64url(private.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256()))}"

    def headers(self) -> dict[str, str]:
        now = time.time()
        if not self._access or now > self._expires:
            assertion = self._assertion(self._key(), int(now))
            try:
                data = self.json("POST", TOKEN_URL, auth=False, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion})
            except CloudAuthError:
                raise
            except CloudError as exc:
                raise CloudAuthError("Google refused this key") from exc
            self._access = str(data.get("access_token") or "")
            if not self._access:
                raise CloudAuthError("Google gave no access token")
            self._expires = now + int(data.get("expires_in") or 3600) - 120
        return {"Authorization": f"Bearer {self._access}"}

    def _files(self, query: str, fields: str = _FIELDS) -> Iterator[dict]:
        token = ""
        while True:
            params: dict = {"q": query, "fields": fields, "pageSize": 200, **_ALL_DRIVES}
            if token:
                params["pageToken"] = token
            data = self.json("GET", f"{API}/files", params=params)
            yield from (f for f in data.get("files") or [] if isinstance(f, dict))
            token = str(data.get("nextPageToken") or "")
            if not token:
                return

    def test_connection(self) -> dict:
        try:
            email = str(self._key().get("client_email"))
            folders = []
            for f in self._files(f"mimeType='{FOLDER}' and trashed=false",
                                 "nextPageToken,files(id,name)"):
                folders.append({"key": str(f.get("id")), "name": str(f.get("name"))})
                if len(folders) >= 500:
                    break
        except CloudError as exc:
            return {"ok": False, "error": str(exc)}
        note = "" if folders else f"No folders are shared with {email} yet."
        return {"ok": True, "account": email, "items": folders, "note": note}

    def _load(self, info: dict) -> ConnectorDoc:
        file_id, name = str(info.get("id")), str(info.get("name") or "untitled")
        mime = str(info.get("mimeType") or "")
        doc = ConnectorDoc(
            key=file_id, title=name, url=str(info.get("webViewLink") or "") or None,
            modified=parse_when(info.get("modifiedTime")), version=self._version(info),
            meta={"mime": mime},
        )
        if mime in _EXPORTS:
            doc.text = self.request("GET", f"{API}/files/{file_id}/export",
                                    params={"mimeType": _EXPORTS[mime]}).text
            return doc
        limit = DEFAULT_LIMITS.max_file_bytes
        if int(info.get("size") or 0) > limit:
            raise CloudError(f"{name} is too large")
        resp = self.request("GET", f"{API}/files/{file_id}", max_bytes=limit,
                            params={"alt": "media", "supportsAllDrives": "true"})
        doc.attachments = [(name, resp.content)]
        return doc

    @staticmethod
    def _version(info: dict) -> str | None:
        return str(info.get("md5Checksum") or info.get("modifiedTime") or "") or None

    def listing(self, since: datetime | None) -> Iterator[Entry]:
        queue = deque(f for f in self.scope() if _ID.match(f))
        seen = set(queue)
        count = 0
        while queue:
            for info in self._files(f"'{queue.popleft()}' in parents and trashed=false"):
                file_id, mime = str(info.get("id")), str(info.get("mimeType") or "")
                if mime == FOLDER:
                    if file_id not in seen and _ID.match(file_id):
                        seen.add(file_id)
                        queue.append(file_id)
                    continue
                if mime.startswith("application/vnd.google-apps.") and mime not in _EXPORTS:
                    continue  # forms, drawings, shortcuts: no text to read
                if mime.startswith(("video/", "audio/")):
                    continue
                count += 1
                if count > _MAX_FILES:
                    raise CloudError("stopped at the file limit for one scan")
                yield Entry(key=file_id, version=self._version(info),
                            modified=parse_when(info.get("modifiedTime")),
                            load=partial(self._load, info))
