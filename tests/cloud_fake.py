"""Helpers for the cloud connector tests: a fake service behind the real
``safe_request`` (``httpx.MockTransport``, faked DNS) and a stored connection."""

from __future__ import annotations

import socket

import httpx

from nexus import netguard


def install(monkeypatch, server) -> None:
    """Answer every request with ``server(request)``; every host looks public."""
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.40", 0))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", resolve)
    transport = httpx.MockTransport(server)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)


def connect(temp_db, kind: str, token: str, **cfg):
    """Store a connection and its token; return ready-to-use source."""
    from nexus.envstore import cloud_env_name, update_env
    from nexus.sources.cloud import make_source
    from nexus.storage.cloud_sources import create_cloud_source, get_cloud_source

    with temp_db() as conn:
        sid = create_cloud_source(conn, {"kind": kind, "name": kind.title(), **cfg})
        row = get_cloud_source(conn, sid)
    update_env({cloud_env_name(sid): token})
    src = make_source(row)
    src._sleep = lambda _s: None
    return src


def reload_source(temp_db, src):
    from nexus.sources.cloud import make_source
    from nexus.storage.cloud_sources import get_cloud_source

    with temp_db() as conn:
        fresh = make_source(get_cloud_source(conn, src.id))
    fresh._sleep = lambda _s: None
    return fresh


def documents(temp_db, kind: str) -> dict[str, dict]:
    with temp_db() as conn:
        return {r["filename"]: dict(r) for r in conn.execute(
            "SELECT * FROM documents WHERE kind = ?", (kind,))}


def item_text(temp_db) -> str:
    with temp_db() as conn:
        return "\n".join(r[0] or "" for r in conn.execute("SELECT content FROM items"))
