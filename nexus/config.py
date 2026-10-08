"""Application configuration and source-availability resolution.

Loads settings from the environment / .env via Pydantic, then exposes helpers
that tell the rest of the system which sources are usable. v1 scope is API-only:
RSS is always available, every other source is enabled only when its API key is
present (otherwise skipped). Stealth/scraping tracks are a future feature.
Nothing here ever raises on a missing key — absence simply disables a capability.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where the .env secrets file lives. Normally the repo root (".env"), but a
# packaged/frozen build redirects it to a user-writable location (e.g.
# %LOCALAPPDATA%\Nexus\.env) via NEXUS_ENV_FILE, since the bundle itself is
# read-only. Honoured by both this loader and nexus.envstore so the dashboard's
# key editor and the settings loader always agree on one file.
_ENV_FILE = os.environ.get("NEXUS_ENV_FILE") or ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Intelligence core ---
    # Which AI backend analyses items. Default comes from .env; the dashboard can
    # override it at runtime (stored in the DB meta table). One of:
    #   anthropic | gemini | openai | ollama | off
    ai_provider: str = Field(default="anthropic")
    anthropic_api_key: str | None = Field(default=None)
    claude_model: str = Field(default="claude-opus-4-7")
    # Google Gemini as an alternative backend (key stays in .env, OpSec).
    gemini_api_key: str | None = Field(default=None)
    gemini_model: str = Field(default="gemini-3.5-flash")
    # OpenAI (ChatGPT) as an alternative backend (key stays in .env, OpSec).
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")
    # Local model via Ollama — runs entirely on this machine, so NO content ever
    # leaves it (the privacy-first choice for orgs that can't use cloud APIs).
    # No API key: it talks to a local Ollama server. Pick model + host below.
    ollama_base_url: str = Field(default="http://localhost:11434")
    # Gemma 4 (Google's open model): native function-calling + multimodal, runs
    # locally via Ollama. e4b (4B-effective) is the laptop-friendly default;
    # step up to gemma4:12b / gemma4:26b on a GPU. Fully offline, no key.
    ollama_model: str = Field(default="gemma4:e4b")
    # Budget guard: how many items reach the model per run. Kept modest so a
    # single scan stays inside free-tier rate/quota limits (e.g. Gemini free);
    # any overflow simply waits for the next scan. Raise it if you're on a paid
    # tier and want faster backlog catch-up.
    analysis_max_items_per_run: int = Field(default=100)
    # Language Claude translates non-target-language content into.
    translation_target_lang: str = Field(default="English")
    # Minimum content length (chars) worth spending tokens on.
    prefilter_min_length: int = Field(default=40)
    # Keyless English-translation fallback. When no AI provider is active (or it
    # produced no translation), non-English items are rendered to English via a
    # LibreTranslate HTTP instance at this URL (self-hosted or public). Empty
    # disables the HTTP backend; an optional offline Argos package may still be
    # used if installed. See nexus/translate.py. Used purely as an external web
    # API, never linked as code (LibreTranslate is AGPL).
    libretranslate_url: str | None = Field(default=None)

    # --- News / Search ---
    serpapi_key: str | None = Field(default=None)
    # Comma-separated search queries to pull from Google News via SERPAPI.
    serpapi_queries: str = Field(default="")
    rss_feeds: str = Field(default="")
    # Time window for keyless Google News search: how far back results may come
    # from. Accepts a number of days ("7", "1") — bounds both freshness and the
    # volume of items each scan pulls. Empty/0 means no limit.
    news_window_days: int = Field(default=7)

    # --- Google Custom Search (JSON API) ---
    # Keyed news/web search via Google's official Programmable Search Engine.
    # Needs BOTH an API key and a Search Engine ID (cx). Absent either, skipped.
    google_cse_key: str | None = Field(default=None)
    google_cse_cx: str | None = Field(default=None)

    # --- Telegram (Telemetry API — telemetryapp.io / api.telemetr.io) ---
    # Telemetry is a search/analytics service over public Telegram channels.
    # Generate the access key via its @telemetrio_api_bot; it is sent as an
    # `api_key` header. Channels are public @handles (resolved to internal ids
    # automatically). Unified investigation queries also run a keyword search
    # across all of public Telegram.
    telemetry_api_key: str | None = Field(default=None)
    telegram_channels: str = Field(default="")
    # The documented API host. telemetryapp.io and api.telemetr.io are the same
    # service; override only if your endpoint differs.
    telemetry_base_url: str = Field(default="https://api.telemetr.io/v1")

    # --- Twitter / X (official API) ---
    twitter_bearer_token: str | None = Field(default=None)
    # Comma-separated search queries for recent-search.
    twitter_queries: str = Field(default="")

    # --- Reddit ---
    reddit_client_id: str | None = Field(default=None)
    reddit_client_secret: str | None = Field(default=None)
    reddit_user_agent: str = Field(default="nexus-osint/0.1")
    # Comma-separated subreddits to pull "new" posts from, e.g. worldnews,osint
    reddit_subreddits: str = Field(default="")

    # --- Unified investigation queries ---
    # One search term, broadcast to every keyword-capable source (Google News,
    # Twitter/X, Reddit search, Telegram search). This is the "investigate X
    # everywhere" backbone; the analyst normally adds these from the dashboard,
    # and this env var seeds them. Comma-separated.
    investigation_queries: str = Field(default="")

    # --- Plug & Play OSINT tools (secrets some adapters need) ---
    # Instagram session cookie used by the Toutatis adapter. Kept in .env only;
    # without it the adapter reports itself unavailable and is skipped.
    instagram_sessionid: str | None = Field(default=None)

    # --- Storage ---
    data_dir: str = Field(default="data")
    database_path: str = Field(default="data/nexus.db")

    # --- Local web server ---
    # Port the dashboard is served on. The host is always loopback (127.0.0.1)
    # and is deliberately NOT configurable: Nexus never binds a public address.
    web_port: int = Field(default=8000)

    # ------------------------------------------------------------------ paths
    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        p = Path(self.database_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------- list-valued env helpers
    @property
    def rss_feed_list(self) -> list[str]:
        return _csv(self.rss_feeds)

    @property
    def telegram_channel_list(self) -> list[str]:
        return _csv(self.telegram_channels)

    @property
    def serpapi_query_list(self) -> list[str]:
        return _csv(self.serpapi_queries)

    @property
    def twitter_query_list(self) -> list[str]:
        return _csv(self.twitter_queries)

    @property
    def reddit_subreddit_list(self) -> list[str]:
        return _csv(self.reddit_subreddits)

    @property
    def investigation_query_list(self) -> list[str]:
        return _csv(self.investigation_queries)

    # ------------------------------------------- capability flags (API-only)
    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def ollama_enabled(self) -> bool:
        # No API key — just needs a host and a model name configured. Whether the
        # local server is actually up is checked at call time (graceful: a down
        # server makes the analysis pass a no-op for that run, never a crash).
        return bool(self.ollama_base_url and self.effective_ollama_model())

    def effective_ollama_model(self) -> str:
        """The Ollama model actually in use: the dashboard pick (DB meta) if the
        analyst chose one, else the .env default.

        Lets a non-technical user pick/download a model from the Settings page
        and have it take effect immediately — no .env editing, no restart. Like
        the provider choice, this is a preference, not a secret, so it lives in
        the DB meta table; the model files themselves stay on this machine.
        """
        from nexus.storage import get_meta_value

        choice = (get_meta_value("ollama_model", None) or "").strip()
        return choice or self.ollama_model

    # ------------------------------------------------- AI provider resolution
    # The chosen provider can be set in .env (ai_provider) and overridden from
    # the dashboard (stored in DB meta). API keys, however, ALWAYS come from
    # .env only — never the DB. Resolution never raises; on any error it falls
    # back to the env default.

    # Providers the analyst may pick from in the UI.
    PROVIDER_CHOICES: ClassVar[tuple[str, ...]] = (
        "anthropic",
        "gemini",
        "openai",
        "ollama",
        "off",
    )

    def chosen_provider(self) -> str:
        """The provider the analyst selected (DB override else env default).

        This is the raw preference; it may name a backend with no API key. Use
        `active_provider()` to get the one actually usable right now.
        """
        from nexus.storage import get_meta_value

        choice = (get_meta_value("ai_provider", None) or self.ai_provider or "anthropic")
        choice = choice.strip().lower()
        return choice if choice in self.PROVIDER_CHOICES else "anthropic"

    def active_provider(self) -> str:
        """Resolved provider that also has a usable key; 'off' otherwise."""
        choice = self.chosen_provider()
        if choice == "anthropic" and self.claude_enabled:
            return "anthropic"
        if choice == "gemini" and self.gemini_enabled:
            return "gemini"
        if choice == "openai" and self.openai_enabled:
            return "openai"
        if choice == "ollama" and self.ollama_enabled:
            return "ollama"
        return "off"

    @property
    def analysis_enabled(self) -> bool:
        return self.active_provider() != "off"

    @property
    def libretranslate_enabled(self) -> bool:
        # The keyless HTTP translation backend is usable once an endpoint is set.
        # Reachability is checked at call time (graceful: an unreachable service
        # simply yields no translation for that item, never a crash).
        return bool(self.libretranslate_url and self.libretranslate_url.strip())

    @property
    def analysis_model(self) -> str:
        """Model id for the active provider (for stamping Analysis.model)."""
        active = self.active_provider()
        if active == "gemini":
            return self.gemini_model
        if active == "openai":
            return self.openai_model
        if active == "ollama":
            return self.effective_ollama_model()
        return self.claude_model

    @property
    def rss_enabled(self) -> bool:
        # RSS is the always-on backbone; useful only once feeds are configured.
        return bool(self.rss_feed_list)

    @property
    def serpapi_enabled(self) -> bool:
        # Key plus at least one query to search for.
        return bool(self.serpapi_key and self.serpapi_query_list)

    @property
    def google_cse_enabled(self) -> bool:
        # Both key and engine id (cx) are required; queries come from capsules.
        return bool(
            self.google_cse_key
            and self.google_cse_cx
            and self.investigation_query_targets()
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telemetry_api_key and self.telegram_channel_list)

    @property
    def twitter_enabled(self) -> bool:
        return bool(self.twitter_bearer_token and self.twitter_query_list)

    @property
    def reddit_enabled(self) -> bool:
        return bool(
            self.reddit_client_id
            and self.reddit_client_secret
            and self.reddit_subreddit_list
        )

    # --------------------------------------- merged targets (env + DB topics)
    # Collection targets can come from two places: the static .env lists above
    # and the user-chosen subscriptions stored in the DB (the Topics page).
    # Sources use the merged, de-duplicated union so a target configured either
    # way is collected. DB lookups never raise (graceful: env-only on failure).

    @staticmethod
    def _db_targets(source: str) -> list[str]:
        from nexus.storage import get_subscription_values

        return get_subscription_values(source)

    @staticmethod
    def _union(env_list: list[str], db_list: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for v in [*env_list, *db_list]:
            if v and v not in seen:
                seen[v] = None
        return list(seen)

    @staticmethod
    def _case_term_targets() -> list[str]:
        from nexus.storage import get_case_term_values

        return get_case_term_values()

    def investigation_query_targets(self) -> list[str]:
        """The unified search terms broadcast to every keyword-capable source:
        env queries + legacy capsule terms + every Case's tracking words."""
        return self._union(
            self._union(self.investigation_query_list, self._db_targets("query")),
            self._case_term_targets(),
        )

    def rss_feed_targets(self) -> list[str]:
        return self._union(self.rss_feed_list, self._db_targets("rss"))

    def serpapi_query_targets(self) -> list[str]:
        # Source-specific queries PLUS the unified investigation queries.
        return self._union(
            self._union(self.serpapi_query_list, self._db_targets("serpapi")),
            self.investigation_query_targets(),
        )

    def twitter_query_targets(self) -> list[str]:
        # Source-specific queries PLUS the unified investigation queries.
        return self._union(
            self._union(self.twitter_query_list, self._db_targets("twitter")),
            self.investigation_query_targets(),
        )

    def reddit_subreddit_targets(self) -> list[str]:
        return self._union(self.reddit_subreddit_list, self._db_targets("reddit"))

    def telegram_channel_targets(self) -> list[str]:
        return self._union(self.telegram_channel_list, self._db_targets("telegram"))

    def cli_tool_available(self, binary: str) -> bool:
        """Whether a Plug & Play OSINT CLI tool is installed on PATH."""
        return shutil.which(binary) is not None

    def availability_report(self) -> dict[str, str]:
        """Human-readable snapshot for the dashboard / startup log.

        DB-aware: a source counts as "on" if it has targets from either .env or
        the Topics page (and, for key-gated sources, its API key is present).
        """
        on = lambda flag: "on" if flag else "off"  # noqa: E731
        # The AI badge reflects the *active* provider, not a hard-coded vendor:
        # pick Gemini and it reads "gemini", pick Claude and it reads "claude".
        ai_label = {
            "anthropic": "claude",
            "gemini": "gemini",
            "openai": "openai",
            "ollama": "local",
            "off": "ai",
        }.get(self.active_provider(), "ai")
        has_queries = bool(self.investigation_query_targets())
        reddit_api = bool(
            self.reddit_client_id
            and self.reddit_client_secret
            and (self.reddit_subreddit_targets() or has_queries)
        )
        return {
            ai_label: on(self.analysis_enabled),
            "rss": on(bool(self.rss_feed_targets())),
            # Keyless search sources: live whenever a capsule has terms.
            "gnews": on(has_queries),
            # GDELT: worldwide multilingual monitoring, keyless, on with terms.
            "gdelt": on(has_queries),
            # Reddit is "on" via the official API if configured, else via the
            # keyless search-RSS fallback whenever there are query terms.
            "reddit": on(reddit_api or has_queries),
            "serpapi": on(bool(self.serpapi_key and self.serpapi_query_targets())),
            # Google Custom Search: keyed, on when both key+cx set and a capsule
            # has terms to search for.
            "google_cse": on(
                bool(self.google_cse_key and self.google_cse_cx and has_queries)
            ),
            "telegram": on(
                bool(
                    self.telemetry_api_key
                    and (self.telegram_channel_targets() or self.investigation_query_targets())
                )
            ),
            "twitter": on(bool(self.twitter_bearer_token and self.twitter_query_targets())),
            "sherlock": on(self.cli_tool_available("sherlock")),
            "maigret": on(self.cli_tool_available("maigret")),
            "socialscan": on(self.cli_tool_available("socialscan")),
            "holehe": on(self.cli_tool_available("holehe")),
            "h8mail": on(self.cli_tool_available("h8mail")),
            "ghunt": on(self.cli_tool_available("ghunt")),
            "phoneinfoga": on(self.cli_tool_available("phoneinfoga")),
            "theHarvester": on(self.cli_tool_available("theHarvester")),
            "subfinder": on(self.cli_tool_available("subfinder")),
            "dnstwist": on(self.cli_tool_available("dnstwist")),
            "spiderfoot": on(self.cli_tool_available("sf")),
            "yt-dlp": on(self.cli_tool_available("yt-dlp")),
            "toutatis": on(
                self.cli_tool_available("toutatis") and bool(self.instagram_sessionid)
            ),
            "onionsearch": on(self.cli_tool_available("onionsearch")),
        }


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so .env is parsed once per process."""
    return Settings()
