"""PoE process detection and window focus.

The hotkey is inert unless the game process is detected (hard constraint in
CLAUDE.md); the hook thread reads `GameWatcher.running` (a threading.Event)
lock-free. Process scans and win32 focus calls are blocking, so they always
run in the executor, never on the asyncio loop itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

import psutil

from sniper.logging_setup import event

try:
    import win32con
    import win32gui
except ImportError:  # non-Windows dev machine; focus becomes a no-op
    win32gui = None
    win32con = None


class GameWatcher:
    def __init__(self, process_names: tuple[str, ...], poll_s: float = 3.0):
        self._names = {n.lower() for n in process_names}
        self._poll_s = poll_s
        self.running = threading.Event()  # read from the keyboard hook thread

    def _scan(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() in self._names:
                return True
        return False

    async def watch(self, on_change: Callable[[bool], None]) -> None:
        loop = asyncio.get_running_loop()
        first = True
        while True:
            running = await loop.run_in_executor(None, self._scan)
            if first or running != self.running.is_set():
                if running:
                    self.running.set()
                else:
                    self.running.clear()
                event("game_state", running=running)
                on_change(running)
                first = False
            await asyncio.sleep(self._poll_s)


def raise_without_focus(hwnd: int) -> bool:
    """Put a window on top (topmost) and restore it if minimized, WITHOUT
    activating it: the game keeps keyboard focus. False when win32 is
    unavailable or the calls fail; the caller falls back to Tk."""
    if win32gui is None or not hwnd:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
    except Exception as e:
        event("overlay_raise_failed", reason=repr(e))
        return False
    stop_taskbar_flash(hwnd)
    return True


def minimize_without_focus(hwnd: int) -> bool:
    """Minimize a window WITHOUT activating anything: SW_SHOWMINNOACTIVE,
    not Tk's iconify (SW_MINIMIZE activates the next window in the Z order,
    which can take the keyboard away from the game).

    Asynchronous, from a helper thread: called on the Tk thread, ShowWindow
    has Tk handle the minimize synchronously, and a Python binding it fires
    then (<Unmap>, <Leave>...) runs with the GIL released by pywin32 - a
    fatal "PyEval_RestoreThread" crash. From another thread the messages
    reach Tk through its own event loop, where callbacks are safe. The
    window is minimized a moment after this returns. False when win32 is
    unavailable; the caller falls back to Tk."""
    if win32gui is None or not hwnd:
        return False

    def run() -> None:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
        except Exception as e:
            event("overlay_minimize_failed", reason=repr(e))

    threading.Thread(target=run, name="overlay-minimize", daemon=True).start()
    return True


def stop_taskbar_flash(hwnd: int) -> None:
    """Cancel a window's orange taskbar flash. Windows starts one whenever
    it refuses a background process's request for the foreground; the user
    never wants that nag from this app."""
    if win32gui is None or not hwnd:
        return
    with contextlib.suppress(Exception):
        win32gui.FlashWindowEx(hwnd, win32con.FLASHW_STOP, 0, 0)


def find_window(title: str) -> int:
    """The first visible top-level window titled exactly `title`, or 0."""
    if win32gui is None:
        return 0
    matches: list[int] = []

    def enum_cb(hwnd: int, _param: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            matches.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    return matches[0] if matches else 0


@dataclass(frozen=True)
class GameRect:
    """The game window's client area, twice over: `x, y, w, h` in this
    process's own (Tk) coordinates, and `pw, ph` its size in physical
    pixels. The app is DPI-unaware, so on a scaled display Windows hands it
    virtualised coordinates; PoE lays its UI out in physical pixels."""

    x: int
    y: int
    w: int
    h: int
    pw: int
    ph: int

    def to_tk(self, px: float, py: float) -> tuple[float, float]:
        """A physical client point -> Tk screen coordinates."""
        return self.x + px * self.w / self.pw, self.y + py * self.h / self.ph

    def to_physical(self, tx: float, ty: float) -> tuple[float, float]:
        """Tk coordinates relative to the client's top left -> physical."""
        return tx * self.pw / self.w, ty * self.ph / self.h


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = ctypes.wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right, rect.bottom


def game_client_rect(title: str) -> GameRect | None:
    """The PoE window's client area, or None (not found, minimized, or no
    win32). Quick user32 calls; fine on the Tk thread."""
    hwnd = find_window(title)
    if not hwnd or win32gui.IsIconic(hwnd):
        return None
    import ctypes

    try:
        x, y, w, h = _client_rect(hwnd)
        # the same rect in PHYSICAL pixels: this thread is per-monitor DPI
        # aware just for the call (as tools/screenshots.py does)
        user32 = ctypes.windll.user32
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            _px, _py, pw, ph = _client_rect(hwnd)
        finally:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    except (OSError, AttributeError) as e:
        event("game_rect_failed", reason=repr(e))
        return None
    if min(w, h, pw, ph) <= 0:
        return None
    return GameRect(x, y, w, h, pw, ph)


def focus_poe_window(title: str) -> bool:
    """Bring the PoE window to the foreground. Blocking win32 calls - run in
    the executor. Windows refuses SetForegroundWindow from background
    processes; the ALT-tap works around it, and because the user physically
    pressed the hotkey milliseconds earlier the OS usually grants it anyway.
    Never retried on failure (logged + surfaced instead)."""
    if win32gui is None:
        return False

    hwnd = find_window(title)
    if not hwnd:
        event("focus_failed", reason="window_not_found", title=title)
        return False
    if win32gui.GetForegroundWindow() == hwnd:
        # the usual case (the hotkey was pressed in game): nothing to do, and
        # no ALT tap or foreground request that could flash the taskbar
        event("game_focused", title=title, already=True)
        return True
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # benign ALT tap so Windows lets a background process take foreground
        import win32api

        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        stop_taskbar_flash(hwnd)  # a refused request flashes it instead
        event("focus_failed", reason=repr(e), title=title)
        return False
    if win32gui.GetForegroundWindow() != hwnd:
        stop_taskbar_flash(hwnd)
        event("focus_failed", reason="refused", title=title)
        return False
    event("game_focused", title=title)
    return True
