"""SharePoint and OneDrive (read-only), through Microsoft Graph.

An app registration with application permissions signs in with a client secret.
"Test connection" offers the document libraries of the tenant's sites, plus the
OneDrive of each user named on the connection. Each scan walks the chosen
libraries, so a file gone from them is removed here too. Files go through the
same readers as an upload. File content is served from the tenant's own
sharepoint.com host: Graph redirects there with a short-lived link, and the
token is not sent along.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Iterator
from datetime import datetime
from functools import partial
from urllib.parse import quote

from nexus.ingest.connector import ConnectorDoc
from nexus.ingest.safety import DEFAULT_LIMITS
from nexus.sources.cloud import CloudAuthError, CloudError, CloudSource, Entry, parse_when

API = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
_ITEM_FIELDS = "id,name,file,folder,size,cTag,eTag,lastModifiedDateTime,webUrl"
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,200}$")
_CLIENT = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_USER = re.compile(r"^[^\s/\\?#]{1,200}@[^\s/\\?#]{1,200}$")
_DRIVE = re.compile(r"^[\w!-]{1,300}$")
_MAX_SITES = 50
_MAX_FILES = 20_000


def parse_account(raw: str) -> tuple[str, str, list[str]]:
    """``tenant, client id[, user...]``. Raises ``ValueError`` with a readable message."""
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    if len(parts) < 2 or not _TENANT.match(parts[0]) or not _CLIENT.match(parts[1]):
        raise ValueError("Enter the tenant ID and the client ID, separated by a comma.")
    users = parts[2:]
    for user in users:
        if not _USER.match(user):
            raise ValueError(f"'{user[:80]}' is not a user's sign-in address.")
    return parts[0], parts[1], users


class SharePointSource(CloudSource):
    KIND = "sharepoint"
    LABEL = "SharePoint / OneDrive"
    HOSTS = ("graph.microsoft.com", "login.microsoftonline.com")
    REDIRECT_HOSTS = ("sharepoint.com",)
    INTRO = ("Reads chosen document libraries and OneDrives through an app registration "
             "with the read-only application permissions Sites.Read.All and Files.Read.All.")
    ACCOUNT_LABEL = "Tenant ID, client ID"
    ACCOUNT_HINT = ("Comma-separated, from the app registration. To read someone's OneDrive, "
                    "add their sign-in address after these.")
    KEY_LABEL = "Client secret"
    KEY_HINT = "The secret's value, from Certificates & secrets."
    SCOPE_NOUN = "libraries"
    NEEDS_SCOPE = True
    FULL_LISTING = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._access = ""
        self._expires = 0.0

    @classmethod
    def normalize_account(cls, raw: str) -> str:
        tenant, client, users = parse_account(raw)
        return ", ".join([tenant, client, *users])

    def headers(self) -> dict[str, str]:
        now = time.time()
        if not self._access or now > self._expires:
            try:
                tenant, client, _users = parse_account(str(self.cfg.get("account") or ""))
            except ValueError as exc:
                raise CloudAuthError(str(exc)) from exc
            try:
                data = self.json("POST", f"{LOGIN}/{tenant}/oauth2/v2.0/token", auth=False, data={
                    "grant_type": "client_credentials", "client_id": client,
                    "client_secret": self.token() or "",
                    "scope": "https://graph.microsoft.com/.default"})
            except CloudAuthError:
                raise
            except CloudError as exc:
                raise CloudAuthError("Microsoft refused this tenant, client ID or secret") from exc
            self._access = str(data.get("access_token") or "")
            if not self._access:
                raise CloudAuthError("Microsoft gave no access token")
            self._expires = now + int(data.get("expires_in") or 3600) - 120
        return {"Authorization": f"Bearer {self._access}"}

    def _paged(self, url: str, params: dict | None = None) -> Iterator[dict]:
        next_url: str | None = url
        while next_url:
            data = self.json("GET", next_url, params=params)
            yield from (v for v in data.get("value") or [] if isinstance(v, dict))
            next_url = data.get("@odata.nextLink")
            params = None  # the next link already carries its query

    def _libraries(self) -> list[dict]:
        found = []
        for count, site in enumerate(self._paged(f"{API}/sites", {"search": "*", "$select": "id,displayName"})):
            if count >= _MAX_SITES:
                break
            site_id = quote(str(site.get("id")), safe="")
            for drive in self._paged(f"{API}/sites/{site_id}/drives", {"$select": "id,name"}):
                found.append({"key": str(drive.get("id")),
                              "name": f"{site.get('displayName')} / {drive.get('name')}"})
        return found

    def test_connection(self) -> dict:
        try:
            tenant, _client, users = parse_account(str(self.cfg.get("account") or ""))
            items = self._libraries()
            missing = []
            for user in users:
                try:
                    drive = self.json("GET", f"{API}/users/{quote(user, safe='')}/drive",
                                      params={"$select": "id"})
                    items.append({"key": str(drive.get("id")), "name": f"OneDrive: {user}"})
                except CloudAuthError:
                    raise
                except CloudError:
                    missing.append(user)
        except (CloudError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        note = f"No OneDrive found for: {', '.join(missing)}." if missing else ""
        if not items:
            note = "No libraries found: check the app's permissions and admin consent."
        return {"ok": True, "account": f"tenant {tenant}", "items": items, "note": note}

    def _load(self, drive: str, info: dict) -> ConnectorDoc:
        name = str(info.get("name") or "untitled")
        limit = DEFAULT_LIMITS.max_file_bytes
        if int(info.get("size") or 0) > limit:
            raise CloudError(f"{name} is too large")
        item = quote(str(info.get("id")), safe="")
        resp = self.request("GET", f"{API}/drives/{drive}/items/{item}/content", max_bytes=limit)
        return ConnectorDoc(
            key=f"{drive}/{info.get('id')}", title=name,
            url=str(info.get("webUrl") or "") or None,
            modified=parse_when(info.get("lastModifiedDateTime")),
            version=self._version(info), attachments=[(name, resp.content)],
            meta={"drive": drive, "mime": (info.get("file") or {}).get("mimeType")},
        )

    @staticmethod
    def _version(info: dict) -> str | None:
        return str(info.get("cTag") or info.get("eTag") or "") or None

    def listing(self, since: datetime | None) -> Iterator[Entry]:
        count = 0
        for drive in self.scope():
            if not _DRIVE.match(drive):
                continue
            queue = deque(["root"])
            while queue:
                folder = quote(queue.popleft(), safe="")
                for info in self._paged(f"{API}/drives/{drive}/items/{folder}/children",
                                        {"$top": 200, "$select": _ITEM_FIELDS}):
                    if "folder" in info:
                        queue.append(str(info.get("id")))
                        continue
                    mime = str((info.get("file") or {}).get("mimeType") or "")
                    if "file" not in info or mime.startswith(("video/", "audio/")):
                        continue  # OneNote packages have no file to read
                    count += 1
                    if count > _MAX_FILES:
                        raise CloudError("stopped at the file limit for one scan")
                    yield Entry(key=f"{drive}/{info.get('id')}", version=self._version(info),
                                modified=parse_when(info.get("lastModifiedDateTime")),
                                load=partial(self._load, drive, info))
