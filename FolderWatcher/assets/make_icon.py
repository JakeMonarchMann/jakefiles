#!/usr/bin/env python3
"""
make_icon.py
One-off build script: draws a folder with a "new item" badge and exports
icon.png and icon.ico for the tray icon and shortcut.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PNG = os.path.join(HERE, "icon.png")
OUT_ICO = os.path.join(HERE, "icon.ico")

CANVAS = 256
FOLDER_BACK = (214, 148, 24, 255)
FOLDER_FRONT = (255, 190, 48, 255)
BADGE = (46, 174, 96, 255)
BADGE_EDGE = (255, 255, 255, 255)


def main() -> None:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # folder: back panel with tab, then front panel
    draw.rounded_rectangle([16, 52, 112, 110], radius=16, fill=FOLDER_BACK)
    draw.rounded_rectangle([16, 76, 240, 208], radius=20, fill=FOLDER_BACK)
    draw.rounded_rectangle([16, 98, 240, 208], radius=20, fill=FOLDER_FRONT)

    # "new" badge with a plus sign
    cx, cy, r = 190, 172, 56
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BADGE, outline=BADGE_EDGE, width=8)
    draw.rectangle([cx - 8, cy - 32, cx + 8, cy + 32], fill=BADGE_EDGE)
    draw.rectangle([cx - 32, cy - 8, cx + 32, cy + 8], fill=BADGE_EDGE)

    canvas.save(OUT_PNG)
    canvas.save(OUT_ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote", OUT_PNG, "and", OUT_ICO)


if __name__ == "__main__":
    main()
