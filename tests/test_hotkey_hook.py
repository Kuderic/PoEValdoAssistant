"""The global hotkey fires once per physical press, whatever else is held.

Through the real keyboard 0.13.5 listener with only the OS hook stubbed out:
events go through the same direct_callback (which tracks held keys) and
pre_process_event (which runs key hooks) that a real key press does.
Before the fix, `add_hotkey` dropped a press made while Shift/Ctrl/any key
was down, and fired again on every auto-repeat.
"""

from __future__ import annotations

import asyncio
import threading

import keyboard
import pytest
from keyboard import KEY_DOWN, KEY_UP, KeyboardEvent

from sniper.hotkey import Hotkey


@pytest.fixture
def listener(monkeypatch):
    # init() builds the name and hook tables; the OS hook is only installed
    # by the listening thread start_if_necessary would start, so skip that
    keyboard._listener.init()
    monkeypatch.setattr(keyboard._listener, "start_if_necessary", lambda: None)
    keyboard._pressed_events.clear()
    yield keyboard._listener
    keyboard._pressed_events.clear()


def code(name: str) -> int:
    return keyboard.key_to_scan_codes(name)[0]


def send(listener, name: str, kind: str) -> None:
    ev = KeyboardEvent(kind, code(name), name=name)
    listener.direct_callback(ev)
    listener.pre_process_event(ev)


def tap(listener, name: str) -> None:
    send(listener, name, KEY_DOWN)
    send(listener, name, KEY_UP)


@pytest.fixture
def counted(monkeypatch):
    """Hotkey whose hand-off to asyncio is counted instead of scheduled."""
    calls: list[object] = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: calls.append(coro))

    def build(combo: str = "`") -> Hotkey:
        running = threading.Event()
        running.set()
        return Hotkey(combo, object(), lambda: "press", running)

    return build, calls


def test_a_plain_tap_fires_once(listener, counted):
    build, calls = counted
    hk = build()
    tap(listener, "`")
    assert len(calls) == 1
    hk.unregister()


def test_holding_the_key_fires_once_not_per_repeat(listener, counted):
    build, calls = counted
    hk = build()
    for _ in range(4):  # first down + 3 auto-repeats
        send(listener, "`", KEY_DOWN)
    send(listener, "`", KEY_UP)
    assert len(calls) == 1
    tap(listener, "`")  # released, so the next press counts again
    assert len(calls) == 2
    hk.unregister()


@pytest.mark.parametrize("other", ["shift", "ctrl", "alt", "w"])
def test_a_press_while_another_key_is_held_still_fires(listener, counted, other):
    build, calls = counted
    hk = build()
    send(listener, other, KEY_DOWN)
    tap(listener, "`")
    send(listener, other, KEY_UP)
    assert len(calls) == 1
    hk.unregister()


def test_a_combo_still_needs_its_own_modifier(listener, counted):
    build, calls = counted
    hk = build("ctrl+f9")
    tap(listener, "f9")
    assert calls == []
    send(listener, "ctrl", KEY_DOWN)
    tap(listener, "f9")
    send(listener, "ctrl", KEY_UP)
    assert len(calls) == 1
    hk.unregister()


def test_nothing_fires_while_poe_is_not_running(listener, counted):
    build, calls = counted
    hk = build()
    hk._poe_running.clear()
    tap(listener, "`")
    assert calls == []
    hk.unregister()


def test_rebind_moves_the_hook(listener, counted):
    build, calls = counted
    hk = build("`")
    assert hk.rebind("ctrl+`") is None  # same trigger key, new modifier
    tap(listener, "`")
    assert calls == []
    send(listener, "ctrl", KEY_DOWN)
    tap(listener, "`")
    send(listener, "ctrl", KEY_UP)
    assert len(calls) == 1
    assert hk.rebind("not a key") is not None  # bad name: old combo stays
    send(listener, "ctrl", KEY_DOWN)
    tap(listener, "`")
    send(listener, "ctrl", KEY_UP)
    assert len(calls) == 2
    hk.unregister()
    tap(listener, "`")
    assert len(calls) == 2


def test_unregister_removes_the_hook(listener, counted):
    build, calls = counted
    hk = build()
    hk.unregister()
    tap(listener, "`")
    assert calls == []
