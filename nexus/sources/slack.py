"""Slack (read-only): chosen channels, stored as one document per channel per day.

A bot token from a custom app in your own workspace reads the channels the bot
was invited to; "Test connection" offers those to choose from. The first scan
goes back ``FIRST_DAYS``; later scans start at the day of the previous scan, so
today's document grows as the day goes on and an edited message or a new thread
reply on a recent day updates that day's document. Thread replies sit under
their message. Files are not downloaded.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial

from nexus.ingest.connector import ConnectorDoc
from nexus.sources.cloud import CloudAuthError, CloudError, CloudSource, Entry

API = "https://slack.com/api"
FIRST_DAYS = 30
_PAGE = 200
_MAX_MESSAGES = 20_000   # one channel, one scan
_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired",
                "account_inactive"}
_NOISE = {"channel_join", "channel_leave", "channel_topic", "channel_purpose",
          "channel_name", "channel_archive", "channel_unarchive", "bot_add", "bot_remove"}
_USER = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]*)>")
_BARE = re.compile(r"<(https?://[^>]+)>")


def _when(ts: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(str(ts)), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class SlackSource(CloudSource):
    KIND = "slack"
    LABEL = "Slack"
    HOSTS = ("slack.com",)
    INTRO = ("Reads chosen channels, one document per day. Create a custom app in your "
             "workspace, then invite its bot to each channel with /invite.")
    KEY_LABEL = "Bot token"
    KEY_HINT = ("Starts with xoxb-. Read scopes only: channels:read, channels:history, "
                "groups:read, groups:history, users:read.")
    SCOPE_NOUN = "channels"
    NEEDS_SCOPE = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._users: dict[str, str] = {}

    def _api(self, method: str, **params: object) -> dict:
        data = self.json("GET", f"{API}/{method}", params=params)
        if data.get("ok"):
            return data
        error = re.sub(r"[^a-z_]", "", str(data.get("error") or "unknown"))[:60]
        if error in _AUTH_ERRORS:
            raise CloudAuthError(f"Slack refused the token ({error})")
        raise CloudError(f"Slack said: {error}")

    def _paged(self, method: str, key: str, **params: object) -> Iterator[dict]:
        cursor = ""
        while True:
            data = self._api(method, limit=_PAGE, **params, **({"cursor": cursor} if cursor else {}))
            yield from (m for m in data.get(key) or [] if isinstance(m, dict))
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return

    def test_connection(self) -> dict:
        try:
            me = self._api("auth.test")
            channels = [c for c in self._paged(
                "conversations.list", "channels", types="public_channel,private_channel",
                exclude_archived="true") if c.get("is_member")]
        except CloudError as exc:
            return {"ok": False, "error": str(exc)}
        note = "" if channels else "The bot is in no channel yet: invite it with /invite in Slack."
        return {"ok": True, "account": f"{me.get('user')} in {me.get('team')}",
                "items": [{"key": str(c.get("id")), "name": f"#{c.get('name')}"} for c in channels],
                "note": note}

    # ---------------------------------------------------------- reading
    def _user(self, user_id: str) -> str:
        if user_id not in self._users:
            try:
                user = self._api("users.info", user=user_id).get("user") or {}
                profile = user.get("profile") or {}
                self._users[user_id] = (profile.get("display_name") or profile.get("real_name")
                                        or user.get("name") or user_id)
            except CloudAuthError:
                raise
            except CloudError:  # no users:read: ids are still usable
                self._users[user_id] = user_id
        return self._users[user_id]

    def _clean(self, text: str) -> str:
        text = _USER.sub(lambda m: "@" + self._user(m.group(1)), text or "")
        text = _CHANNEL.sub(r"#\1", text)
        text = _LINK.sub(r"\2 (\1)", text)
        text = _BARE.sub(r"\1", text)
        return html.unescape(text)

    def _line(self, msg: dict, indent: str = "") -> str:
        when = _when(msg.get("ts"))
        stamp = when.strftime("%H:%M") if when else "--:--"
        who = (self._user(str(msg["user"])) if msg.get("user")
               else str(msg.get("username") or msg.get("bot_id") or "unknown"))
        return f"{indent}[{stamp}] {who}: {self._clean(str(msg.get('text') or ''))}"

    def _load(self, channel: str, name: str, day: str, messages: list[dict],
              version: str, modified: datetime | None) -> ConnectorDoc:
        lines: list[str] = []
        for msg in messages:
            lines.append(self._line(msg))
            if int(msg.get("reply_count") or 0):
                replies = self._paged("conversations.replies", "messages",
                                      channel=channel, ts=str(msg.get("ts")))
                lines.extend(self._line(r, "    ") for r in replies
                             if r.get("ts") != msg.get("ts"))
        return ConnectorDoc(
            key=f"{channel}/{day}", title=f"#{name} {day}", text="\n".join(lines),
            url=f"https://slack.com/archives/{channel}", modified=modified, version=version,
            meta={"channel": name, "channel_id": channel, "day": day, "messages": len(messages)},
        )

    def listing(self, since: datetime | None) -> Iterator[Entry]:
        start = since or datetime.now(UTC) - timedelta(days=FIRST_DAYS)
        oldest = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        for channel in self.scope():
            info = self._api("conversations.info", channel=channel).get("channel") or {}
            name = str(info.get("name") or channel)
            days: dict[str, list[dict]] = {}
            for count, msg in enumerate(self._paged("conversations.history", "messages",
                                                    channel=channel,
                                                    oldest=str(oldest.timestamp())), 1):
                if count > _MAX_MESSAGES:
                    raise CloudError(f"#{name} has too many messages for one scan")
                when = _when(msg.get("ts"))
                if when and msg.get("subtype") not in _NOISE:
                    days.setdefault(when.strftime("%Y-%m-%d"), []).append(msg)
            for day in sorted(days):
                messages = sorted(days[day], key=lambda m: float(str(m.get("ts"))))
                # Changes with a new or edited message or a new thread reply.
                state = "|".join(
                    f"{m.get('ts')}:{(m.get('edited') or {}).get('ts', '')}:{m.get('latest_reply', '')}"
                    for m in messages)
                version = hashlib.sha256(state.encode()).hexdigest()[:32]
                modified = _when(messages[-1].get("ts"))
                yield Entry(key=f"{channel}/{day}", version=version, modified=modified,
                            load=partial(self._load, channel, name, day, messages, version, modified))
