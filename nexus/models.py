"""Domain models for the collection -> analysis pipeline.

`RawItem`   — what a Source/Adapter emits before any processing.
`Analysis`  — the Claude-produced intelligence layer for an item.
`ProcessedItem` — a RawItem enriched with dedup + analysis, ready for storage/UI.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

_URL_RE = re.compile(r"https?://\S+")
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTISPACE_RE = re.compile(r"\s+")


class ThreatLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Party(str, Enum):
    """First-party = the target's own words; third-party = others about them."""

    FIRST = "first_party"
    THIRD = "third_party"
    UNKNOWN = "unknown"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RawItem(BaseModel):
    """A single piece of content as fetched from a source, pre-analysis."""

    source: str = Field(description="Logical source name, e.g. 'rss', 'telegram'.")
    track: str = Field(default="api", description="Acquisition track (api for v1).")
    external_id: str | None = Field(
        default=None, description="Source-native id used for de-dup of re-fetches."
    )
    url: str | None = None
    author: str | None = None
    title: str | None = None
    content: str = Field(default="", description="Original-language text.")
    language: str | None = Field(default=None, description="ISO code if known.")
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)
    media_urls: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict, description="Untouched source payload.")

    @property
    def content_hash(self) -> str:
        """Exact identity of the stored text (light normalization)."""
        basis = (self.content or self.title or self.url or "").strip().lower()
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    @property
    def dedup_key(self) -> str:
        """Aggressively normalized key for near-duplicate clustering.

        Lowercase, strip URLs and punctuation, collapse whitespace — so reposts
        with minor formatting differences collapse into one cluster.
        """
        text = (self.content or self.title or "").lower()
        text = _URL_RE.sub(" ", text)
        text = _NONWORD_RE.sub(" ", text)
        text = _MULTISPACE_RE.sub(" ", text).strip()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Analysis(BaseModel):
    """Claude's intelligence layer for one item (all fields optional)."""

    threat_level: ThreatLevel = ThreatLevel.NONE
    summary: str | None = None
    translation: str | None = Field(
        default=None, description="Content translated to the configured target language."
    )
    target_lang: str | None = None
    entities: list[str] = Field(
        default_factory=list,
        description="Flat list of all entities (back-compat / FTS convenience).",
    )
    entity_groups: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Entities grouped by type: people/organizations/locations/identifiers.",
    )
    party: Party = Party.UNKNOWN
    contradiction: bool = Field(
        default=False, description="True if cross-source disinformation suspected."
    )
    confidence: str | None = Field(
        default=None, description="Model self-confidence: low / medium / high."
    )
    model: str | None = None
    analyzed_at: datetime | None = None


class ProcessedItem(BaseModel):
    """A stored item: raw content + dedup state + (optional) analysis."""

    id: int | None = None
    raw: RawItem
    content_hash: str
    cluster_id: str | None = None
    shared_count: int = Field(default=1, description="Echo/virality counter.")
    analysis: Analysis | None = None

    @classmethod
    def from_raw(cls, raw: RawItem) -> ProcessedItem:
        return cls(raw=raw, content_hash=raw.content_hash)

    @property
    def is_analyzed(self) -> bool:
        return self.analysis is not None and self.analysis.analyzed_at is not None

    @property
    def echo_tag(self) -> str | None:
        """UI badge for viral/echoed items."""
        if self.shared_count > 1:
            return f"🔥 Echoed {self.shared_count} times"
        return None
