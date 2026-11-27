"""Where the seller's item sits on screen in Faustus' shop (the Merchant
window), and when to mark it.

The trade data gives the item's cell in the seller's tab (listing.stash
{x, y}); this module turns that into a box in the game window's client
area, in physical pixels.

Geometry, from a real 2560x1440 capture (see DESIGN.md, Shop marker): PoE
scales its whole UI with the client HEIGHT, the inventory
panel sits at the right edge, and the Merchant window is centred in the
space left of it. So the grid's size and top scale with height, and its
centre sits a height-proportional distance left of the client's centre.
One measurement (`ShopGrid`) therefore holds at any resolution; a user
whose layout differs calibrates it (two clicks on the grid's corners) or
types the numbers in the settings panel.

Pure: no Tk, no win32, no I/O beyond the small settings file helpers.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

# A normal (and Merchant's) tab is 12x12; a cell past 11 only fits a quad tab
TAB_CELLS = 12
QUAD_CELLS = 24
# Saved by the settings panel / calibration, next to scoring_overrides.yaml
SHOP_OVERLAY_NAME = "shop_overlay.yaml"
# A calibrated grid smaller than this (physical px) is a mis-click
MIN_GRID_PX = 120
# After a successful travel click, the next area load within this long is
# the seller's hideout. Teleports start within a second or two of the click.
ARM_S = 20.0


@dataclass(frozen=True)
class ShopGrid:
    """The Merchant window's item grid (its outer border), measured in
    physical pixels of a `width` x `height` game client area."""

    left: float
    top: float
    size: float  # the grid is square
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be > 0")
        if self.size < MIN_GRID_PX:
            raise ValueError(f"grid size must be at least {MIN_GRID_PX}px")
        if self.left < 0 or self.top < 0:
            raise ValueError("left and top must be >= 0")
        if self.left + self.size > self.width or self.top + self.size > self.height:
            raise ValueError("the grid must fit inside the game window")


# Measured on the user's 2560x1440 capture: the blue grid border spans
# x 412-1261 and y 345-1195 (12 cells of ~70.8px).
DEFAULT_GRID = ShopGrid(left=412, top=345, size=850, width=2560, height=1440)


def grid_side(x: int, y: int) -> int:
    """Cells per side of the seller's tab. The trade data does not say how
    big the tab is: 12x12 unless the cell only fits a quad tab."""
    return TAB_CELLS if max(x, y) < TAB_CELLS else QUAD_CELLS


def place(grid: ShopGrid, width: float, height: float) -> tuple[float, float, float]:
    """(left, top, size) of the grid in a `width` x `height` client area."""
    s = height / grid.height
    size = grid.size * s
    # how far the grid's centre sits left of the client's centre
    offset = grid.width / 2 - (grid.left + grid.size / 2)
    centre_x = width / 2 - offset * s
    return centre_x - size / 2, grid.top * s, size


def cell_box(
    grid: ShopGrid, width: float, height: float, x: int, y: int
) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of cell (x, y) - 0-based from the top left - in a
    `width` x `height` client area."""
    left, top, size = place(grid, width, height)
    cell = size / grid_side(x, y)
    return left + x * cell, top + y * cell, left + (x + 1) * cell, top + (y + 1) * cell


def from_corners(
    a: tuple[float, float], b: tuple[float, float], width: int, height: int
) -> ShopGrid:
    """The grid from two clicked corners (either order). The grid is square,
    so its size is the mean of the two spans. ValueError on a mis-click."""
    left, top = min(a[0], b[0]), min(a[1], b[1])
    w, h = abs(a[0] - b[0]), abs(a[1] - b[1])
    if min(w, h) < MIN_GRID_PX:
        raise ValueError("those corners are too close together")
    if max(w, h) > 1.25 * min(w, h):
        raise ValueError("the grid is square - click its top-left and bottom-right corners")
    size = (w + h) / 2
    return ShopGrid(
        left=round(left, 1),
        top=round(top, 1),
        size=round(min(size, width - left, height - top), 1),
        width=int(width),
        height=int(height),
    )


def grid_from_mapping(raw) -> ShopGrid:
    """A ShopGrid from config / saved YAML. ValueError if malformed."""
    if not isinstance(raw, dict):
        raise ValueError("grid must be a mapping of left, top, size, width, height")
    keys = ("left", "top", "size", "width", "height")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ValueError(f"grid is missing {missing}")
    unknown = sorted(set(raw) - set(keys))
    if unknown:
        raise ValueError(f"grid has unknown key(s) {unknown}")
    try:
        return ShopGrid(
            left=float(raw["left"]),
            top=float(raw["top"]),
            size=float(raw["size"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
        )
    except (TypeError, ValueError) as e:
        raise ValueError(f"grid is invalid: {e}") from None


def load_saved(path: Path) -> dict:
    """{"enabled": bool, "grid": ShopGrid} - whatever of those the settings
    panel saved. A broken file is ignored, never fatal."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    if raw.get("grid") is not None:
        with contextlib.suppress(ValueError):
            out["grid"] = grid_from_mapping(raw["grid"])
    return out


def save(path: Path, enabled: bool, grid: ShopGrid | None) -> None:
    """Persist the panel's values. grid None = back to the default."""
    data: dict = {"enabled": enabled}
    if grid is not None:
        data["grid"] = asdict(grid)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class ShopMarker:
    """When the marker is up. Pure state; the overlay feeds it events and
    acts on the answers.

    - travel_ok: a travel click landed. The next area load (within ARM_S)
      is the seller's hideout, so the marker goes up then - or right away
      when PoE's log cannot be read, so the feature still works without it.
    - area_entered: a load started. Shows an armed marker; any other load
      (the user left the hideout) takes the marker down.
    - tick: the marker comes down `show_s` after it went up.
    """

    def __init__(self, show_s: float, arm_s: float = ARM_S):
        self.show_s = show_s
        self.arm_s = arm_s
        self._armed: tuple[tuple, float] | None = None  # (stash, deadline)
        self.shown: tuple | None = None  # the stash on screen
        self._until = 0.0

    def travel_ok(self, stash: tuple, now: float, log_live: bool) -> tuple | None:
        """The stash to show now, or None (armed for the next load)."""
        if not log_live:
            self._armed = None
            return self._show(stash, now)
        self._armed = (stash, now + self.arm_s)
        return None

    def area_entered(self, now: float) -> tuple[str, tuple | None]:
        """("show", stash) | ("hide", None) | ("", None)."""
        armed, self._armed = self._armed, None
        if armed is not None and now <= armed[1]:
            return "show", self._show(armed[0], now)
        if self.shown is not None:
            self.shown = None
            return "hide", None
        return "", None

    def tick(self, now: float) -> bool:
        """True when the marker must come down now."""
        if self._armed is not None and now > self._armed[1]:
            self._armed = None
        if self.shown is not None and now >= self._until:
            self.shown = None
            return True
        return False

    def show_now(self, stash: tuple, now: float, seconds: float) -> tuple:
        """Up right away for `seconds` (the settings panel's Test)."""
        self._armed = None
        self.shown = stash
        self._until = now + seconds
        return stash

    def cancel(self) -> bool:
        """Drop everything (feature switched off). True if it was up."""
        self._armed = None
        was, self.shown = self.shown, None
        return was is not None

    def _show(self, stash: tuple, now: float) -> tuple:
        self.shown = stash
        self._until = now + self.show_s
        return stash
