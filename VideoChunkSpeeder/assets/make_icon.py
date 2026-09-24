#!/usr/bin/env python3
"""
make_icon.py
One-off build script: draws a simple "monitor + fast-forward" icon (matching
the app's own speed badge style) and exports icon.png and icon.ico for use as
the shortcut icon.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(HERE, "icon.png")
OUT_ICO = os.path.join(HERE, "icon.ico")

CANVAS = 256
SCREEN = (32, 36, 46, 255)
SCREEN_EDGE = (80, 88, 104, 255)
ACCENT = (255, 176, 32, 255)
STAND = (64, 70, 84, 255)


def main() -> None:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # monitor body
    draw.rounded_rectangle([20, 26, 236, 190], radius=24, fill=SCREEN, outline=SCREEN_EDGE, width=6)

    # stand
    draw.rectangle([118, 190, 138, 214], fill=STAND)
    draw.rounded_rectangle([82, 210, 174, 230], radius=10, fill=STAND)

    # fast-forward glyph (two triangles), centered on the screen
    cx, cy = 128, 106
    tri_h, tri_w = 36, 32
    for dx in (-32, 4):
        draw.polygon(
            [(cx + dx, cy - tri_h), (cx + dx, cy + tri_h), (cx + dx + tri_w, cy)],
            fill=ACCENT,
        )

    canvas.save(OUT_PNG)
    canvas.save(OUT_ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote", OUT_PNG, "and", OUT_ICO)


if __name__ == "__main__":
    main()
