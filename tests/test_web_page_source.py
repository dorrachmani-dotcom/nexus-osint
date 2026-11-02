"""Watched web pages / docs sites: crawl limits, robots.txt, change detection.

``safe_request`` is replaced with an in-memory fake site, so there is no
network and every requested URL is recorded.
"""

from __future__ import annotations

import httpx
import pytest

from nexus.sources import webpage as W
from nexus.storage.web_sources import create_web_source, get_web_source


class FakeSite:
    def __init__(self, pages: dict[str, tuple[int, str, str]]) -> None:
        self.pages = pages  # url -> (status, content-type, body)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, method, url, **kwargs):
        self.requests.append((url, kwargs))
        status, ctype, body = self.pages.get(url, (404, "text/plain", "not found"))
        return httpx.Response(status, headers={"content-type": ctype}, text=body,
                              request=httpx.Request(method, url))


def _html(title: str, body: str, links: tuple[str, ...] = ()) -> str:
    anchors = "".join(f"<a href='{h}'>{h}</a>" for h in links)
    return f"<html><head><title>{title}</title></head><body><p>{body}</p>{anchors}</body></html>"


@pytest.fixture
def site(monkeypatch):
    s = FakeSite({})
    monkeypatch.setattr(W, "safe_request", s)
    return s


def _source(temp_db, **cfg) -> W.WebPageSource:
    base = {"url": "https://docs.example.org/guide/", "name": "Docs"}
    base.update(cfg)
    with temp_db() as conn:
        sid = create_web_source(conn, base)
        row = get_web_source(conn, sid)
    return W.WebPageSource(row, sleep=lambda s: None)


def _scan(src: W.WebPageSource):
    from nexus.collector import Collector

    items = src.fetch()
    Collector()._persist(src.name, items)
    src.commit_state()
    return items


def test_new_then_unchanged_then_changed(temp_db, site):
    url = "https://docs.example.org/guide/"
    site.pages[url] = (200, "text/html", _html("Guide", "Step one.\nStep two."))
    src = _source(temp_db)
    first = _scan(src)
    assert len(first) == 1 and first[0].raw["change"] == "new"
    assert first[0].title == "Guide" and "Step one." in first[0].content

    src = _source_reload(temp_db, src)
    assert _scan(src) == []  # unchanged

    site.pages[url] = (200, "text/html", _html("Guide", "Step one.<br>Step three."))
    src = _source_reload(temp_db, src)
    changed = _scan(src)
    assert len(changed) == 1 and changed[0].title == "Guide — updated"
    assert "+ Step three." in changed[0].content
    assert changed[0].raw["change"] == "changed"


def _source_reload(temp_db, src):
    with temp_db() as conn:
        row = get_web_source(conn, src.id)
    return W.WebPageSource(row, sleep=lambda s: None)


def test_state_not_committed_until_commit(temp_db, site):
    url = "https://docs.example.org/guide/"
    site.pages[url] = (200, "text/html", _html("Guide", "Hello"))
    src = _source(temp_db)
    assert src.fetch()  # no commit
    src2 = _source_reload(temp_db, src)
    assert src2.fetch()  # still "new": state was never committed


def test_crawl_same_site_under_path_with_limits(temp_db, site):
    root = "https://docs.example.org/guide/"
    site.pages.update({
        root: (200, "text/html", _html("Home", "root text", (
            "a.html", "b.html", "/other/x.html", "https://elsewhere.example/", "style.css",
            "a.html#frag"))),
        root + "a.html": (200, "text/html", _html("A", "page a", ("c.html",))),
        root + "b.html": (200, "text/html", _html("B", "page b")),
        root + "c.html": (200, "text/html", _html("C", "page c")),
    })
    src = _source(temp_db, crawl="1", max_depth="1", max_pages="10")
    items = src.fetch()
    titles = {i.title for i in items}
    assert titles == {"Home", "A", "B"}  # c.html is depth 2
    requested = {u for u, _ in site.requests}
    assert "https://elsewhere.example/" not in requested
    assert "https://docs.example.org/other/x.html" not in requested
    assert not any(u.endswith(".css") for u in requested)
    # robots.txt only from the target host
    robots = {u for u in requested if u.endswith("robots.txt")}
    assert robots == {"https://docs.example.org/robots.txt"}


def test_max_pages_cap(temp_db, site):
    root = "https://docs.example.org/guide/"
    links = tuple(f"p{i}.html" for i in range(10))
    site.pages[root] = (200, "text/html", _html("Home", "x", links))
    for h in links:
        site.pages[root + h] = (200, "text/html", _html(h, h))
    src = _source(temp_db, crawl="1", max_depth="2", max_pages="3")
    assert len(src.fetch()) == 3


def test_robots_disallow_respected(temp_db, site):
    root = "https://docs.example.org/guide/"
    site.pages.update({
        "https://docs.example.org/robots.txt": (200, "text/plain",
                                                "User-agent: *\nDisallow: /guide/private/\n"),
        root: (200, "text/html", _html("Home", "x", ("private/secret.html", "open.html"))),
        root + "private/secret.html": (200, "text/html", _html("Secret", "s")),
        root + "open.html": (200, "text/html", _html("Open", "o")),
    })
    src = _source(temp_db, crawl="1", max_depth="1")
    titles = {i.title for i in src.fetch()}
    assert titles == {"Home", "Open"}
    assert "skipped by robots.txt" in src.status


def test_linked_document_is_parsed(temp_db, site):
    url = "https://docs.example.org/guide/data.json"
    site.pages[url] = (200, "application/json", '[{"title": "Row one", "v": 1}, {"title": "Row two"}]')
    src = _source(temp_db, url=url)
    items = src.fetch()
    assert items and "Row one" in items[0].content


def test_auth_secret_from_env_and_tls_mode(temp_db, site):
    from nexus.envstore import update_env, web_source_env_name

    url = "https://docs.example.org/guide/"
    site.pages[url] = (200, "text/html", _html("Guide", "x"))
    src = _source(temp_db, auth_type="basic", tls_mode="insecure")
    assert not src.is_available()  # secret missing
    update_env({web_source_env_name(src.id): "alice:pw"})
    src = _source_reload(temp_db, src)
    assert src.is_available()
    src.fetch()
    page_call = next(kw for u, kw in site.requests if u == url)
    assert page_call["headers"]["Authorization"] == "Basic YWxpY2U6cHc="
    assert page_call["verify"] is False


def test_blocked_url_degrades(temp_db, monkeypatch):
    def refuse(*a, **k):
        raise ValueError("refusing non-public host")

    monkeypatch.setattr(W, "safe_request", refuse)
    src = _source(temp_db)
    assert src.fetch() == []
    assert "blocked" in src.status


def test_diff_summary_counts():
    s = W.diff_summary("a\nb\nc", "a\nc\nd\ne")
    assert s.startswith("2 line(s) added, 1 line(s) removed.")
    assert "+ d" in s and "- b" in s


def test_collector_includes_and_commits(temp_db, site, monkeypatch):
    from nexus.collector import Collector

    url = "https://docs.example.org/guide/"
    site.pages[url] = (200, "text/html", _html("Guide", "content"))
    with temp_db() as conn:
        create_web_source(conn, {"url": url, "name": "Docs"})
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    c = Collector()
    names = [s.name for s in c.available_sources()]
    assert "Docs" in names
    stats = c.scan()
    assert stats["Docs"]["new"] == 1
    with temp_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0] == 1


# --------------------------------------------------------------------- web UI
def test_web_sources_page_and_create(temp_db, site):
    from fastapi.testclient import TestClient

    from nexus.web.app import app

    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/sources/web").status_code == 200
    r = c.post("/sources/web", data={"url": "https://docs.example.org/guide/", "crawl": "1",
                                     "max_depth": "9", "max_pages": "999",
                                     "auth_type": "bearer", "secret": "tok"})
    assert r.status_code == 200 and "docs.example.org" in r.text
    with temp_db() as conn:
        row = conn.execute("SELECT * FROM web_sources").fetchone()
        assert row["max_depth"] == 3 and row["max_pages"] == 200
    from nexus.envstore import read_env_value

    assert read_env_value(f"WEB_SOURCE_{row['id']}_SECRET") == "tok"
    bad = c.post("/sources/web", data={"url": "file:///etc/passwd"})
    assert "full http" in bad.text
