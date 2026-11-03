"""Flexible custom API sources: formats, pagination, auth, POST template,
flatten fallback, SSRF policy and the planner/Auto-Adapt with non-JSON samples.

Requests go through the real ``nexus.netguard.safe_request`` (so the SSRF and
redirect policy is exercised) with faked DNS and an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import json
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from nexus import netguard
from nexus.sources import api_formats, custom as C

_DNS = {
    "api.example.org": "93.184.216.34",
    "auth.example.org": "93.184.216.35",
    "other.example.net": "93.184.216.36",
    "intranet.corp": "10.20.1.5",
    "loop.example.org": "127.0.0.1",
}


@pytest.fixture
def net(monkeypatch):
    """Fake DNS + a routable fake HTTP server; returns the request log."""

    def resolve(host, *args, **kwargs):
        addr = _DNS.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))]

    monkeypatch.setattr(netguard.socket, "getaddrinfo", resolve)
    routes: dict = {}
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        fn = routes.get(key)
        if fn is None:
            return httpx.Response(404, text="nope")
        return fn(request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", client)
    C.clear_token_cache()

    class Net:
        pass

    n = Net()
    n.routes = routes  # type: ignore[attr-defined]
    n.log = log  # type: ignore[attr-defined]
    return n


def _src(**cfg) -> C.CustomApiSource:
    base = {"id": 7, "name": "T", "enabled": 1, "base_url": "https://api.example.org",
            "endpoint": "/v1/items"}
    base.update(cfg)
    return C.CustomApiSource(base)


def _q(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


# ------------------------------------------------------------------ formats
def test_detect_format_by_type_and_sniff():
    d = api_formats.detect_format
    assert d("application/json", b"{}") == "json"
    assert d("application/x-ndjson", b"{}\n{}") == "ndjson"
    assert d("application/json", b'{"a":1}\n{"a":2}\n') == "ndjson"
    assert d("application/rss+xml", b"<rss/>") == "rss"
    assert d("application/xml", b"<feed xmlns='http://www.w3.org/2005/Atom'/>") == "rss"
    assert d("text/xml", b"<root><r/></root>") == "xml"
    assert d("text/csv", b"a,b\n1,2") == "csv"
    assert d("text/html; charset=utf-8", b"<html></html>") == "html"
    assert d(None, b"  [1,2]") == "json"
    assert d("text/plain", b"<!DOCTYPE html><html>") == "html"
    assert d("application/octet-stream", b"name;city\nAda;Paris\n") == "csv"
    assert d(None, b"just words") == "text"


@pytest.mark.parametrize(
    ("ctype", "body", "expect_titles"),
    [
        ("application/json", json.dumps({"data": [{"title": "A", "body": "x"}]}), ["A"]),
        ("application/x-ndjson", '{"title": "A"}\n{"title": "B"}\n', ["A", "B"]),
        ("text/csv", "title,body\nA,first\nB,second\n", ["A", "B"]),
        ("application/xml",
         "<resp><rec><title>A</title></rec><rec><title>B</title></rec>"
         "<rec><title>C</title></rec></resp>", ["A", "B", "C"]),
        ("application/rss+xml",
         "<rss version='2.0'><channel><title>c</title><item><title>A</title>"
         "<link>https://n.example.org/a</link><description>alpha</description></item>"
         "</channel></rss>", ["A"]),
        ("text/html", "<html><head><title>A</title></head><body><p>Hello page</p></body></html>",
         ["A"]),
    ],
)
def test_every_format_ingests_without_mapping(net, ctype, body, expect_titles):
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, headers={"content-type": ctype}, text=body)
    src = _src(items_path="data" if "json" in ctype and "nd" not in ctype else "")
    items = src.fetch()
    assert [i.title for i in items] == expect_titles
    assert all(i.content for i in items)
    assert all(i.raw.get("flattened") for i in items)
    assert src.responded and src.last_sample


def test_xml_with_items_path_and_mapping(net):
    xml = ("<out><meta><n>2</n></meta><list><e id='1'><t>One</t><u>https://x.example.org/1</u>"
           "</e><e id='2'><t>Two</t><u>https://x.example.org/2</u></e></list></out>")
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, headers={"content-type": "application/xml"}, text=xml)
    src = _src(items_path="out.list.e", map_content="t", map_url="u", map_title="@id")
    items = src.fetch()
    assert [(i.content, i.url, i.title) for i in items] == [
        ("One", "https://x.example.org/1", "1"), ("Two", "https://x.example.org/2", "2")]


def test_xml_entities_refused(net):
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
            "<r><x>&b;</x></r>")
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, headers={"content-type": "application/xml"}, text=bomb)
    src = _src()
    assert src.fetch() == []
    assert "could not read" in src.last_error


def test_mapping_still_skips_unmapped_records(net):
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, json={"data": [{"text": "a"}, {"other": 1}]})
    items = _src(items_path="data", map_content="text").fetch()
    assert [i.content for i in items] == ["a"]
    assert not items[0].raw.get("flattened")


def test_fallback_picks_url_date_author():
    src = _src()
    item = src._fallback_item({"headline": "H", "link": "https://n.example.org/1",
                               "created_at": "2026-01-02T03:04:05Z", "user": {"name": "ann"},
                               "nested": {"k": "v"}}, None)
    assert item is not None
    assert item.title == "H" and item.url == "https://n.example.org/1"
    assert item.author == "ann" and item.published_at and item.published_at.year == 2026
    assert "nested.k: v" in item.content


# --------------------------------------------------------------- pagination
def test_page_number_pagination_with_cap(net):
    def page(r):
        n = int(_q(r)["page"])
        return httpx.Response(200, json={"items": [{"title": f"p{n}-{i}"} for i in range(2)]})

    net.routes["https://api.example.org/v1/items"] = page
    src = _src(items_path="items", pagination="page", page_param="page", page_start=1,
               page_size_param="per_page", page_size=2, max_pages=3)
    items = src.fetch()
    assert len(items) == 6
    assert [_q(r)["page"] for r in net.log] == ["1", "2", "3"]
    assert all(_q(r)["per_page"] == "2" for r in net.log)


def test_offset_pagination_stops_on_empty_page(net):
    data = [{"title": f"r{i}"} for i in range(5)]

    def page(r):
        off, lim = int(_q(r)["offset"]), int(_q(r)["limit"])
        return httpx.Response(200, json=data[off:off + lim])

    net.routes["https://api.example.org/v1/items"] = page
    src = _src(pagination="offset", page_param="offset", page_size_param="limit",
               page_size=2, max_pages=10)
    assert [i.title for i in src.fetch()] == [f"r{i}" for i in range(5)]
    assert [_q(r)["offset"] for r in net.log] == ["0", "2", "4", "5"]


def test_cursor_pagination(net):
    pages = {None: ("c1", "a"), "c1": ("c2", "b"), "c2": (None, "c")}

    def page(r):
        nxt, title = pages[_q(r).get("cursor")]
        return httpx.Response(200, json={"data": [{"title": title}], "meta": {"next": nxt}})

    net.routes["https://api.example.org/v1/items"] = page
    src = _src(items_path="data", pagination="cursor", page_param="cursor",
               cursor_path="meta.next", max_pages=10)
    assert [i.title for i in src.fetch()] == ["a", "b", "c"]


def test_next_link_in_body_and_header(net):
    def body_page(r):
        if r.url.params.get("p") == "2":
            return httpx.Response(200, json={"results": [{"title": "second"}], "next": None})
        return httpx.Response(200, json={"results": [{"title": "first"}],
                                         "next": "/v1/items?p=2"})

    net.routes["https://api.example.org/v1/items"] = body_page
    src = _src(items_path="results", pagination="next_link", cursor_path="next", max_pages=5)
    assert [i.title for i in src.fetch()] == ["first", "second"]

    def header_page(r):
        if r.url.path.endswith("/page2"):
            return httpx.Response(200, json=[{"title": "B"}])
        return httpx.Response(200, json=[{"title": "A"}],
                              headers={"Link": '<https://api.example.org/v1/page2>; rel="next"'})

    net.routes.clear()
    net.routes["https://api.example.org/v1/items"] = header_page
    net.routes["https://api.example.org/v1/page2"] = header_page
    src = _src(pagination="next_link", max_pages=5)
    assert [i.title for i in src.fetch()] == ["A", "B"]


def test_next_link_never_leaves_api_host(net, monkeypatch):
    monkeypatch.setattr(C, "read_env_value", lambda k: "s3cret" if k.endswith("_KEY") else None)
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, json={"r": [{"title": "x"}], "next": "https://other.example.net/steal"})
    net.routes["https://other.example.net/steal"] = lambda r: httpx.Response(200, json={})
    src = _src(items_path="r", pagination="next_link", cursor_path="next", max_pages=5,
               auth_type="bearer")
    assert len(src.fetch()) == 1
    assert all(r.url.host == "api.example.org" for r in net.log)


# ---------------------------------------------------------------------- auth
def _secrets(monkeypatch, key=None, headers=None):
    def read(name):
        if name.endswith("_KEY"):
            return key
        if name.endswith("_HEADERS"):
            return json.dumps(headers) if headers else None
        return None

    monkeypatch.setattr(C, "read_env_value", read)


def test_basic_auth_and_secret_headers(net, monkeypatch):
    _secrets(monkeypatch, key="ann:pw", headers={"X-Tenant": "t1", "X-Key": "k2"})
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(200, json=[{"a": 1}])
    src = _src(auth_type="basic", extra_headers='{"Accept-Language": "en"}')
    assert src.is_available()
    src.fetch()
    h = net.log[0].headers
    assert h["authorization"] == "Basic " + base64.b64encode(b"ann:pw").decode()
    assert h["x-tenant"] == "t1" and h["x-key"] == "k2" and h["accept-language"] == "en"


def test_keyed_auth_needs_key(monkeypatch):
    _secrets(monkeypatch, key=None)
    for auth in ("basic", "oauth2", "bearer", "header", "query"):
        assert not _src(auth_type=auth, oauth_token_url="https://auth.example.org/t").is_available()


def test_oauth2_client_credentials_token_cached(net, monkeypatch):
    _secrets(monkeypatch, key="client-secret")
    tokens = []

    def token(r):
        tokens.append(r)
        return httpx.Response(200, json={"access_token": f"tok{len(tokens)}", "expires_in": 3600})

    net.routes["https://auth.example.org/oauth/token"] = token
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200 if r.headers.get("authorization") == "Bearer tok1" else 401, json=[{"title": "ok"}])
    cfg = {"auth_type": "oauth2", "auth_param": "client-id",
               "oauth_token_url": "https://auth.example.org/oauth/token", "oauth_scope": "read"}
    assert [i.title for i in _src(**cfg).fetch()] == ["ok"]
    assert [i.title for i in _src(**cfg).fetch()] == ["ok"]
    assert len(tokens) == 1  # cached across source instances
    form = parse_qs(tokens[0].content.decode())
    assert form["grant_type"] == ["client_credentials"] and form["scope"] == ["read"]
    assert tokens[0].headers["authorization"].startswith("Basic ")


def test_oauth2_refreshes_once_on_401(net, monkeypatch):
    _secrets(monkeypatch, key="client-secret")
    issued = []

    def token(r):
        issued.append(1)
        return httpx.Response(200, json={"access_token": f"tok{len(issued)}"})

    net.routes["https://auth.example.org/oauth/token"] = token
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200 if r.headers.get("authorization") == "Bearer tok2" else 401, json=[{"title": "ok"}])
    items = _src(auth_type="oauth2", auth_param="cid",
                 oauth_token_url="https://auth.example.org/oauth/token").fetch()
    assert [i.title for i in items] == ["ok"] and len(issued) == 2


def test_post_body_template_fills_query(net, monkeypatch):
    from nexus.config import get_settings

    monkeypatch.setattr(type(get_settings()), "investigation_query_targets",
                        lambda self: ['acme "corp"'])
    seen = []

    def search(r):
        seen.append(json.loads(r.content))
        return httpx.Response(200, json={"hits": [{"title": "found"}]})

    net.routes["https://api.example.org/v1/items"] = search
    src = _src(http_method="POST", items_path="hits",
               body_template='{"q": {"match": "{query}"}, "size": 5}')
    assert [i.title for i in src.fetch()] == ["found"]
    assert seen == [{"q": {"match": 'acme "corp"'}, "size": 5}]


# ------------------------------------------------------------------- netguard
def test_private_host_blocked_without_allow_list(net, monkeypatch):
    monkeypatch.delenv("INTERNAL_ALLOWED_HOSTS", raising=False)
    net.routes["https://intranet.corp/api"] = lambda r: httpx.Response(200, json=[{"a": 1}])
    src = _src(base_url="https://intranet.corp", endpoint="/api")
    assert src.fetch() == []
    assert src.last_error.startswith("blocked") and not net.log


def test_private_host_allowed_with_allow_list(net, monkeypatch):
    from nexus.config import get_settings

    monkeypatch.setenv("INTERNAL_ALLOWED_HOSTS", "intranet.corp")
    get_settings.cache_clear()
    net.routes["https://intranet.corp/api"] = lambda r: httpx.Response(200, json=[{"t": "in"}])
    assert len(_src(base_url="https://intranet.corp", endpoint="/api").fetch()) == 1


def test_redirect_to_loopback_is_refused(net):
    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        302, headers={"Location": "http://loop.example.org/admin"})
    src = _src()
    assert src.fetch() == []
    assert "blocked" in src.last_error
    assert all(r.url.host != "loop.example.org" for r in net.log)


def test_tls_mode_passed_to_netguard(monkeypatch):
    seen = {}

    def fake(method, url, **kw):
        seen.update(kw)
        return httpx.Response(200, json=[], request=httpx.Request(method, url))

    monkeypatch.setattr(C, "safe_request", fake)
    _src(tls_mode="insecure").fetch()
    assert seen["verify"] is False


# ------------------------------------------------------- storage / migration
def test_clean_and_store_new_fields(temp_db):
    from nexus.storage import create_custom_source, get_custom_source

    with temp_db() as conn:
        sid = create_custom_source(conn, {
            "name": "x", "base_url": "https://api.example.org", "auth_type": "oauth2",
            "response_format": "csv", "pagination": "cursor", "max_pages": "99",
            "page_size": "-3", "extra_headers": '{"A": "b"}', "tls_mode": "bogus",
            "oauth_token_url": "https://auth.example.org/t",
        })
        row = get_custom_source(conn, sid)
    assert row["auth_type"] == "oauth2" and row["response_format"] == "csv"
    assert row["pagination"] == "cursor" and row["max_pages"] == 20 and row["page_size"] == 0
    assert json.loads(row["extra_headers"]) == {"A": "b"} and row["tls_mode"] == "verify"


def test_migration_adds_columns_to_old_table(tmp_path, monkeypatch):
    import sqlite3

    from nexus.config import get_settings

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE custom_sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
        " enabled INTEGER NOT NULL DEFAULT 1, base_url TEXT NOT NULL, endpoint TEXT NOT NULL "
        "DEFAULT '', http_method TEXT NOT NULL DEFAULT 'GET', auth_type TEXT NOT NULL DEFAULT "
        "'none', auth_param TEXT, query_param TEXT, extra_params TEXT NOT NULL DEFAULT '{}', "
        "items_path TEXT NOT NULL DEFAULT '', map_title TEXT, map_content TEXT, map_url TEXT, "
        "map_author TEXT, map_published TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT "
        "(datetime('now')))")
    conn.execute("INSERT INTO custom_sources (name, base_url) VALUES ('legacy', 'https://a.example.org')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    get_settings.cache_clear()
    from nexus.db import get_connection, init_db
    from nexus.storage import list_custom_sources

    init_db()
    with get_connection() as c:
        row = list_custom_sources(c)[0]
    assert row["name"] == "legacy" and row["response_format"] == "auto"
    assert row["pagination"] == "none" and row["max_pages"] == 1 and row["tls_mode"] == "verify"


def test_secret_headers_env_key_is_writable(monkeypatch, tmp_path):
    from nexus import envstore

    envstore.update_env({envstore.custom_source_headers_env_name(3): '{"X": "y"}',
                         "CUSTOM_SOURCE_X_HEADERS": "nope"})
    assert envstore.read_env_value("CUSTOM_SOURCE_3_HEADERS") == '{"X": "y"}'
    assert envstore.read_env_value("CUSTOM_SOURCE_X_HEADERS") is None
    envstore.update_env({"CUSTOM_SOURCE_3_HEADERS": ""})
    assert envstore.read_env_value("CUSTOM_SOURCE_3_HEADERS") is None


# ------------------------------------------------------------ planner / adapt
class _Provider:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system, user, max_tokens=0):
        self.prompts.append(user)
        return json.dumps(self.reply)


def test_planner_converts_non_json_sample_and_keeps_new_keys(monkeypatch):
    from nexus.analysis import source_planner as P

    prov = _Provider({"name": "Feed", "response_format": "xml", "pagination": "page",
                      "page_param": "p", "max_pages": 3, "extra_headers": {"Accept": "x"},
                      "tls_mode": "insecure", "items_path": "res.row", "map_content": "v"})
    monkeypatch.setattr(P, "get_provider", lambda s: prov)
    out = P.plan_source(sample="<res><row><v>1</v></row><row><v>2</v></row></res>")
    assert out["ok"]
    cfg = out["config"]
    assert cfg["response_format"] == "xml" and cfg["pagination"] == "page"
    assert cfg["extra_headers"] == '{"Accept": "x"}'
    assert "tls_mode" not in cfg  # never taken from the model
    assert "detected format: xml" in prov.prompts[0]
    assert '"row"' in prov.prompts[0]


def test_auto_adapt_learns_from_csv_sample(temp_db, net, monkeypatch):
    from nexus.analysis import source_planner as P
    from nexus.sources import auto_adapt
    from nexus.storage import (
        create_custom_source,
        get_custom_source,
        record_custom_source_health,
    )

    net.routes["https://api.example.org/v1/items"] = lambda r: httpx.Response(
        200, headers={"content-type": "text/csv"}, text="headline,body\nH1,B1\nH2,B2\n")
    with temp_db() as conn:
        sid = create_custom_source(conn, {"name": "csv", "base_url": "https://api.example.org",
                                          "endpoint": "/v1/items", "map_content": "gone"})
        row = get_custom_source(conn, sid)
    src = C.CustomApiSource(row)
    assert src.fetch() == [] and src.responded
    assert json.loads(src.last_sample or "")[0]["headline"] == "H1"
    prov = _Provider({"items_path": "", "map_content": "body", "map_title": "headline"})
    monkeypatch.setattr(P, "get_provider", lambda s: prov)
    monkeypatch.setattr(auto_adapt, "get_provider", lambda s: prov)
    with temp_db() as conn:
        for _ in range(auto_adapt.DRIFT_THRESHOLD):
            record_custom_source_health(conn, sid, responded=True, mapped=0,
                                        sample=src.last_sample)
        assert auto_adapt.maybe_adapt(conn, sid)["adapted"]
        row = get_custom_source(conn, sid)
    healed = C.CustomApiSource(row).fetch()
    assert [(i.title, i.content) for i in healed] == [("H1", "B1"), ("H2", "B2")]


# ------------------------------------------------------------------------ UI
def test_form_and_create_route_store_secrets_in_env_only(temp_db):
    from fastapi.testclient import TestClient

    from nexus import envstore
    from nexus.storage import list_custom_sources
    from nexus.web.app import app

    c = TestClient(app, base_url="http://127.0.0.1")
    page = c.get("/sources/custom")
    assert page.status_code == 200
    for needle in ('name="pagination"', 'name="response_format"', 'value="oauth2"',
                   'name="secret_headers"', 'name="body_template"', 'name="tls_mode"'):
        assert needle in page.text
    r = c.post("/sources/custom", data={
        "name": "Feed", "base_url": "https://api.example.org", "auth_type": "basic",
        "api_key": "ann:pw", "secret_headers": '{"X-Key": "k"}', "pagination": "page",
        "page_param": "page", "max_pages": "3", "response_format": "csv",
    })
    assert r.status_code == 200 and "Feed" in r.text and "secret headers set" in r.text
    with temp_db() as conn:
        row = list_custom_sources(conn)[0]
    assert "ann:pw" not in json.dumps(row) and '"k"' not in json.dumps(row)
    assert envstore.read_env_value(f"CUSTOM_SOURCE_{row['id']}_KEY") == "ann:pw"
    assert json.loads(envstore.read_env_value(f"CUSTOM_SOURCE_{row['id']}_HEADERS") or "") == {
        "X-Key": "k"}
    bad = c.post(f"/sources/custom/{row['id']}/update", data={
        "name": "Feed", "base_url": "https://api.example.org", "secret_headers": "not json"})
    assert "Secret headers must be a JSON object" in bad.text
    c.post(f"/sources/custom/{row['id']}/delete")
    assert envstore.read_env_value(f"CUSTOM_SOURCE_{row['id']}_HEADERS") is None
