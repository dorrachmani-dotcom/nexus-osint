# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the packaged **Linux** build of Nexus-OSINT.

Unlike the Windows spec (which produces a one-folder app later wrapped by Inno
Setup), this produces a single self-contained ELF executable — the closest Linux
equivalent of handing someone one ``Nexus-Setup.exe``. The recipient downloads
one file, marks it executable, and runs it. No Python, no install step.

Build it ON a Linux machine (PyInstaller does not cross-compile from Windows)::

    bash installer/linux/build_linux.sh

or directly::

    pyinstaller installer/nexus_linux.spec --noconfirm \
        --distpath installer/dist-linux --workpath installer/build-linux

Environment switches (same names as the Windows spec):
  * NEXUS_BUNDLE_CHROMIUM=1 -> embed a Linux Playwright Chromium headless-shell
                               for offline evidence screenshots. Point
                               NEXUS_CHROMIUM_SRC at an ms-playwright dir holding
                               a Linux ``chromium_headless_shell-*`` build.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH points at this file's dir (installer/); repo root is one level up.
REPO = Path(SPECPATH).resolve().parent

# ---------------------------------------------------------------- data files
datas = [
    (str(REPO / "nexus" / "web" / "templates"), "nexus/web/templates"),
    (str(REPO / "nexus" / "web" / "static"), "nexus/web/static"),
    (str(REPO / ".env.example"), "."),
]

# --------------------------------------------------- optional bundled Chromium
# Embed a *Linux* Playwright Chromium so evidence screenshots work offline. The
# build script runs `playwright install chromium-headless-shell` and points
# NEXUS_CHROMIUM_SRC at the resulting ms-playwright directory.
if os.environ.get("NEXUS_BUNDLE_CHROMIUM") == "1":
    src = os.environ.get("NEXUS_CHROMIUM_SRC") or str(
        Path(os.environ.get("HOME", "")) / ".cache" / "ms-playwright"
    )
    src_path = Path(src) if src else None
    if not (src_path and src_path.is_dir()):
        raise SystemExit(
            f"NEXUS_BUNDLE_CHROMIUM=1 but no Chromium folder at {src!r}. "
            "Run `playwright install chromium-headless-shell` or set "
            "NEXUS_CHROMIUM_SRC."
        )

    # Bundle only the newest chrome-headless-shell, never the whole tree (which
    # may also hold full Chromium / ffmpeg / older builds if NEXUS_CHROMIUM_SRC
    # points at a shared ms-playwright). evidence.py only needs the headless
    # shell and prefers it. The build script already installs just this into a
    # clean dir, so the normal build is unaffected; this only guards a manual
    # build against an over-full source dir. Sub-folder names are preserved so
    # evidence.py's glob (chromium_headless_shell-*/chrome-headless-shell-*/...)
    # still resolves. (No winldd here — that helper is Windows-only.)
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

    links = src_path / ".links"
    if links.is_file():
        datas.append((str(links), "ms-playwright"))

# ------------------------------------------------------------ hidden imports
hiddenimports = collect_submodules("uvicorn")
binaries = []

# The PDF export backend (xhtml2pdf -> reportlab) loads bundled fonts/data and
# resolves submodules dynamically; collect_all so real PDFs render offline.
for _pkg in ("reportlab", "xhtml2pdf", "html5lib", "svglib", "arabic_reshaper"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# ------------------------------------------------------------------ excludes
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

# One-file build: bundle everything into a single executable named "nexus".
# It self-extracts to a temp dir on each launch (first start is a little slower
# in exchange for being a single portable file, matching the Windows deliverable).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="nexus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
