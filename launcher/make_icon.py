"""Generate the Nexus launcher icons.

Pure-Pillow, no network. Draws a dark rounded tile with an amber "nexus"
network motif (a central node linked to satellite nodes) that matches the
app's amber-on-slate theme, and writes:

  * launcher/nexus.ico  — multi-resolution Windows icon
  * launcher/nexus.png  — 256px PNG for Linux .desktop entries

Both files are committed alongside this script; regenerate with:
    # Windows
    .venv\\Scripts\\python.exe launcher\\make_icon.py
    # Linux/macOS
    .venv/bin/python launcher/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# Theme colours (match the dashboard): slate-950 background, amber-500 accent.
BG = (15, 23, 42, 255)        # #0f172a
BG_EDGE = (30, 41, 59, 255)   # #1e293b — subtle border
AMBER = (245, 158, 11, 255)   # #f59e0b
AMBER_DIM = (180, 120, 12, 255)
NODE_CORE = (251, 191, 36, 255)  # #fbbf24

SIZE = 256  # master canvas; downscaled into the .ico


def _rounded_tile(d: ImageDraw.ImageDraw, s: int) -> None:
    r = int(s * 0.18)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG, outline=BG_EDGE,
                        width=max(2, s // 64))


def _node(d: ImageDraw.ImageDraw, x: float, y: float, rad: float, fill) -> None:
    d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=fill)


def render(s: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _rounded_tile(d, s)

    cx, cy = s / 2, s / 2
    orbit = s * 0.30
    core_r = s * 0.085
    sat_r = s * 0.055
    line_w = max(2, int(s * 0.018))

    # Four satellite nodes around the centre — a small "link graph".
    sats = []
    for i in range(4):
        ang = math.radians(45 + i * 90)
        sats.append((cx + orbit * math.cos(ang), cy + orbit * math.sin(ang)))

    # Edges first (so nodes sit on top).
    for (x, y) in sats:
        d.line([cx, cy, x, y], fill=AMBER_DIM, width=line_w)
    # One satellite-to-satellite edge to suggest a network, not just a star.
    d.line([sats[0][0], sats[0][1], sats[1][0], sats[1][1]], fill=AMBER_DIM,
           width=line_w)

    for (x, y) in sats:
        _node(d, x, y, sat_r, AMBER)
    _node(d, cx, cy, core_r, NODE_CORE)
    return img


def main() -> None:
    master = render(SIZE)

    ico = Path(__file__).with_name("nexus.ico")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
             (256, 256)]
    master.save(ico, format="ICO", sizes=sizes)
    print(f"Wrote {ico} ({ico.stat().st_size} bytes)")

    png = Path(__file__).with_name("nexus.png")
    master.save(png, format="PNG")
    print(f"Wrote {png} ({png.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
