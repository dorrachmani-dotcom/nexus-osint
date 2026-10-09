"""Telegram source — public channel posts via the Telemetry API.

Telemetry (telemetryapp.io / api.telemetr.io — the same service) is a search
engine and analytics platform over public Telegram channels. This source is
key-gated: it needs TELEMETRY_API_KEY plus either at least one channel in
TELEGRAM_CHANNELS or a unified investigation query. v1 is API-only (no
Telethon/stealth). Without a key the source reports itself unavailable and is
skipped — nothing crashes.

API shape (OpenAPI v1, base https://api.telemetr.io/v1):
  - Auth: a header named ``api_key`` (NOT ``Authorization: Bearer``). The key
    is generated via Telemetry's @telemetrio_api_bot.
  - A channel is addressed by its *internal_id*, not its @handle, so we first
    resolve a handle through ``GET /v1/channels/search?term=@handle``.
  - Channel posts:   ``GET /v1/messages/channel?internal_id=...&short_info=true``
  - Keyword search:  ``GET /v1/search/messages?term=...&period=30d``
Response fields are read defensively so minor drift degrades to "fewer fields"
rather than a crash. TELEMETRY_BASE_URL can override the host.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from nexus.config import Settings, get_settings
from nexus.models import RawItem
from nexus.sources.base import Source

logger = logging.getLogger("nexus.sources.telegram")

_LIMIT_PER_CHANNEL = 50
_SEARCH_PERIOD = "30d"  # 7d|14d|30d|60d|90d|all — how far back keyword search looks
_TIMEOUT = 30.0

# Pull a @username out of a t.me link, e.g. https://t.me/durov -> durov.
_TME_USERNAME = re.compile(r"t\.me/(?:s/)?(?:joinchat/)?([A-Za-z0-9_]+)")


def _parse_date(value) -> datetime | None:
    if value is None:
        return None
    # Accept unix epoch seconds or an ISO string.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _username_from_link(link: str | None) -> str | None:
    if not link:
        return None
    m = _TME_USERNAME.search(str(link))
    return m.group(1) if m else None


class TelegramSource(Source):
    name = "telegram"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # Cache @handle -> internal_id within a single fetch run to avoid
        # spending a resolve request per channel on every scan.
        self._id_cache: dict[str, str | None] = {}

    def is_available(self) -> bool:
        return bool(
            self.settings.telemetry_api_key
            and (
                self.settings.telegram_channel_targets()
                or self.settings.investigation_query_targets()
            )
        )

    # ------------------------------------------------------------------ helpers
    def _normalize_channel(self, channel: str) -> str:
        return channel.lstrip("@").strip()

    def _headers(self) -> dict[str, str]:
        # Telemetry uses an `api_key` header, not Bearer auth.
        return {"api_key": self.settings.telemetry_api_key or ""}

    @staticmethod
    def _chat_index(chats) -> dict[str, dict]:
        """Map internal_id -> ChatShort/ChatInfo dict for URL/handle lookup."""
        index: dict[str, dict] = {}
        if isinstance(chats, list):
            for c in chats:
                if isinstance(c, dict) and c.get("internal_id") is not None:
                    index[str(c["internal_id"])] = c
        return index

    def _resolve_internal_id(self, base: str, handle: str) -> str | None:
        """Resolve a public @handle (or t.me link) to a Telemetry internal_id."""
        if handle in self._id_cache:
            return self._id_cache[handle]
        internal_id: str | None = None
        try:
            resp = httpx.get(
                f"{base}/channels/search",
                headers=self._headers(),
                params={"term": handle, "limit": 1},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            chats = data if isinstance(data, list) else (
                data.get("chats") or data.get("results") or data.get("channels") or []
            )
            for c in chats:
                if isinstance(c, dict) and c.get("internal_id") is not None:
                    internal_id = str(c["internal_id"])
                    break
        except Exception as exc:
            logger.warning("Telegram channel resolve failed: %s (%s)", handle, exc)
        self._id_cache[handle] = internal_id
        return internal_id

    def _map_message(
        self,
        msg: dict,
        chats: dict[str, dict],
        fallback_handle: str | None,
        query: str | None = None,
    ) -> RawItem | None:
        """Map one API message dict to a RawItem, tolerating field-name drift."""
        if not isinstance(msg, dict):
            return None
        text = (msg.get("text") or msg.get("message") or "").strip()
        if not text:
            return None

        peer_id = msg.get("peer_id") or msg.get("channel_id") or msg.get("internal_id")
        message_id = msg.get("message_id") or msg.get("id")

        # Prefer the channel handle from the joined chat metadata; fall back to
        # the @handle we queried with.
        chat = chats.get(str(peer_id)) if peer_id is not None else None
        username = None
        title = None
        if chat:
            username = _username_from_link(chat.get("link")) or chat.get("username")
            title = chat.get("title")
        username = username or fallback_handle
        author = username or title or (str(peer_id) if peer_id else "telegram")

        if username and message_id:
            url: str | None = f"https://t.me/{username}/{message_id}"
        else:
            url = msg.get("link") or (chat.get("link") if chat else None)

        external_id = None
        if peer_id is not None and message_id is not None:
            external_id = f"{peer_id}:{message_id}"

        published = _parse_date(msg.get("date") or msg.get("created_at"))
        return RawItem(
            source=self.name,
            track="api",
            external_id=external_id,
            url=url,
            author=author,
            title=title,
            content=text,
            published_at=published,
            raw={"channel": username, "query": query, "message": msg},
        )

    @staticmethod
    def _extract_messages(data) -> tuple[list, list]:
        """Pull (messages, chats) out of a response in either shape."""
        if isinstance(data, list):
            return data, []
        if isinstance(data, dict):
            messages = (
                data.get("messages")
                or data.get("results")
                or data.get("items")
                or []
            )
            chats = data.get("chats") or data.get("short_info") or []
            return (messages if isinstance(messages, list) else []), chats
        return [], []

    # -------------------------------------------------------------------- fetch
    def fetch(self, since: datetime | None = None) -> list[RawItem]:
        base = self.settings.telemetry_base_url.rstrip("/")
        self._id_cache.clear()
        items: list[RawItem] = []

        for channel in self.settings.telegram_channel_targets():
            handle = self._normalize_channel(channel)
            internal_id = self._resolve_internal_id(base, f"@{handle}")
            if not internal_id:
                logger.warning("Telegram channel not found on Telemetry: %s", channel)
                continue
            try:
                resp = httpx.get(
                    f"{base}/messages/channel",
                    headers=self._headers(),
                    params={"internal_id": internal_id, "short_info": "true"},
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Telegram channel failed: %s (%s)", channel, exc)
                continue

            messages, chats = self._extract_messages(data)
            chat_index = self._chat_index(chats)
            for msg in messages[:_LIMIT_PER_CHANNEL]:
                item = self._map_message(msg, chat_index, handle)
                if item is None:
                    continue
                if since and item.published_at and item.published_at <= since:
                    continue
                items.append(item)

        # Unified investigation queries: keyword search across all of Telegram.
        for query in self.settings.investigation_query_targets():
            try:
                resp = httpx.get(
                    f"{base}/search/messages",
                    headers=self._headers(),
                    params={
                        "term": query,
                        "period": _SEARCH_PERIOD,
                        "return_short_info": "true",
                    },
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Telegram search unavailable: %s (%s)", query, exc)
                continue

            messages, chats = self._extract_messages(data)
            chat_index = self._chat_index(chats)
            for msg in messages[:_LIMIT_PER_CHANNEL]:
                # Search results may carry per-message chat metadata too.
                local_index = dict(chat_index)
                local_index.update(self._chat_index(msg.get("chats")))
                item = self._map_message(msg, local_index, None, query=query)
                if item is None:
                    continue
                if since and item.published_at and item.published_at <= since:
                    continue
                items.append(item)

        logger.info("Telegram fetched %d items", len(items))
        return items
