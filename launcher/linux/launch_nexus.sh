#!/usr/bin/env bash
# Nexus-OSINT desktop launcher (Linux).
#
# Behaves like a native app:
#   1. Starts the local server (in the background) if it isn't already up.
#      The server's Boot Sync re-runs your latest topic queries on every fresh
#      start, so reopening always shows current results.
#   2. Opens a chromeless "app window" (Chrome/Chromium/Edge in --app mode) —
#      no address bar, no tabs, so it doesn't look like a web page.
#   3. When you close that window, the server we started is shut down.
#
# Nothing here ever touches the network beyond 127.0.0.1, and it starts no
# server if one is already running (so a second launch just opens a window).

set -u

# launcher/linux/ is two levels under the repo root.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
URL="http://127.0.0.1:8000"
HEALTH="$URL/health"
DATADIR="$ROOT/data"
PROFILE="$DATADIR/app-profile-linux"   # isolated browser profile for app mode
PYBIN="$ROOT/.venv/bin/python"

mkdir -p "$DATADIR"

server_up() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 2 -O /dev/null "$HEALTH" >/dev/null 2>&1
    else
        # No HTTP client to probe with: assume not up so we try to start it.
        return 1
    fi
}

find_browser() {
    for b in google-chrome google-chrome-stable chromium chromium-browser \
             microsoft-edge microsoft-edge-stable brave-browser; do
        if command -v "$b" >/dev/null 2>&1; then echo "$b"; return 0; fi
    done
    return 1
}

# --- 1. Start the server if needed -----------------------------------------
STARTED_BY_US=0
SERVER_PID=""
if ! server_up; then
    if [ ! -x "$PYBIN" ]; then
        # No virtual-env: fall back to opening whatever may be running.
        if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
        exit 0
    fi
    ( cd "$ROOT" && nohup "$PYBIN" -m uvicorn nexus.web.app:app \
        --host 127.0.0.1 --port 8000 \
        >"$DATADIR/nexus-launcher.log" 2>"$DATADIR/nexus-launcher.err" & echo $! ) \
        > "$DATADIR/.launcher-pid"
    SERVER_PID="$(cat "$DATADIR/.launcher-pid" 2>/dev/null)"
    STARTED_BY_US=1

    # Wait for the server to answer (up to ~30s on a cold first start).
    for _ in $(seq 1 40); do
        if server_up; then break; fi
        sleep 0.75
    done
fi

# --- 2. Open the chromeless app window --------------------------------------
BROWSER="$(find_browser || true)"
if [ -n "$BROWSER" ]; then
    "$BROWSER" --app="$URL" --user-data-dir="$PROFILE" \
        --window-size=1440,900 --no-first-run --no-default-browser-check \
        >/dev/null 2>&1
    # Control returns here when the app window is closed.
else
    # No Chromium-family browser: open in the default browser (a normal tab)
    # and leave the server running (we can't detect the tab closing).
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
    exit 0
fi

# --- 3. When the app window closes, stop the server we started --------------
if [ "$STARTED_BY_US" = "1" ] && [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
fi
rm -f "$DATADIR/.launcher-pid" 2>/dev/null || true
