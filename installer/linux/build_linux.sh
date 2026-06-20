#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Build a single self-contained Linux executable for Nexus-OSINT.
#
# This is the Linux counterpart of the Windows Nexus-Setup.exe: it produces ONE
# file (installer/dist-linux/nexus) that a recipient can download, mark
# executable, and run — no Python, no pip, no install step, fully offline.
#
# PyInstaller cannot cross-compile, so this MUST run on a Linux machine (the
# same CPU arch as your target — typically x86_64). On Windows, run it inside
# WSL or a Linux Docker container.
#
# Usage (from the repo root):
#     bash installer/linux/build_linux.sh
#
# Result:
#     installer/dist-linux/nexus           <- hand this single file to anyone
#
# Requirements on the build machine: python3.10+, python3-venv, and an internet
# connection for this build step only (to fetch pip deps + the Linux browser).
# The produced binary itself needs no internet.
# ----------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

BUILD_VENV="$REPO/.venv-build-linux"
DIST="$REPO/installer/dist-linux"
WORK="$REPO/installer/build-linux"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

# --- 1. Toolchain ----------------------------------------------------------
say "Checking for Python 3.10+"
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "  Python 3.10+ not found. Install it, e.g.:"
    echo "    Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip"
    echo "    Fedora:        sudo dnf install python3 python3-pip"
    exit 1
fi
echo "  Using: $("$PY" --version 2>&1)"

# --- 2. Isolated build venv ------------------------------------------------
say "Creating build virtual environment"
"$PY" -m venv "$BUILD_VENV"
VPY="$BUILD_VENV/bin/python"
"$VPY" -m pip install --upgrade pip >/dev/null

say "Installing app dependencies (this can take a few minutes)"
"$VPY" -m pip install -r "$REPO/requirements.txt"
"$VPY" -m pip install pyinstaller

# --- 3. Linux Chromium for offline evidence screenshots --------------------
# Download just the headless shell (smallest browser that evidence.py uses) into
# a local ms-playwright dir, then bundle it.
say "Fetching Linux Chromium headless-shell for offline evidence"
CHROMIUM_DIR="$REPO/installer/_chromium_src_linux"
mkdir -p "$CHROMIUM_DIR"
if PLAYWRIGHT_BROWSERS_PATH="$CHROMIUM_DIR" "$VPY" -m playwright install chromium-headless-shell; then
    export NEXUS_BUNDLE_CHROMIUM=1
    export NEXUS_CHROMIUM_SRC="$CHROMIUM_DIR"
    echo "  Chromium ready -> $CHROMIUM_DIR"
else
    echo "  WARNING: Chromium download failed; building WITHOUT bundled browser."
    echo "  The app still runs; evidence screenshots will be disabled offline."
    unset NEXUS_BUNDLE_CHROMIUM || true
fi

# --- 4. Build the single-file binary ---------------------------------------
say "Running PyInstaller (one-file build)"
rm -rf "$DIST" "$WORK"
"$VPY" -m PyInstaller "$REPO/installer/nexus_linux.spec" --noconfirm \
    --distpath "$DIST" --workpath "$WORK"

BIN="$DIST/nexus"
if [ -x "$BIN" ]; then
    chmod +x "$BIN"
    SIZE="$(du -h "$BIN" | cut -f1)"
    say "Done!"
    echo "  Single-file Linux app:  $BIN  ($SIZE)"
    echo ""
    echo "  Hand that one file to anyone. They run it with:"
    echo "      chmod +x nexus && ./nexus"
    echo "  It opens the dashboard in their browser, fully local (127.0.0.1)."
else
    echo "  Build finished but $BIN was not produced — check the log above." >&2
    exit 1
fi
