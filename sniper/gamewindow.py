"""PoE process detection and window focus.

The hotkey is inert unless the game process is detected (hard constraint in
CLAUDE.md); the hook thread reads `GameWatcher.running` (a threading.Event)
lock-free. Process scans and win32 focus calls are blocking, so they always
run in the executor, never on the asyncio loop itself.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

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
    return True


def focus_poe_window(title: str) -> bool:
    """Bring the PoE window to the foreground. Blocking win32 calls - run in
    the executor. Windows refuses SetForegroundWindow from background
    processes; the ALT-tap works around it, and because the user physically
    pressed the hotkey milliseconds earlier the OS usually grants it anyway.
    Never retried on failure (logged + surfaced instead)."""
    if win32gui is None:
        return False

    matches: list[int] = []

    def enum_cb(hwnd: int, _param: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            matches.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    if not matches:
        event("focus_failed", reason="window_not_found", title=title)
        return False
    hwnd = matches[0]
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # benign ALT tap so Windows lets a background process take foreground
        import win32api

        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        event("focus_failed", reason=repr(e), title=title)
        return False
    event("game_focused", title=title)
    return True
