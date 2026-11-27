"""The in-game shop marker window and the two-click grid calibration.

The marker is a borderless, always-on-top Tk window laid exactly over the
game's client area. Its background is a transparent colour key, so only the
drawn box shows; WS_EX_TRANSPARENT makes every click fall through to the
game and WS_EX_NOACTIVATE keeps the keyboard with the game - it never takes
focus, and it never sends the game anything. It only draws.

It shows over windowed / borderless-fullscreen PoE; exclusive fullscreen
draws over everything and hides it.

Tk thread only.
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from collections.abc import Callable

from sniper.gamewindow import GameRect
from sniper.logging_setup import event
from sniper.shopgrid import ShopGrid, ShopSpot, cell_box, from_corners, grid_side, place

try:
    import win32con
    import win32gui
except ImportError:  # non-Windows dev machine: the marker is a no-op
    win32gui = None
    win32con = None

KEY = "#010203"  # the colour key: pixels this colour are see-through
MARK = "#39ff88"  # the item traveled to
OTHER = "#4dd8ff"  # the same seller's other alerted items
MARK_EDGE = "#000000"  # a dark halo so the box reads on any item art
GRID = "#ffd34d"  # the grid's outline (and every cell, in the test view)
LABEL_BG = "#0b0e11"
LABEL_FG = "#f2f4f5"


def _legend(n: int, spot: ShopSpot, numbered: bool) -> str:
    """'1 ★ tab "7" · col 9, row 10 · Mageblood 80 divine'."""
    head = (f"{n} " if numbered else "") + ("★ " if n == 1 else "")
    tab = f'tab "{spot.tab}" · ' if spot.tab else ""
    tail = f" · {spot.caption}" if spot.caption and numbered else ""
    return f"{head}{tab}col {spot.x + 1}, row {spot.y + 1}{tail}"


def _px(h: int, at_1440: float) -> int:
    """A stroke / font size that scales with the game's height."""
    return max(1, round(at_1440 * h / 1440))


class ShopMarkerWindow:
    def __init__(self, root: tk.Misc, font_family: str = "Segoe UI"):
        self._family = font_family
        win = tk.Toplevel(root)
        win.withdraw()  # never mapped by Tk: shown with SW_SHOWNOACTIVATE
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.config(bg=KEY)
        with contextlib.suppress(tk.TclError):  # not Windows
            win.attributes("-transparentcolor", KEY)
        self._canvas = tk.Canvas(win, bg=KEY, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)
        win.update_idletasks()
        self._win = win
        self._hwnd = 0
        if win32gui is not None:
            try:
                self._hwnd = int(win.wm_frame(), 16)
                ex = win32gui.GetWindowLong(self._hwnd, win32con.GWL_EXSTYLE)
                ex |= (
                    win32con.WS_EX_LAYERED
                    | win32con.WS_EX_TRANSPARENT  # clicks go to the game
                    | win32con.WS_EX_NOACTIVATE  # the game keeps the keyboard
                    | win32con.WS_EX_TOOLWINDOW  # no taskbar button
                    | win32con.WS_EX_TOPMOST
                )
                win32gui.SetWindowLong(self._hwnd, win32con.GWL_EXSTYLE, ex)
            except Exception as e:
                event("shop_marker_init_failed", reason=repr(e))
                self._hwnd = 0
        self.visible = False

    def show(
        self, rect: GameRect, grid: ShopGrid, spots: tuple[ShopSpot, ...], test: bool = False
    ) -> None:
        """Outline each spot's cell over the game: spots[0] (the item
        traveled to) in green, the seller's other alerted items in cyan,
        numbered when there are several, with a legend above the shop.
        `test` also draws every cell line, to check the calibration."""
        if not self._hwnd or not spots:
            return
        c = self._canvas
        c.delete("all")
        pw, ph = rect.pw, rect.ph

        def tk_xy(px: float, py: float) -> tuple[float, float]:
            return px * rect.w / pw, py * rect.h / ph  # window-relative

        left, top, size = place(grid, pw, ph)
        gx0, gy0 = tk_xy(left, top)
        gx1, gy1 = tk_xy(left + size, top + size)
        thin = _px(rect.h, 2)
        c.create_rectangle(gx0, gy0, gx1, gy1, outline=GRID, width=thin)
        if test:
            first = spots[0]
            side = grid_side(first.x, first.y)
            for i in range(1, side):
                lx, _ = tk_xy(left + size * i / side, 0)
                _, ly = tk_xy(0, top + size * i / side)
                c.create_line(lx, gy0, lx, gy1, fill=GRID, width=1)
                c.create_line(gx0, ly, gx1, ly, fill=GRID, width=1)
        many = len(spots) > 1
        thick = _px(rect.h, 5)
        pad = thick / 2
        badge_font = (self._family, -_px(rect.h, 18), "bold")
        # the traveled item last, so it sits on top where cells coincide
        # (two items at the same cell in different tabs)
        for n, spot in reversed(list(enumerate(spots, 1))):
            colour = MARK if n == 1 else OTHER
            x0, y0, x1, y1 = cell_box(grid, pw, ph, spot.x, spot.y)
            bx0, by0 = tk_xy(x0, y0)
            bx1, by1 = tk_xy(x1, y1)
            box = (bx0 - pad, by0 - pad, bx1 + pad, by1 + pad)
            c.create_rectangle(*box, outline=MARK_EDGE, width=thick + 2)
            c.create_rectangle(*box, outline=colour, width=thick)
            if many:
                r = _px(rect.h, 13)
                cx, cy = box[0] + r, box[1] + r
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=colour, outline=MARK_EDGE)
                c.create_text(cx, cy, text=str(n), fill=MARK_EDGE, font=badge_font)
        # the legend stacks upward from just above the grid, clear of the
        # shop's tab buttons
        if test:
            lines = [("Test: the yellow lines should sit on the shop's grid", MARK)]
        else:
            lines = [
                (_legend(n, spot, many), MARK if n == 1 else OTHER)
                for n, spot in enumerate(spots, 1)
            ]
        font = (self._family, -_px(rect.h, 22), "bold")
        step = _px(rect.h, 34)
        bottom = max(gy0 - _px(rect.h, 70), step * len(lines))
        m = _px(rect.h, 6)
        for i, (text, colour) in enumerate(reversed(lines)):
            tid = c.create_text(
                (gx0 + gx1) / 2, bottom - i * step, text=text, fill=LABEL_FG, font=font, anchor="s"
            )
            bx = c.bbox(tid)
            if bx:
                back = c.create_rectangle(
                    bx[0] - m,
                    bx[1] - m // 2,
                    bx[2] + m,
                    bx[3] + m // 2,
                    fill=LABEL_BG,
                    outline=colour,
                )
                c.tag_lower(back, tid)
        self._win.geometry(f"{rect.w}x{rect.h}+{rect.x}+{rect.y}")
        try:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            win32gui.SetWindowPos(
                self._hwnd,
                win32con.HWND_TOPMOST,
                rect.x,
                rect.y,
                rect.w,
                rect.h,
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
        except Exception as e:
            event("shop_marker_show_failed", reason=repr(e))
            return
        self.visible = True

    def hide(self) -> None:
        self._canvas.delete("all")
        if self._hwnd and self.visible:
            try:
                win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            except Exception as e:
                event("shop_marker_hide_failed", reason=repr(e))
        self.visible = False


class Calibrator:
    """A dimmed, full-size layer over the game: the user clicks the shop
    grid's top-left and bottom-right corners, previews the 12x12 grid, and
    Enter saves it. Esc cancels. Clicks land here, not in the game.

    `on_done(grid | None)` fires once (None = cancelled)."""

    def __init__(
        self,
        root: tk.Misc,
        rect: GameRect,
        on_done: Callable[[ShopGrid | None], None],
        font_family: str = "Segoe UI",
    ):
        self._rect = rect
        self._on_done = on_done
        self._points: list[tuple[float, float]] = []  # physical, client-relative
        self._grid: ShopGrid | None = None
        self._finished = False
        self._family = font_family
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.55)
        win.config(bg="black", cursor="crosshair")
        win.geometry(f"{rect.w}x{rect.h}+{rect.x}+{rect.y}")
        c = tk.Canvas(win, bg="black", highlightthickness=0, bd=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        self._win, self._canvas = win, c
        c.bind("<Button-1>", self._click)
        c.bind("<Motion>", self._motion)
        win.bind("<Escape>", lambda e: self._finish(None))
        win.bind("<Return>", lambda e: self._grid is not None and self._finish(self._grid))
        win.protocol("WM_DELETE_WINDOW", lambda: self._finish(None))
        self._draw()
        win.lift()
        win.focus_force()

    def _font(self, size: int):
        return (self._family, -_px(self._rect.h, size), "bold")

    def _say(self) -> str:
        if self._grid is not None:
            return (
                "Do the yellow lines sit on the shop's grid?   "
                "Enter = save  ·  click = start again  ·  Esc = cancel"
            )
        if not self._points:
            return (
                "Open Faustus' Merchant window in game, then click the TOP-LEFT "
                "corner of its item grid.   Esc = cancel"
            )
        return "Now click the BOTTOM-RIGHT corner of the item grid.   Esc = cancel"

    def _draw(self, error: str = "") -> None:
        c, rect = self._canvas, self._rect
        c.delete("static")
        c.create_text(
            rect.w / 2,
            _px(rect.h, 60),
            text=self._say(),
            fill="white",
            font=self._font(26),
            tags="static",
        )
        if error:
            c.create_text(
                rect.w / 2,
                _px(rect.h, 110),
                text=error,
                fill="#ff6b6b",
                font=self._font(22),
                tags="static",
            )
        for px, py in self._points:
            x, y = px * rect.w / rect.pw, py * rect.h / rect.ph
            r = _px(rect.h, 8)
            c.create_oval(x - r, y - r, x + r, y + r, outline=MARK, width=2, tags="static")
        if self._grid is not None:
            left, top, size = place(self._grid, rect.pw, rect.ph)
            sx, sy = rect.w / rect.pw, rect.h / rect.ph
            for i in range(13):
                lx = (left + size * i / 12) * sx
                ly = (top + size * i / 12) * sy
                width = 3 if i in (0, 12) else 1
                c.create_line(
                    lx, top * sy, lx, (top + size) * sy, fill=GRID, width=width, tags="static"
                )
                c.create_line(
                    left * sx, ly, (left + size) * sx, ly, fill=GRID, width=width, tags="static"
                )

    def _motion(self, e) -> None:
        c = self._canvas
        c.delete("cross")
        if self._grid is None:
            c.create_line(e.x, 0, e.x, self._rect.h, fill="white", tags="cross")
            c.create_line(0, e.y, self._rect.w, e.y, fill="white", tags="cross")

    def _click(self, e) -> None:
        if self._grid is not None:  # start again
            self._grid = None
            self._points = []
        self._points.append(self._rect.to_physical(e.x, e.y))
        error = ""
        if len(self._points) == 2:
            try:
                self._grid = from_corners(*self._points, self._rect.pw, self._rect.ph)
                self._canvas.delete("cross")
            except ValueError as exc:
                error = f"{exc} - try again"
                self._points = []
        self._draw(error)

    def _finish(self, grid: ShopGrid | None) -> None:
        if self._finished:
            return
        self._finished = True
        self._win.destroy()
        self._on_done(grid)
