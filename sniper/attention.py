"""Pull the overlay in front of everything WITHOUT taking keyboard focus.

Used for jackpot alerts. Activating the overlay (Tk's lift/focus_force,
or a plain deiconify, which restores AND activates) would move keyboard
focus out of the game mid-map: movement and skill keys would land in the
overlay instead. The hotkey is global and does not need focus, so the
overlay only has to be SEEN. Everything here therefore uses the
no-activate variants:

- a minimised overlay is restored with SW_SHOWNOACTIVATE;
- it is re-asserted HWND_TOPMOST with SWP_NOACTIVATE (Windows can drop a
  topmost window behind a borderless game that grabbed the foreground);
- its taskbar button flashes until stop_flash, for when it is on another
  monitor or covered by an exclusive-fullscreen game, which no overlay can
  draw over.

No-ops off Windows. Failures are swallowed by the caller's logging - this is
best-effort attention, never something that may break alerting.
"""

from __future__ import annotations

import ctypes
import sys

SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
FLASHW_STOP = 0x0
FLASHW_ALL = 0x3  # caption + taskbar button
FLASHW_TIMER = 0x4  # until FLASHW_STOP

if sys.platform == "win32":
    from ctypes import wintypes

    class _FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    # A private user32 handle, so the argtypes below never leak into anyone
    # else's ctypes.windll.user32. Explicit argtypes matter on x64: without
    # them HWND_TOPMOST (-1) is passed as a 32-bit int and arrives as
    # 0x00000000FFFFFFFF, which is not HWND_TOPMOST.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
    _user32.FlashWindowEx.restype = wintypes.BOOL
    _HWND_TOPMOST = wintypes.HWND(-1)
else:
    _user32 = None


def frame_hwnd(root) -> int | None:
    """The top-level frame HWND Tk wraps the root in (what the taskbar and
    the z-order see), or None off Windows."""
    if _user32 is None:
        return None
    return int(root.wm_frame(), 16)


def _flash(hwnd: int, flags: int) -> None:
    info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, flags, 0, 0)
    _user32.FlashWindowEx(ctypes.byref(info))


def bring_to_front(hwnd: int | None) -> None:
    if _user32 is None or hwnd is None:
        return
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    _user32.SetWindowPos(
        hwnd,
        _HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )
    _flash(hwnd, FLASHW_ALL | FLASHW_TIMER)


def stop_flash(hwnd: int | None) -> None:
    if _user32 is None or hwnd is None:
        return
    _flash(hwnd, FLASHW_STOP)
