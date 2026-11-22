"""The app's window icon: a 16x16 pixel-art novelty, drawn from a character
map so there is no image file to ship. Scaled 2x and 4x (nearest-neighbour,
so the pixels stay crisp) for the title bar, taskbar and Alt-Tab sizes.
"""

from __future__ import annotations

import tkinter as tk

PALETTE = {
    "o": "#3b1030",  # outline
    "H": "#ffb3e2",  # highlight
    "P": "#e05cb4",  # body
    "S": "#a83a86",  # shade
    "B": "#8a2f78",  # base
    "b": "#c25aa8",  # base highlight
}

PIXELS = (
    "......oooo......",
    ".....oHPPPo.....",
    "....oHHPPPSo....",
    "....oHPPPPSo....",
    ".....oPPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    ".....oHPPSo.....",
    "...ooBBBBBBoo...",
    "..oBBbBBBBBBBo..",
    "..oBBBBBBBBBBo..",
    "...oooooooooo...",
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
