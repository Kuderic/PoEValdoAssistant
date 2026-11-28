"""Global hotkey. The `keyboard` callback runs on its hook thread and does
two cheap things only: check the PoE-running gate and hand off to the
asyncio loop. Everything else (alert consumption, the single click_travel,
window focus) happens on the loop via App.hotkey_pressed.

One physical press schedules exactly one hotkey_pressed() call; the
AlertStore's consume semantics guarantee at most one click per press.

The combo's trigger key (its last key) is hooked directly rather than through
`keyboard.add_hotkey`, for two reasons found in keyboard 0.13.5:
- add_hotkey only matches when the keys held are EXACTLY the combo, so a
  press while Shift, Ctrl, Alt or a skill key happened to be down (shift-
  clicking currency, moving items) was silently dropped - no log line at all.
  Here extra keys are ignored; the combo's own modifiers must still be held.
- add_hotkey fires on every auto-repeat key-down, so holding the key fired
  several presses (with `consume: top`, several alerts). Here only the first
  key-down counts until the key is released.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine

import keyboard

from sniper.logging_setup import event


class HotkeyError(Exception):
    pass


def _hook_combo(combo: str, on_press: Callable[[], None]) -> Callable[[], None]:
    """Hook `combo`; returns the function that removes the hook. Raises
    ValueError for a combo keyboard cannot parse or a multi-step one."""
    steps = keyboard.parse_hotkey(combo)
    if len(steps) != 1:
        raise ValueError("multi-step hotkeys (keys separated by a comma) are not supported")
    *modifiers, trigger = steps[0]
    held = {"down": False}

    def on_key(e) -> None:
        if e.event_type == keyboard.KEY_UP:
            held["down"] = False
            return
        if held["down"]:
            return  # auto-repeat while the key is held: still the same press
        held["down"] = True
        if all(any(keyboard.is_pressed(code) for code in codes) for codes in modifiers):
            on_press()

    return keyboard.hook_key(tuple(trigger), on_key)


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
            self._remove = _hook_combo(combo, self._on_press)
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

    def rebind(self, combo: str) -> str | None:
        """Swap the hotkey combo live (settings panel). Returns an error
        message on failure (old combo stays active), None on success."""
        combo = combo.strip()
        if combo == self._combo:
            return None
        try:
            keyboard.parse_hotkey(combo)  # reject a bad name before unhooking
        except Exception as e:
            return f"invalid hotkey {combo!r}: {e}"
        # Old hook off BEFORE the new one goes on: keyboard keys its removal
        # bookkeeping by key, so two live hooks on the same trigger key (a
        # rebind from ` to ctrl+`) would break the second removal.
        self._remove()
        try:
            self._remove = _hook_combo(combo, self._on_press)
        except Exception as e:
            self._remove = _hook_combo(self._combo, self._on_press)
            return f"invalid hotkey {combo!r}: {e}"
        self._combo = combo
        event("hotkey_rebound", combo=combo)
        return None

    @property
    def combo(self) -> str:
        return self._combo

    def unregister(self) -> None:
        self._remove()


def trigger_key(combo: str) -> tuple | None:
    """The scan codes of a combo's trigger (last) key; None if it does not
    parse. Two hooks must never share one: keyboard keys a hook's removal
    by its trigger, so removing the second would fail (see Hotkey.rebind)."""
    try:
        steps = keyboard.parse_hotkey(combo)
    except Exception:
        return None
    return tuple(steps[0][-1]) if len(steps) == 1 else None


class KeyHook:
    """A global key for a LOCAL action - closing the alert pop-up - never a
    server action. The key is not swallowed: the game still gets it (Esc
    still closes its panels). `on_press` runs on keyboard's hook thread, so
    it must be tiny and non-blocking. An empty combo hooks nothing."""

    def __init__(self, combo: str, on_press: Callable[[], None], name: str = "key"):
        self._name = name
        self._on_press = on_press
        self._combo = ""
        self._remove: Callable[[], None] | None = None
        error = self.rebind(combo)
        if error:
            raise ValueError(error)

    @property
    def combo(self) -> str:
        return self._combo

    def rebind(self, combo: str) -> str | None:
        """Swap the key live. Returns an error (the old key stays) or None."""
        combo = combo.strip()
        if combo == self._combo and (self._remove is not None or not combo):
            return None
        if combo:
            try:
                keyboard.parse_hotkey(combo)
            except Exception as e:
                return f"invalid key {combo!r}: {e}"
        old = self._combo
        self.unregister()  # off before on: the same trigger may be re-hooked
        try:
            if combo:
                self._remove = _hook_combo(combo, self._on_press)
        except Exception as e:
            if old:
                self._remove = _hook_combo(old, self._on_press)
            return f"invalid key {combo!r}: {e}"
        self._combo = combo
        event(f"{self._name}_bound", combo=combo)
        return None

    def unregister(self) -> None:
        if self._remove is not None:
            self._remove()
            self._remove = None
