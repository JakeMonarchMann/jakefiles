#!/usr/bin/env python3
"""
make_icon.py
One-off build script: takes the trawler boat icon and composites a flat-style
CH-47 Chinook helicopter above it, connected by sling lines, then exports
icon.png and icon.ico for use as the shortcut icon.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_BOAT = os.path.join(HERE, "icon_source.png")
OUT_PNG = os.path.join(HERE, "icon.png")
OUT_ICO = os.path.join(HERE, "icon.ico")

CANVAS = 256

# Flat "toy" Chinook palette, kept close to the boat's cartoon style.
OLIVE = (91, 107, 58, 255)
OLIVE_DARK = (66, 79, 40, 255)
CANOPY = (20, 24, 40, 255)
ROTOR = (35, 35, 35, 255)
ROPE = (60, 60, 60, 255)


def draw_chinook(draw: ImageDraw.ImageDraw, cx: int, top: int) -> tuple[int, int]:
    """Draws a simple twin-rotor cargo helicopter. Returns the hook point (x, y)."""
    body_w, body_h = 150, 46
    left = cx - body_w // 2
    right = cx + body_w // 2
    body_top = top
    body_bottom = top + body_h

    # rear ramp taper
    draw.polygon(
        [
            (right - 18, body_top + 6),
            (right + 14, body_top + 16),
            (right + 14, body_bottom - 10),
            (right - 18, body_bottom),
        ],
        fill=OLIVE,
    )

    # main fuselage
    draw.rounded_rectangle(
        [left, body_top, right - 14, body_bottom],
        radius=20,
        fill=OLIVE,
    )

    # belly underline for depth
    draw.rounded_rectangle(
        [left + 6, body_bottom - 12, right - 20, body_bottom],
        radius=8,
        fill=OLIVE_DARK,
    )

    # cockpit canopy at front
    draw.rounded_rectangle(
        [left + 6, body_top + 6, left + 44, body_bottom - 8],
        radius=10,
        fill=CANOPY,
    )

    # rotor pylons + masts
    front_x = left + 30
    rear_x = right - 20
    mast_h = 14
    for rx in (front_x, rear_x):
        draw.rectangle([rx - 4, body_top - mast_h, rx + 4, body_top + 2], fill=OLIVE_DARK)
        # rotor blades (flat ellipse, spinning-blur look)
        draw.ellipse(
            [rx - 46, body_top - mast_h - 6, rx + 46, body_top - mast_h + 6],
            fill=ROTOR,
        )
        draw.ellipse(
            [rx - 5, body_top - mast_h - 5, rx + 5, body_top - mast_h + 5],
            fill=OLIVE_DARK,
        )

    hook_x = cx
    hook_y = body_bottom
    return hook_x, hook_y


def main() -> None:
    boat = Image.open(SRC_BOAT).convert("RGBA")

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    cx = CANVAS // 2
    hook_x, hook_y = draw_chinook(draw, cx, top=18)

    # shrink the boat and hang it from the sling
    scale = 0.62
    boat_small = boat.resize((int(boat.width * scale), int(boat.height * scale)), Image.LANCZOS)
    boat_top = hook_y + 34
    boat_x = cx - boat_small.width // 2

    # sling rope from hook down to the boat's cabin roof
    rope_top_y = hook_y
    rope_bottom_y = boat_top + 6
    draw.line([(cx - 16, rope_top_y), (boat_x + boat_small.width * 0.3, rope_bottom_y)], fill=ROPE, width=4)
    draw.line([(cx + 16, rope_top_y), (boat_x + boat_small.width * 0.7, rope_bottom_y)], fill=ROPE, width=4)

    canvas.alpha_composite(boat_small, (boat_x, boat_top))

    canvas.save(OUT_PNG)
    canvas.save(
        OUT_ICO,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("Wrote", OUT_PNG, "and", OUT_ICO)


if __name__ == "__main__":
    main()
