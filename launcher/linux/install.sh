#!/usr/bin/env bash
# Nexus-OSINT — one-time Linux setup (full install).
#
# Run once on a fresh machine:   bash launcher/linux/install.sh
# It will:
#   1. Find Python 3.
#   2. Create the project's virtual environment (.venv) if needed.
#   3. Install all Python dependencies.
#   4. Generate the app icon.
#   5. Add a "Nexus OSINT" launcher to your applications menu and Desktop.
#
# After this, click the "Nexus OSINT" icon (menu or Desktop) to use the app.
# Safe to run again: it skips steps that are already done.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
ICON="$ROOT/launcher/nexus.png"
LAUNCH="$HERE/launch_nexus.sh"

step() { printf '\n[%s] %s\n' "$1" "$2"; }

# --- 1. Find Python 3 ------------------------------------------------------
step 1 "Looking for Python 3..."
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
            PY="$c"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo ""
    echo "  Python 3.10+ was not found."
    echo "  Install it with your package manager, e.g.:"
    echo "    Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip"
    echo "    Fedora:         sudo dnf install python3 python3-pip"
    echo "    Arch:           sudo pacman -S python python-pip"
    echo "  Then run this again."
    exit 1
fi
echo "  Found: $("$PY" --version 2>&1)"

# --- 2. Create the virtual environment -------------------------------------
if [ -x "$VENV_PY" ]; then
    step 2 "Virtual environment already exists — skipping."
else
    step 2 "Creating virtual environment (.venv)..."
    "$PY" -m venv "$VENV"
fi

# --- 3. Install dependencies -----------------------------------------------
step 3 "Installing dependencies (this can take a few minutes)..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r "$ROOT/requirements.txt"

# --- 4. Generate the icon --------------------------------------------------
step 4 "Generating app icon..."
"$VENV_PY" "$ROOT/launcher/make_icon.py" || \
    echo "  (icon generation skipped — a committed icon will be used)"

# --- 5. Create the launcher entry ------------------------------------------
step 5 "Creating the application launcher..."
chmod +x "$LAUNCH"

DESKTOP_ENTRY="$(cat <<EOF
[Desktop Entry]
Type=Application
Name=Nexus OSINT
Comment=Open Nexus-OSINT
Exec=$LAUNCH
Icon=$ICON
Terminal=false
Categories=Utility;Network;
EOF
)"

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS_DIR"
printf '%s\n' "$DESKTOP_ENTRY" > "$APPS_DIR/nexus-osint.desktop"
chmod +x "$APPS_DIR/nexus-osint.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

# Also drop a copy on the Desktop if one exists.
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
    printf '%s\n' "$DESKTOP_ENTRY" > "$DESKTOP_DIR/nexus-osint.desktop"
    chmod +x "$DESKTOP_DIR/nexus-osint.desktop"
    # GNOME requires the file to be marked trusted before it shows the icon.
    command -v gio >/dev/null 2>&1 && \
        gio set "$DESKTOP_DIR/nexus-osint.desktop" metadata::trusted true 2>/dev/null || true
fi

echo ""
echo "  All set!  Find 'Nexus OSINT' in your applications menu (and on the"
echo "  Desktop). Click it any time to open the app."
echo ""
