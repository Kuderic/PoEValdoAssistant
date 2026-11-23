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
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:  # non-Windows dev machine; focus becomes a no-op
    win32api = win32con = win32gui = win32process = None


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


@dataclass(frozen=True)
class BuyerWindow:
    """The game client marked as the buyer (two-client setups).

    Both clients share the title "Path of Exile", so the title alone cannot
    tell them apart, and Windows recycles HWNDs: the pid plus the process
    creation time pin the exact client that was marked.
    """

    hwnd: int
    pid: int
    created: float


def _process_created(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def mark_foreground(title: str) -> BuyerWindow | None:
    """Remember the foreground window as the buyer client. Rejects anything
    that is not a PoE window, so a mis-press over the browser marks nothing.
    Blocking win32 - run in the executor."""
    if win32gui is None:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or win32gui.GetWindowText(hwnd) != title:
            event("buyer_mark_rejected", reason="foreground_not_poe")
            return None
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception as e:  # noqa: BLE001 - pywintypes.error
        event("buyer_mark_rejected", reason=repr(e))
        return None
    created = _process_created(pid)
    if created is None:
        event("buyer_mark_rejected", reason="process_gone", pid=pid)
        return None
    buyer = BuyerWindow(hwnd=int(hwnd), pid=int(pid), created=created)
    event("buyer_marked", hwnd=buyer.hwnd, pid=buyer.pid)
    return buyer


def buyer_alive(buyer: BuyerWindow, title: str) -> bool:
    """True only while the marked client is the same live window it was:
    same HWND, still titled like PoE, same pid, same process start time."""
    if win32gui is None:
        return False
    try:
        if not win32gui.IsWindow(buyer.hwnd) or win32gui.GetWindowText(buyer.hwnd) != title:
            return False
        _tid, pid = win32process.GetWindowThreadProcessId(buyer.hwnd)
    except Exception:  # noqa: BLE001 - window died mid-check (pywintypes.error)
        return False
    created = _process_created(pid)
    # tolerance, not float equality: a live buyer must never read as lost
    # (the gate fails closed); a restart differs by far more than a second
    return pid == buyer.pid and created is not None and abs(created - buyer.created) < 1.0


def _topmost_poe_window(title: str) -> int | None:
    matches: list[int] = []

    def enum_cb(hwnd: int, _param: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            matches.append(hwnd)

    # EnumWindows walks the z-order top-down: [0] is the most recently used
    win32gui.EnumWindows(enum_cb, None)
    return matches[0] if matches else None


def _bring_forward(hwnd: int) -> None:
    """Foreground the window WITHOUT synthesizing any input.

    The old synthetic ALT tap injected a keypress into whichever client
    had focus - with two clients usually the other one - which is software
    input into a game client. Attaching our thread to the foreground
    thread's input queue grants the same SetForegroundWindow right with no
    input at all.
    """
    if win32gui.IsIconic(hwnd):  # restore only when minimised: keep maximised as-is
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    fg = win32gui.GetForegroundWindow()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    me = win32api.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != me:
        try:
            win32process.AttachThreadInput(me, fg_tid, True)
            attached = True
        except Exception:  # noqa: BLE001 - refusal just means a weaker attempt
            pass
    try:
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)
    finally:
        if attached:  # a failing detach must not mask the real error
            with contextlib.suppress(Exception):
                win32process.AttachThreadInput(me, fg_tid, False)


def focus_poe_window(title: str, buyer: BuyerWindow | None = None) -> bool:
    """Bring the PoE window to the foreground: the marked buyer client when
    one is marked, else the most recently used PoE window. Blocking win32
    calls - run in the executor. Never retried; a refusal flashes the
    taskbar button instead (still no input sent)."""
    if win32gui is None:
        return False
    if buyer is not None:
        if not buyer_alive(buyer, title):
            event("focus_failed", reason="buyer_lost", pid=buyer.pid)
            return False
        hwnd = buyer.hwnd
    else:
        hwnd = _topmost_poe_window(title)
        if hwnd is None:
            event("focus_failed", reason="window_not_found", title=title)
            return False
    try:
        _bring_forward(hwnd)
    except Exception as e:  # noqa: BLE001
        event("focus_failed", reason=repr(e), title=title)
        with contextlib.suppress(Exception):  # best effort
            win32gui.FlashWindowEx(hwnd, win32con.FLASHW_ALL | win32con.FLASHW_TIMERNOFG, 0, 0)
        return False
    event("game_focused", title=title, buyer=buyer is not None)
    return True
