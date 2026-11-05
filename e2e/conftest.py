"""Browser tests against a real server, a throwaway data folder and demo data.

Run with ``python -m pytest e2e -q`` (they are not part of the normal suite; see
``testpaths`` in pyproject.toml). They need Playwright's Chromium:
``python -m playwright install chromium``. Edge is used when it is missing.

Sherlock's model is never reached: AI is on, so the chat is usable, but the only
provider is an Ollama address nothing listens on, and tests answer ``/assistant/ask``
themselves. Every test fails on a CSP violation or an uncaught page error.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

WATCH_CSP = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__csp.push(e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline'));
}, true);
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(port: int) -> None:
    import uvicorn

    from nexus.web.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture(scope="session")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    work: Path = tmp_path_factory.mktemp("nexus-e2e")
    env_file = work / "e2e.env"
    env_file.write_text("", encoding="utf-8")
    os.environ.update({
        "DATA_DIR": str(work), "DATABASE_PATH": str(work / "nexus.db"),
        "NEXUS_ENV_FILE": str(env_file), "AI_PROVIDER": "ollama",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9",
    })
    port = _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        pytest.fail("the server did not start")
    loaded = httpx.post(f"{base}/demo/load", headers={"Origin": base}, timeout=60)
    assert loaded.status_code < 400, loaded.text
    return base


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception:  # Playwright's Chromium not installed: use the system Edge
            instance = p.chromium.launch(channel="msedge")
        yield instance
        instance.close()


@pytest.fixture
def page(browser: Browser, base_url: str) -> Iterator[Page]:
    context = browser.new_context(base_url=base_url)
    context.add_init_script(WATCH_CSP)
    tab = context.new_page()
    tab.set_default_timeout(10_000)
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    yield tab
    violations = tab.evaluate("window.__csp || []")
    context.close()
    assert not violations, violations
    assert not errors, errors
