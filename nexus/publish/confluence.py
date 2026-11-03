"""Write back to Confluence: create a page, or append / update a section of an
existing page.

Built on :class:`nexus.sources.confluence.ConfluenceSource`, so it shares the
connection's credentials (from ``.env`` only), the SSRF / internal-network
guard, the TLS mode and the "same host only" rule. Writing additionally needs
the connection's own opt-in switch ("Allow publishing to Confluence", off by
default) and a token with write rights.

APIs
  * Cloud: REST v2 - ``POST /wiki/api/v2/pages``, ``PUT /wiki/api/v2/pages/{id}``
    (version + 1), ``GET /wiki/api/v2/spaces/{id}/operations`` for the write check.
  * Data Center / Server: REST v1 - ``POST /rest/api/content``,
    ``PUT /rest/api/content/{id}``, ``GET /rest/api/space/{key}?expand=operations``.
  * Page search on both: v1 CQL ``/rest/api/content/search``.

HTTP 429 / 503 wait for ``Retry-After`` (capped) and retry; a 409 version
conflict re-reads the page and retries once.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import httpx

from nexus.netguard import ResponseTooLarge, safe_request, tls_verify_for
from nexus.sources.confluence import (
    _MAX_RETRIES,
    _TIMEOUT,
    ConfluenceError,
    ConfluenceSource,
    retry_after_seconds,
)

logger = logging.getLogger("nexus.publish.confluence")

_WRITE_BYTES = 10 * 1024 * 1024
_SPACE_KEY = re.compile(r"^[A-Za-z0-9_~.-]{1,255}$")
_PAGE_ID = re.compile(r"^[0-9]{1,30}$")


def _cql_string(text: str) -> str:
    """A CQL string literal (quotes and backslashes escaped, length bounded)."""
    cleaned = " ".join(str(text or "").split())[:200]
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def valid_page_id(value) -> str | None:
    value = str(value or "").strip()
    return value if _PAGE_ID.match(value) else None


def valid_space_key(value) -> str | None:
    value = str(value or "").strip()
    return value if _SPACE_KEY.match(value) else None


class ConfluencePublisher(ConfluenceSource):
    """A Confluence connection used for writing."""

    def __init__(self, cfg: dict, settings=None, *,
                 sleep: Callable[[float], None] | None = None) -> None:
        if sleep is None:
            super().__init__(cfg, settings)
        else:
            super().__init__(cfg, settings, sleep=sleep)
        self._space_ids: dict[str, str] = {}

    # ---------------------------------------------------------- readiness
    def write_allowed(self) -> tuple[bool, str]:
        """``(True, "")`` or ``(False, why)`` - never touches the network."""
        if not self.cfg.get("allow_publish"):
            return False, ("publishing is off for this connection: turn on "
                           "\"Allow publishing to Confluence\" under Sources > Confluence")
        if not self.base.lower().startswith(("http://", "https://")):
            return False, "the connection has no valid address"
        if not self._token():
            return False, "the connection's token is missing (Sources > Confluence)"
        if self.cloud and not (self.cfg.get("email") or "").strip():
            return False, "Confluence Cloud needs the account e-mail"
        return True, ""

    # --------------------------------------------------------------- HTTP
    def _send(self, method: str, url: str, *, json: dict | None = None,
              params: dict | None = None) -> httpx.Response:
        """One write-capable request; returns the response (even 4xx) after
        handling 429 / 503. Raises :class:`ConfluenceError` on transport errors."""
        if not self._same_host(url):
            raise ConfluenceError("refused a link to another host")
        headers = self._headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = safe_request(
                    method, url, timeout=_TIMEOUT, headers=headers, params=params, json=json,
                    verify=tls_verify_for(self.cfg.get("tls_mode")), max_bytes=_WRITE_BYTES,
                )
            except ResponseTooLarge as exc:
                raise ConfluenceError("a response was too large") from exc
            except ValueError as exc:  # refused by the SSRF guard
                raise ConfluenceError(f"blocked: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ConfluenceError(f"could not connect ({type(exc).__name__})") from exc
            if resp.status_code in (429, 503) and attempt < _MAX_RETRIES:
                wait = retry_after_seconds(resp.headers.get("retry-after"), attempt)
                logger.info("Confluence '%s' is rate-limited; waiting %.0fs", self.name, wait)
                self._sleep(wait)
                continue
            if resp.status_code in (429, 503):
                raise ConfluenceError("still rate-limited after several retries; try later")
            return resp
        raise ConfluenceError("still rate-limited after several retries; try later")

    @staticmethod
    def _message(resp: httpx.Response) -> str:
        """The server's own error text (bounded; never echoes request headers)."""
        try:
            data = resp.json()
        except ValueError:
            return ""
        msg = ""
        if isinstance(data, dict):
            msg = str(data.get("message") or "")
            errs = data.get("errors")
            if not msg and isinstance(errs, list) and errs and isinstance(errs[0], dict):
                msg = str(errs[0].get("title") or errs[0].get("detail") or "")
        return " ".join(msg.split())[:200]

    def _raise_for(self, resp: httpx.Response, what: str) -> None:
        code = resp.status_code
        if code < 400:
            return
        if code in (401, 403):
            raise ConfluenceError(
                f"HTTP {code}: the token cannot {what} here - it needs write permission "
                "in this space (check the account's space permissions and token scopes)")
        if code == 404:
            raise ConfluenceError(f"HTTP 404: could not {what} (page or space not found)")
        msg = self._message(resp)
        if code == 400 and "already exists" in msg.lower():
            raise ConfluenceError("a page with this title already exists in that space - "
                                  "change the title or append to that page instead")
        raise ConfluenceError(f"HTTP {code}: could not {what}" + (f" ({msg})" if msg else ""))

    def _json_of(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ConfluenceError("the server did not answer with JSON") from exc
        if not isinstance(data, dict):
            raise ConfluenceError("unexpected response shape")
        return data

    def _page_url(self, data: dict) -> str | None:
        webui = (data.get("_links") or {}).get("webui")
        if isinstance(webui, str) and webui.startswith("/"):
            return self.web_base + webui
        return None

    # -------------------------------------------------------------- spaces
    def space_id(self, key: str) -> str:
        """Cloud only: the numeric space id the v2 API wants."""
        if key not in self._space_ids:
            data = self._json(f"{self.v2}/spaces", {"keys": key, "limit": 1})
            results = [r for r in data.get("results") or [] if isinstance(r, dict)]
            if not results or not results[0].get("id"):
                raise ConfluenceError(f"space {key} not found or not readable")
            self._space_ids[key] = str(results[0]["id"])
        return self._space_ids[key]

    def write_permission(self, key: str) -> bool | None:
        """Whether this account may create pages in space ``key`` (None = unknown)."""
        try:
            if self.cloud:
                sid = self.space_id(key)
                data = self._json(f"{self.v2}/spaces/{sid}/operations")
            else:
                data = self._json(f"{self.v1}/space/{key}", {"expand": "operations"})
        except ConfluenceError:
            return None
        ops = data.get("operations")
        if isinstance(ops, dict):  # some versions wrap the list
            ops = ops.get("results")
        if not isinstance(ops, list):
            return None
        for op in ops:
            if not isinstance(op, dict):
                continue
            if (str(op.get("operation")).lower() in ("create", "update")
                    and str(op.get("targetType")).lower() in ("page", "space")):
                return True
        return False

    def test_write(self) -> dict:
        """For "Test connection": readable spaces plus write permission per chosen space."""
        result = self.test_connection()
        if not result.get("ok"):
            return result
        keys = self.spaces() or [s["key"] for s in result.get("spaces", [])[:5]]
        result["write"] = [{"key": k, "write": self.write_permission(k)} for k in keys[:10]]
        result["allow_publish"] = bool(self.cfg.get("allow_publish"))
        return result

    # --------------------------------------------------------------- pages
    def search_pages(self, query: str, space: str | None = None, limit: int = 10) -> list[dict]:
        parts = ["type = page"]
        if space and valid_space_key(space):
            parts.append(f'space = "{space}"')
        q = (query or "").strip()
        if q:
            parts.append(f"title ~ {_cql_string(q)}")
        cql = " AND ".join(parts) + " ORDER BY lastmodified DESC"
        data = self._json(f"{self.v1}/content/search",
                          {"cql": cql, "limit": max(1, min(limit, 25)), "expand": "space"})
        out = []
        for r in data.get("results") or []:
            if not isinstance(r, dict) or not valid_page_id(r.get("id")):
                continue
            out.append({"id": str(r["id"]), "title": str(r.get("title") or r["id"])[:300],
                        "space": str((r.get("space") or {}).get("key") or ""),
                        "url": self._page_url(r)})
        return out

    def get_page(self, page_id: str) -> dict:
        pid = valid_page_id(page_id)
        if pid is None:
            raise ConfluenceError("invalid page id")
        if self.cloud:
            resp = self._send("GET", f"{self.v2}/pages/{pid}", params={"body-format": "storage"})
        else:
            resp = self._send("GET", f"{self.v1}/content/{pid}",
                              params={"expand": "body.storage,version,space"})
        self._raise_for(resp, "read that page")
        data = self._json_of(resp)
        return {
            "id": pid, "title": str(data.get("title") or pid),
            "version": int((data.get("version") or {}).get("number") or 1),
            "body": str(((data.get("body") or {}).get("storage") or {}).get("value") or ""),
            "space": str((data.get("space") or {}).get("key") or ""),
            "space_id": str(data.get("spaceId") or ""),
            "url": self._page_url(data),
        }

    def create_page(self, space_key: str, title: str, body: str,
                    parent_id: str | None = None) -> dict:
        key = valid_space_key(space_key)
        if key is None:
            raise ConfluenceError("choose a space")
        parent = valid_page_id(parent_id) if parent_id else None
        if self.cloud:
            payload: dict = {"spaceId": self.space_id(key), "status": "current", "title": title,
                             "body": {"representation": "storage", "value": body}}
            if parent:
                payload["parentId"] = parent
            resp = self._send("POST", f"{self.v2}/pages", json=payload)
        else:
            payload = {"type": "page", "title": title, "space": {"key": key},
                       "body": {"storage": {"value": body, "representation": "storage"}}}
            if parent:
                payload["ancestors"] = [{"id": parent}]
            resp = self._send("POST", f"{self.v1}/content", json=payload)
        self._raise_for(resp, "create a page")
        data = self._json_of(resp)
        pid = valid_page_id(data.get("id"))
        if pid is None:
            raise ConfluenceError("the server did not return the new page id")
        return {"id": pid, "title": str(data.get("title") or title), "space": key,
                "url": self._page_url(data),
                "version": int((data.get("version") or {}).get("number") or 1)}

    def _put(self, page: dict, title: str, body: str) -> httpx.Response:
        number = page["version"] + 1
        if self.cloud:
            payload = {"id": page["id"], "status": "current", "title": title,
                       "body": {"representation": "storage", "value": body},
                       "version": {"number": number, "message": "Updated by Nexus-OSINT"}}
            return self._send("PUT", f"{self.v2}/pages/{page['id']}", json=payload)
        payload = {"id": page["id"], "type": "page", "title": title,
                   "version": {"number": number},
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        return self._send("PUT", f"{self.v1}/content/{page['id']}", json=payload)

    def update_page(self, page_id: str, build: Callable[[dict], tuple[str, str]]) -> dict:
        """Read the page, ``build(page) -> (title, body)``, write version + 1.

        A 409 (someone saved in between) re-reads the page and retries once.
        """
        for attempt in range(2):
            page = self.get_page(page_id)
            title, body = build(page)
            resp = self._put(page, title, body)
            if resp.status_code == 409 and attempt == 0:
                logger.info("Confluence '%s': version conflict on a page; retrying once",
                            self.name)
                continue
            if resp.status_code == 409:
                raise ConfluenceError("the page kept changing while saving (version "
                                      "conflict) - try again in a moment")
            self._raise_for(resp, "update that page")
            data = self._json_of(resp)
            return {"id": page["id"], "title": str(data.get("title") or title),
                    "space": page.get("space") or "",
                    "url": self._page_url(data) or page.get("url"),
                    "version": int((data.get("version") or {}).get("number")
                                   or page["version"] + 1)}
        raise ConfluenceError("version conflict")  # pragma: no cover - loop always returns


def load_publisher(conn, source_id: int, settings=None) -> ConfluencePublisher | None:
    from nexus.storage.confluence_sources import get_confluence_source

    row = get_confluence_source(conn, source_id)
    return ConfluencePublisher(row, settings) if row else None
