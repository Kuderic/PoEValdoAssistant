"""The app's icon: Valdo's golden puzzle box in a sniper scope's crosshairs,
as 16x16 pixel art drawn from a character map so there is no image file to
ship. Scaled 2x and 4x (nearest-neighbour, so the pixels stay crisp) for the
title bar, taskbar and Alt-Tab sizes; tools/build_exe.py builds the exe's
.ico from the same map. The dark lens keeps it legible on light and dark
taskbars alike.
"""

from __future__ import annotations

import tkinter as tk

PALETTE = {
    "o": "#0b1118",  # outline
    "d": "#16222e",  # lens
    "R": "#5fe0d6",  # scope ring, lit
    "r": "#2a9d9a",  # scope ring, shade
    "X": "#ff5a4e",  # crosshair
    "H": "#fff6c8",  # box highlight
    "T": "#ffd866",  # box top
    "L": "#e0a22a",  # box left face
    "D": "#a8681a",  # box right face
}

PIXELS = (
    "......oooo......",
    "....ooRXXRoo....",
    "..ooRRdXXdRRoo..",
    "..oRdddXXdddRo..",
    ".oRddddooddddRo.",
    ".oRddooHTooddro.",
    "oRddoHHTTTToddro",
    "oXXXoLLTTDDoXXXo",
    "oXXXoLLLDDDoXXXo",
    "oRddoLLLDDDoddro",
    ".oRddooLDooddro.",
    ".oRddddooddddro.",
    "..oRdddXXdddro..",
    "..ooRrdXXdrroo..",
    "....oorXXroo....",
    "......oooo......",
)


def base_image(master: tk.Misc) -> tk.PhotoImage:
    """The 16x16 original; '.' stays transparent."""
    img = tk.PhotoImage(master=master, width=len(PIXELS[0]), height=len(PIXELS))
    for y, row in enumerate(PIXELS):
        for x, ch in enumerate(row):
            if ch in PALETTE:
                img.put(PALETTE[ch], (x, y))
    return img


def apply(root: tk.Tk) -> list[tk.PhotoImage]:
    """Set the icon on `root` and every later Toplevel (the settings panel).
    Returns the images: the caller must keep them referenced, or Tk shows a
    blank icon once Python garbage-collects them."""
    small = base_image(root)
    images = [small, small.zoom(2), small.zoom(4)]
    root.iconphoto(True, *images)
    return images
