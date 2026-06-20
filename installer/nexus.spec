# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the packaged Windows build of Nexus-OSINT.

Produces a one-folder app (``dist/Nexus/Nexus.exe`` + ``_internal/``). The folder
is later wrapped by Inno Setup into a single ``Nexus-Setup.exe`` installer, but it
also runs as-is (portable) if you just copy the folder.

Build (from the repo root)::

    pyinstaller installer/nexus.spec --noconfirm

Optional environment switches:
  * NEXUS_DEBUG_CONSOLE=1   -> keep a console window (shows tracebacks while testing)
  * NEXUS_BUNDLE_CHROMIUM=1 -> embed a Playwright Chromium folder for offline
                               evidence screenshots. Point NEXUS_CHROMIUM_SRC at a
                               ms-playwright directory (defaults to the dev machine's
                               %USERPROFILE%\\AppData\\Local\\ms-playwright).
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller and points at this file's directory
# (installer/). The repo root is one level up.
REPO = Path(SPECPATH).resolve().parent

# ---------------------------------------------------------------- data files
# Jinja templates are loaded by path at runtime, so they must ship as data.
# (There is no static/ dir — CSS/JS come from CDNs.) The .env.example seeds a
# fresh, user-writable .env on first run.
datas = [
    (str(REPO / "nexus" / "web" / "templates"), "nexus/web/templates"),
    (str(REPO / ".env.example"), "."),
]

# --------------------------------------------------- optional bundled Chromium
# Evidence screenshots use Playwright's Chromium. Bundling it makes the installer
# work fully offline (the scoped "Core + evidence" deliverable). It is large
# (~150-300 MB), so it is opt-in at build time.
if os.environ.get("NEXUS_BUNDLE_CHROMIUM") == "1":
    src = os.environ.get("NEXUS_CHROMIUM_SRC") or str(
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    )
    src_path = Path(src) if src else None
    if not (src_path and src_path.is_dir()):
        raise SystemExit(
            f"NEXUS_BUNDLE_CHROMIUM=1 but no Chromium folder at {src!r}. "
            "Run `playwright install chromium-headless-shell` or set NEXUS_CHROMIUM_SRC."
        )

    # Bundle a LEAN browser set, not the whole ms-playwright tree (which can be
    # ~1.4 GB: two full Chromium builds + two headless shells + ffmpeg). Evidence
    # screenshots only need chrome-headless-shell, and evidence.py prefers it, so
    # we ship just the newest headless-shell (+ the tiny Windows DLL-resolver).
    # This is roughly an 80% size cut versus embedding the entire folder.
    # PLAYWRIGHT_BROWSERS_PATH (set in run_nexus.py) points at this ms-playwright/
    # root; the original sub-folder names are preserved so evidence.py's glob
    # (chromium_headless_shell-*/chrome-headless-shell-*/...) still resolves.
    def _newest(pattern):
        matches = sorted(src_path.glob(pattern), key=lambda p: p.name)
        return matches[-1] if matches else None

    shell = _newest("chromium_headless_shell-*")
    if shell is None:
        raise SystemExit(
            "NEXUS_BUNDLE_CHROMIUM=1 but found no chrome-headless-shell under "
            f"{src!r}. Run `playwright install chromium-headless-shell`."
        )
    datas.append((str(shell), f"ms-playwright/{shell.name}"))

    # winldd lets Chromium resolve its DLLs on Windows; ~250 KB, keep newest.
    winldd = _newest("winldd-*")
    if winldd is not None:
        datas.append((str(winldd), f"ms-playwright/{winldd.name}"))

    # Playwright's bookkeeping marker; tiny, harmless, keeps the layout intact.
    links = src_path / ".links"
    if links.is_file():
        datas.append((str(links), "ms-playwright"))

# ------------------------------------------------------------ hidden imports
# uvicorn loads its protocol/loop implementations dynamically; collect them so
# the frozen server can actually serve. Provider SDKs (anthropic/google/openai)
# and the rest of nexus are discovered by static analysis; absent optional ones
# degrade gracefully at runtime.
hiddenimports = collect_submodules("uvicorn")
binaries = []

# The PDF export backend (xhtml2pdf -> reportlab) loads bundled fonts and other
# data files at render time and resolves several submodules dynamically. Without
# these, CreatePDF() fails inside the frozen app and export silently degrades to
# HTML. collect_all pulls each package's data, binaries, and submodules so real
# PDFs are produced offline.
for _pkg in ("reportlab", "xhtml2pdf", "html5lib", "svglib", "arabic_reshaper"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# ------------------------------------------------------------------ excludes
# Trim weight and avoid native-lib pitfalls. weasyprint needs GTK DLLs we don't
# ship — the app already falls back to xhtml2pdf on Windows for PDF export.
excludes = [
    "weasyprint",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "matplotlib",
    "pytest",
    "_pytest",
    "IPython",
    "notebook",
]

console = os.environ.get("NEXUS_DEBUG_CONSOLE") == "1"
icon = str(REPO / "launcher" / "nexus.ico")

a = Analysis(
    [str(REPO / "installer" / "run_nexus.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Nexus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Nexus",
)
