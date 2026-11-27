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
# An item of the marked seller's that alerts while the marker is up keeps
# it up at least this much longer
ADDED_MIN_S = 20.0
# Same-seller alerts seen this recently are marked along with the item
# traveled to (they may have expired off the overlay, but most will still
# be in the shop; the shop shows what is actually there)
SAME_SELLER_S = 180.0


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


@dataclass(frozen=True)
class ShopSpot:
    """One item to mark: its listing, the seller's tab and its 0-based cell,
    and a short caption for the legend ("Mageblood 80 divine")."""

    listing_id: str
    tab: str
    x: int
    y: int
    caption: str = ""


class ShopMarker:
    """When the marker is up and which items it marks. Pure state; the
    overlay feeds it events and acts on the answers.

    The marker belongs to one SELLER: their hideout is where the travel
    goes, so every alerted item of theirs is in the shop the player is about
    to open. spots[0] is the item traveled to.

    - travel_ok: a travel click landed. The next area load (within ARM_S)
      is the seller's hideout, so the marker goes up then - or right away
      when PoE's log cannot be read, so the feature still works without it.
    - add: another of that seller's items alerted meanwhile (armed or up);
      it joins the marker. While up, it keeps the marker up a little longer
      so a late arrival is not gone the moment it is drawn.
    - area_entered: a load started. Shows an armed marker; any other load
      (the user left the hideout) takes the marker down.
    - tick: the marker comes down `show_s` after it went up.
    """

    def __init__(self, show_s: float, arm_s: float = ARM_S, max_spots: int = 8):
        self.show_s = show_s
        self.arm_s = arm_s
        self.max_spots = max_spots
        self.seller = ""
        self._spots: tuple[ShopSpot, ...] = ()
        self._deadline: float | None = None  # armed until this; None = not armed
        self.shown: tuple[ShopSpot, ...] | None = None  # the spots on screen
        self._until = 0.0

    def travel_ok(
        self, spots: tuple[ShopSpot, ...], seller: str, now: float, log_live: bool
    ) -> tuple[ShopSpot, ...] | None:
        """The spots to show now, or None (armed for the next load)."""
        self.seller = seller
        self._spots = tuple(spots[: self.max_spots])
        if not log_live:
            self._deadline = None
            return self._show(now)
        self._deadline = now + self.arm_s
        return None

    def add(self, spot: ShopSpot, seller: str, now: float) -> str:
        """Another item of the marked seller's: "shown" when it joined the
        marker on screen (redraw it), "armed" when it joined one still
        waiting for the hideout, "" when it did not join."""
        active = self.shown is not None or (self._deadline is not None and now <= self._deadline)
        if not active or not seller or seller != self.seller:
            return ""
        if any(s.listing_id == spot.listing_id for s in self._spots):
            return ""
        if len(self._spots) >= self.max_spots:
            return ""
        self._spots += (spot,)
        if self.shown is None:
            return "armed"
        self.shown = self._spots
        self._until = max(self._until, now + ADDED_MIN_S)
        return "shown"

    def area_entered(self, now: float) -> tuple[str, tuple[ShopSpot, ...] | None]:
        """("show", spots) | ("hide", None) | ("", None)."""
        deadline, self._deadline = self._deadline, None
        if deadline is not None and now <= deadline and self._spots:
            return "show", self._show(now)
        if self.shown is not None:
            self._clear()
            return "hide", None
        return "", None

    def tick(self, now: float) -> bool:
        """True when the marker must come down now."""
        if self._deadline is not None and now > self._deadline:
            self._deadline = None
            if self.shown is None:
                self._clear()
        if self.shown is not None and now >= self._until:
            self._clear()
            return True
        return False

    def show_now(self, spots: tuple[ShopSpot, ...], now: float, seconds: float):
        """Up right away for `seconds` (the settings panel's Test). Not tied
        to a seller, so nothing joins it."""
        self._deadline = None
        self.seller = ""
        self._spots = tuple(spots)
        self.shown = self._spots
        self._until = now + seconds
        return self.shown

    def cancel(self) -> bool:
        """Drop everything (feature switched off). True if it was up."""
        was = self.shown is not None
        self._deadline = None
        self._clear()
        return was

    def _show(self, now: float) -> tuple[ShopSpot, ...]:
        self.shown = self._spots
        self._until = now + self.show_s
        return self.shown

    def _clear(self) -> None:
        self.shown = None
        self._spots = ()
        self.seller = ""
