"""Window focus and buyer marking, with fake win32 modules (the real ones
exist only on Windows). The key guarantees: no synthetic keyboard input,
the marked buyer is the only window focused, and a lost buyer is refused
rather than silently replaced by the other client."""

from types import SimpleNamespace

import pytest

from sniper import gamewindow
from sniper.gamewindow import BuyerWindow, buyer_alive, focus_poe_window, mark_foreground

TITLE = "Path of Exile"


class FakeWin:
    """Two PoE clients (hwnd 10 = buyer, pid 100; hwnd 20 = other, pid 200)
    plus a browser (hwnd 30)."""

    def __init__(self):
        self.titles = {10: TITLE, 20: TITLE, 30: "Chrome"}
        self.pids = {10: 100, 20: 200, 30: 300}
        self.zorder = [20, 10, 30]  # client B most recently used
        self.foreground = 20
        self.iconic: set[int] = set()
        self.calls: list[tuple] = []

    def modules(self):
        gui = SimpleNamespace(
            GetForegroundWindow=lambda: self.foreground,
            GetWindowText=lambda h: self.titles.get(h, ""),
            IsWindow=lambda h: h in self.titles,
            IsWindowVisible=lambda h: True,
            IsIconic=lambda h: h in self.iconic,
            EnumWindows=lambda cb, p: [cb(h, p) for h in self.zorder],
            ShowWindow=lambda h, c: self.calls.append(("show", h, c)),
            SetForegroundWindow=lambda h: self.calls.append(("fg", h)),
            BringWindowToTop=lambda h: self.calls.append(("top", h)),
            FlashWindowEx=lambda *a: self.calls.append(("flash", a[0])),
        )
        proc = SimpleNamespace(
            GetWindowThreadProcessId=lambda h: (h * 10, self.pids[h]),
            AttachThreadInput=lambda a, b, on: self.calls.append(("attach", b, on)),
        )
        # deliberately NO keybd_event: any synthetic-input call would raise
        api = SimpleNamespace(GetCurrentThreadId=lambda: 1)
        con = SimpleNamespace(SW_RESTORE=9, FLASHW_ALL=3, FLASHW_TIMERNOFG=12)
        return gui, proc, api, con


@pytest.fixture
def win(monkeypatch):
    fake = FakeWin()
    gui, proc, api, con = fake.modules()
    monkeypatch.setattr(gamewindow, "win32gui", gui)
    monkeypatch.setattr(gamewindow, "win32process", proc)
    monkeypatch.setattr(gamewindow, "win32api", api)
    monkeypatch.setattr(gamewindow, "win32con", con)
    monkeypatch.setattr(gamewindow, "_process_created", lambda pid: float(pid) + 0.5)
    return fake


def test_mark_rejects_non_poe_foreground(win):
    win.foreground = 30
    assert mark_foreground(TITLE) is None


def test_mark_records_pid_and_start_time(win):
    win.foreground = 10
    assert mark_foreground(TITLE) == BuyerWindow(hwnd=10, pid=100, created=100.5)


def test_focus_marked_buyer_even_when_other_client_is_on_top(win):
    buyer = BuyerWindow(hwnd=10, pid=100, created=100.5)
    assert focus_poe_window(TITLE, buyer) is True
    assert ("fg", 10) in win.calls and ("fg", 20) not in win.calls
    # attached to the foreground thread's input queue, then detached
    assert ("attach", 200, True) in win.calls and ("attach", 200, False) in win.calls


def test_lost_buyer_is_refused_not_replaced_by_other_client(win):
    buyer = BuyerWindow(hwnd=10, pid=100, created=100.5)
    del win.titles[10]  # client A closed
    assert focus_poe_window(TITLE, buyer) is False
    assert not [c for c in win.calls if c[0] == "fg"]


def test_recycled_hwnd_with_different_process_is_not_the_buyer(win):
    buyer = BuyerWindow(hwnd=10, pid=100, created=100.5)
    win.pids[10] = 999  # same hwnd number, new process
    assert buyer_alive(buyer, TITLE) is False


def test_restarted_client_same_pid_different_start_time(win, monkeypatch):
    buyer = BuyerWindow(hwnd=10, pid=100, created=1.0)
    assert buyer_alive(buyer, TITLE) is False


def test_unmarked_focus_takes_most_recent_poe_window(win):
    assert focus_poe_window(TITLE) is True
    assert ("fg", 20) in win.calls


def test_maximised_window_is_not_restored(win):
    focus_poe_window(TITLE, BuyerWindow(hwnd=10, pid=100, created=100.5))
    assert not [c for c in win.calls if c[0] == "show"]
    win.iconic.add(10)
    focus_poe_window(TITLE, BuyerWindow(hwnd=10, pid=100, created=100.5))
    assert ("show", 10, 9) in win.calls


def test_refused_foreground_flashes_instead(win, monkeypatch):
    def refuse(h):
        raise RuntimeError("access denied")

    monkeypatch.setattr(gamewindow.win32gui, "SetForegroundWindow", refuse)
    assert focus_poe_window(TITLE) is False
    assert ("flash", 20) in win.calls
    assert ("attach", 200, False) in win.calls  # detached even on refusal


def test_no_synthetic_input_api_is_referenced():
    import inspect

    src = inspect.getsource(gamewindow)
    assert "keybd_event" not in src and "SendInput" not in src


def test_window_dying_mid_check_reads_as_lost_not_an_exception(win, monkeypatch):
    def boom(h):
        raise OSError("invalid window handle")  # pywintypes.error stand-in

    monkeypatch.setattr(gamewindow.win32process, "GetWindowThreadProcessId", boom)
    assert buyer_alive(BuyerWindow(hwnd=10, pid=100, created=100.5), TITLE) is False
    win.foreground = 10
    assert mark_foreground(TITLE) is None


def test_failing_detach_still_flashes(win, monkeypatch):
    def refuse(h):
        raise RuntimeError("denied")

    def attach(a, b, on):
        if not on:
            raise RuntimeError("detach failed")

    monkeypatch.setattr(gamewindow.win32gui, "SetForegroundWindow", refuse)
    monkeypatch.setattr(gamewindow.win32process, "AttachThreadInput", attach)
    assert focus_poe_window(TITLE) is False
    assert ("flash", 20) in win.calls


def test_start_time_jitter_does_not_lose_a_live_buyer(win):
    assert buyer_alive(BuyerWindow(hwnd=10, pid=100, created=100.5 + 0.004), TITLE) is True
