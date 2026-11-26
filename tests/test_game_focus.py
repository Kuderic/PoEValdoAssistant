"""Bringing PoE (and the overlay) forward must never leave an orange
taskbar flash behind: Windows starts one whenever it refuses a background
foreground request."""

from __future__ import annotations

import sys
import types

import pytest

from sniper import gamewindow

POE = 101


class FakeWin32:
    def __init__(self, foreground: int, grant: bool):
        self.foreground = foreground
        self.grant = grant
        self.calls: list[tuple] = []

    # win32gui
    def EnumWindows(self, cb, param):
        cb(POE, param)

    def IsWindowVisible(self, hwnd):
        return True

    def GetWindowText(self, hwnd):
        return "Path of Exile"

    def GetForegroundWindow(self):
        return self.foreground

    def IsIconic(self, hwnd):
        return False

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("show", hwnd))

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        if self.grant:
            self.foreground = hwnd

    def SetWindowPos(self, *args):
        self.calls.append(("pos", args[0]))

    def FlashWindowEx(self, hwnd, flags, count, timeout):
        self.calls.append(("flash_stop", hwnd))


@pytest.fixture
def fake(monkeypatch):
    def install(foreground: int, grant: bool = True) -> FakeWin32:
        f = FakeWin32(foreground, grant)
        con = types.SimpleNamespace(
            SW_RESTORE=9,
            SW_SHOWNOACTIVATE=4,
            VK_MENU=0x12,
            KEYEVENTF_KEYUP=2,
            SWP_NOMOVE=2,
            SWP_NOSIZE=1,
            SWP_NOACTIVATE=0x10,
            HWND_TOPMOST=-1,
            FLASHW_STOP=0,
        )
        api = types.SimpleNamespace(keybd_event=lambda *a: f.calls.append(("alt",)))
        monkeypatch.setattr(gamewindow, "win32gui", f)
        monkeypatch.setattr(gamewindow, "win32con", con)
        monkeypatch.setitem(sys.modules, "win32api", api)
        return f

    return install


def test_game_already_in_front_is_left_alone(fake):
    f = fake(foreground=POE)
    assert gamewindow.focus_poe_window("Path of Exile") is True
    assert f.calls == []  # no ALT tap, no foreground request, nothing to flash


def test_game_behind_is_brought_forward(fake):
    f = fake(foreground=7)
    assert gamewindow.focus_poe_window("Path of Exile") is True
    assert ("foreground", POE) in f.calls
    assert ("flash_stop", POE) not in f.calls


def test_a_refused_request_does_not_leave_the_taskbar_flashing(fake):
    f = fake(foreground=7, grant=False)
    assert gamewindow.focus_poe_window("Path of Exile") is False
    assert f.calls[-1] == ("flash_stop", POE)


def test_raising_the_overlay_never_flashes_it(fake):
    f = fake(foreground=POE)
    assert gamewindow.raise_without_focus(55) is True
    assert f.calls == [("pos", 55), ("flash_stop", 55)]
    assert f.foreground == POE  # the game keeps the keyboard
