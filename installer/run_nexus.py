"""Frozen-app entry point for the packaged Windows build.

This is what the bundled ``Nexus.exe`` runs. Unlike the developer launcher
(which assumes a checked-out repo + a .venv), this file is the application: it
configures a user-writable home, starts the local server in-process, and opens a
chromeless window — so a non-technical recipient just double-clicks and it works,
fully offline, with no Python install.

What it sets up before importing the app (order matters — the settings loader
reads these on first import):

  * NEXUS_HOME     -> %LOCALAPPDATA%\\Nexus            (writable app data root)
  * DATA_DIR       -> %NEXUS_HOME%\\data               (SQLite, evidence, exports)
  * DATABASE_PATH  -> %NEXUS_HOME%\\data\\nexus.db
  * NEXUS_ENV_FILE -> %NEXUS_HOME%\\.env               (BYOK keys live here, OpSec)
  * PLAYWRIGHT_BROWSERS_PATH -> bundled Chromium        (evidence screenshots offline)

Everything stays on 127.0.0.1. The bundle directory itself is treated as
read-only; all state the user creates goes under NEXUS_HOME.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"
HEALTH = f"{URL}/health"


def _bundle_dir() -> Path:
    """Directory holding bundled data files (templates, Chromium, .env.example).

    PyInstaller extracts/refers to data files relative to ``sys._MEIPASS``. When
    running from source (not frozen) we fall back to the repo root so this script
    can be smoke-tested with a plain ``python installer/run_nexus.py``.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _app_home() -> Path:
    """User-writable application home.

    Windows: %LOCALAPPDATA%\\Nexus.  Linux/macOS: $XDG_DATA_HOME/Nexus or
    ~/.local/share/Nexus.  Falls back to ~/Nexus if nothing else is set.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    if not base:
        base = str(Path.home())
    home = Path(base) / "Nexus"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _configure_environment() -> Path:
    """Point the app at a writable home and the bundled Chromium. Returns home."""
    home = _app_home()
    data_dir = home / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("NEXUS_HOME", str(home))
    # These override the defaults baked into nexus.config / nexus.envstore.
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DATABASE_PATH"] = str(data_dir / "nexus.db")
    os.environ["NEXUS_ENV_FILE"] = str(home / ".env")

    # First run: seed a .env from the bundled template so the Settings page has a
    # file to edit. Never overwrite an existing one (it holds the user's keys).
    env_path = home / ".env"
    if not env_path.exists():
        template = _bundle_dir() / ".env.example"
        try:
            if template.exists():
                shutil.copyfile(template, env_path)
            else:
                env_path.write_text("", encoding="utf-8")
        except OSError:
            pass

    # Bundled Playwright Chromium for evidence screenshots (offline). Only set if
    # the browser folder was actually shipped; otherwise evidence degrades to a
    # graceful "no screenshot" rather than erroring.
    browsers = _bundle_dir() / "ms-playwright"
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)

    return home


def _server_already_up() -> bool:
    """True only if a *Nexus* server is already answering on the port.

    A bare socket-open check gave false positives: another local service on the
    same port, or a half-dead prior instance, would look "up" and we'd open a
    window onto a dead/foreign endpoint (ERR_CONNECTION_REFUSED). Confirming
    /health returns 200 makes this reliable.
    """
    import urllib.request

    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            pass
    except OSError:
        return False
    try:
        with urllib.request.urlopen(HEALTH, timeout=2) as resp:  # noqa: S310 (loopback)
            return resp.status == 200
    except Exception:
        return False


def _wait_for_health(timeout_s: float = 120.0) -> bool:
    """Poll /health until the server answers or we give up.

    The timeout is generous (120s) on purpose: on the very first launch after
    install, Windows Defender real-time scanning reads the whole freshly
    unpacked bundle while Python imports it, which can push first-boot well past
    a tighter limit. Subsequent launches are fast (already scanned). Waiting here
    is what prevents the window from opening onto a not-yet-ready server.
    """
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=2) as resp:  # noqa: S310 (loopback only)
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _run_server() -> None:
    """Run uvicorn in-process (called on a daemon thread)."""
    # Make sure the app package is importable. When frozen, PyInstaller wires
    # this up; when run from source, the script dir (installer/) is on sys.path
    # instead of the repo root, so add the bundle/repo root explicitly.
    root = str(_bundle_dir())
    if root not in sys.path:
        sys.path.insert(0, root)

    import uvicorn

    from nexus.web.app import app

    # log_config=None keeps uvicorn from reconfiguring logging under the frozen
    # app (where it has no console); the app configures its own logging.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", log_config=None)


def _find_browser() -> str | None:
    """Locate a Chromium-family browser for a chromeless --app window.

    Returns the executable path, or None if none found (caller then falls back
    to the OS default browser). Covers Windows (Edge/Chrome) and Linux
    (chromium/chrome/brave/edge under any name on PATH).
    """
    if sys.platform.startswith("win"):
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates = [
            Path(pf) / "Microsoft/Edge/Application/msedge.exe",
            Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
            Path(pf) / "Google/Chrome/Application/chrome.exe",
            Path(pf86) / "Google/Chrome/Application/chrome.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None
    # Linux / macOS: probe PATH for common Chromium-family launchers.
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


def _open_window(home: Path) -> "subprocess.Popen | None":
    """Open the dashboard in a chromeless app window, or a normal browser tab.

    Returns the browser process handle when an --app window was launched, so the
    caller can shut the server down when the user closes it. Returns None when we
    fell back to the default browser (we then leave the server running).
    """
    import subprocess

    browser = _find_browser()
    if browser:
        profile = home / "app-profile"
        args = [
            browser,
            f"--app={URL}",
            f"--user-data-dir={profile}",
            "--window-size=1440,900",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        try:
            return subprocess.Popen(args)
        except OSError:
            pass
    # No Chromium browser found (or launch failed): open the default browser.
    try:
        webbrowser.open(URL)
    except Exception:
        pass
    return None


def main() -> int:
    home = _configure_environment()

    # Headless mode (smoke tests / running as a hidden service): start the server
    # and block, but never open a window.
    headless = bool(os.environ.get("NEXUS_NO_WINDOW"))

    if _server_already_up():
        # A second double-click just opens another window onto the running app.
        if not headless:
            _open_window(home)
        return 0

    server = threading.Thread(target=_run_server, daemon=True, name="nexus-server")
    server.start()

    if headless:
        if not _wait_for_health():
            return 1
        try:
            while server.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    if not _wait_for_health():
        # Server never came up; still try to show *something* so the user isn't
        # left with a silent failure.
        _open_window(home)
        time.sleep(3)
        return 1

    proc = _open_window(home)
    if proc is not None:
        # Tie the app's lifetime to the window: when the user closes it, stop.
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return 0

    # Opened in the default browser instead — keep the server alive until the
    # process is terminated (Task Manager / shortcut close).
    try:
        while server.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
