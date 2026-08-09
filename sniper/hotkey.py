"""Global hotkey. The `keyboard` callback runs on its hook thread and does
two cheap things only: check the PoE-running gate and hand off to the
asyncio loop. Everything else (alert consumption, the single click_travel,
window focus) happens on the loop via App.hotkey_pressed.

One press schedules exactly one hotkey_pressed() call; the AlertStore's
consume semantics guarantee at most one click per press.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine

import keyboard

from sniper.logging_setup import event


class HotkeyError(Exception):
    pass


class Hotkey:
    def __init__(
        self,
        combo: str,
        loop: asyncio.AbstractEventLoop,
        coro_factory: Callable[[], Coroutine],
        poe_running: threading.Event,
    ):
        self._combo = combo
        self._loop = loop
        self._coro_factory = coro_factory
        self._poe_running = poe_running
        try:
            self._handle = keyboard.add_hotkey(combo, self._on_press)
        except Exception as e:  # registration can fail without elevation
            raise HotkeyError(
                f"could not register global hotkey {combo!r}: {e}. "
                "Try running from an elevated terminal."
            ) from e
        event("hotkey_registered", combo=combo)

    def _on_press(self) -> None:
        """Keyboard hook thread. Keep this tiny and non-blocking."""
        if not self._poe_running.is_set():
            event("hotkey_ignored", reason="poe_not_running", combo=self._combo)
            return
        asyncio.run_coroutine_threadsafe(self._coro_factory(), self._loop)

    def unregister(self) -> None:
        keyboard.remove_hotkey(self._handle)
