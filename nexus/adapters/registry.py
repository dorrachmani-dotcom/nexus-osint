"""Adapter registry — single place the web layer asks for tool adapters."""

from __future__ import annotations

from nexus.adapters.base import ToolAdapter
from nexus.adapters.dnstwist import DnstwistAdapter
from nexus.adapters.ghunt import GHuntAdapter
from nexus.adapters.h8mail import H8mailAdapter
from nexus.adapters.holehe import HoleheAdapter
from nexus.adapters.maigret import MaigretAdapter
from nexus.adapters.onionsearch import OnionSearchAdapter
from nexus.adapters.phoneinfoga import PhoneInfogaAdapter
from nexus.adapters.sherlock import SherlockAdapter
from nexus.adapters.socialscan import SocialscanAdapter
from nexus.adapters.spiderfoot import SpiderFootAdapter
from nexus.adapters.subfinder import SubfinderAdapter
from nexus.adapters.theharvester import TheHarvesterAdapter
from nexus.adapters.toutatis import ToutatisAdapter
from nexus.adapters.ytdlp import YtDlpAdapter
from nexus.config import Settings, get_settings

# Order matters only for display. Grouped by target type:
# username -> email -> phone -> domain -> social/media -> dark web.
_ADAPTER_CLASSES: list[type[ToolAdapter]] = [
    SherlockAdapter,
    MaigretAdapter,
    SocialscanAdapter,
    HoleheAdapter,
    H8mailAdapter,
    GHuntAdapter,
    PhoneInfogaAdapter,
    TheHarvesterAdapter,
    SubfinderAdapter,
    DnstwistAdapter,
    SpiderFootAdapter,
    ToutatisAdapter,
    YtDlpAdapter,
    OnionSearchAdapter,
]


def all_adapters(settings: Settings | None = None) -> list[ToolAdapter]:
    settings = settings or get_settings()
    return [cls(settings) for cls in _ADAPTER_CLASSES]


def get_adapter(name: str, settings: Settings | None = None) -> ToolAdapter | None:
    for adapter in all_adapters(settings):
        if adapter.name.lower() == (name or "").lower():
            return adapter
    return None
