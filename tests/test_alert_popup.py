"""The alert pop-up: a minimized overlay comes back on an alert, cropped to
its alert card and see-through, without taking focus from the game. The
teleport hotkey (or a travel, or a click into it) opens the whole window;
the close key (Esc by default) minimizes it again."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import keyboard
import pytest
from conftest import make_config
from keyboard import KEY_DOWN, KEY_UP, KeyboardEvent
from test_jackpot_sound import view

from sniper import overlay as overlay_mod
from sniper.bus import AlertsChanged, Bus, HotkeyPressed, PopupKeyPressed, Traveled
from sniper.config import (
    ConfigError,
    PopupConfig,
    load_config,
    load_extra_overrides,
    save_scoring_overrides,
)
from sniper.hotkey import KeyHook, trigger_key
from sniper.overlay import MIN_SIZE, POPUP_GAP, Overlay

ROOT = Path(__file__).resolve().parent.parent
FULL = (515, 800)


# ------------------------------------------------------------------ config


def test_on_by_default_at_80_percent_closed_with_esc(tmp_path):
    assert PopupConfig() == PopupConfig(enabled=True, opacity=0.8, dismiss_key="esc")
    shipped = load_config(ROOT / "config.yaml", state_dir=tmp_path).popup
    assert shipped == PopupConfig(enabled=True, opacity=0.8, dismiss_key="esc")


def test_the_panel_settings_round_trip_and_win(tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    save_scoring_overrides(
        make_config().mod_scoring, path, popup_enabled=False, popup_opacity=0.5, popup_key=""
    )
    assert load_extra_overrides(path) == {
        "popup_enabled": False,
        "popup_opacity": 0.5,
        "popup_key": "",
    }
    popup = load_config(ROOT / "config.yaml", state_dir=tmp_path).popup
    assert popup == PopupConfig(enabled=False, opacity=0.5, dismiss_key="")


@pytest.mark.parametrize("opacity", [0.1, 1.5, "lots"])
def test_an_unreadable_opacity_is_refused(tmp_path, opacity):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"popup:\n  opacity: {opacity}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="popup.opacity"):
        load_config(cfg, defaults=ROOT / "config.yaml", state_dir=tmp_path)


# ------------------------------------------------------------- close key


@pytest.fixture
def listener(monkeypatch):
    """keyboard's real listener with only the OS hook left out (as in
    test_hotkey_hook)."""
    keyboard._listener.init()
    monkeypatch.setattr(keyboard._listener, "start_if_necessary", lambda: None)
    keyboard._pressed_events.clear()
    yield keyboard._listener
    keyboard._pressed_events.clear()


def tap(listener, name: str) -> None:
    code = keyboard.key_to_scan_codes(name)[0]
    for kind in (KEY_DOWN, KEY_UP):
        ev = KeyboardEvent(kind, code, name=name)
        listener.direct_callback(ev)
        listener.pre_process_event(ev)


def test_the_close_key_fires_once_per_press_and_rebinds(listener):
    presses: list[int] = []
    hook = KeyHook("esc", lambda: presses.append(1))
    tap(listener, "esc")
    assert presses == [1]
    assert hook.rebind("f2") is None
    tap(listener, "esc")
    tap(listener, "f2")
    assert presses == [1, 1] and hook.combo == "f2"
    assert "invalid key" in hook.rebind("notakey")
    tap(listener, "f2")  # the old key stays
    assert len(presses) == 3
    assert hook.rebind("") is None  # no key at all
    tap(listener, "f2")
    assert len(presses) == 3
    hook.unregister()


def test_two_combos_on_one_key_are_spotted():
    assert trigger_key("t") == trigger_key("ctrl+alt+t")
    assert trigger_key("esc") != trigger_key("`")
    assert trigger_key("notakey") is None


# ----------------------------------------------------------------- overlay


@pytest.fixture
def make_ov(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    win = SimpleNamespace(state="iconic", raised=0, minimized=0, geometry=[])
    # stand-ins for the window manager: the real calls would put a window
    # over the game the tests run beside
    monkeypatch.setattr(root, "state", lambda: win.state)
    real_geometry = root.geometry

    def geometry(spec=None):
        if spec is None:
            return f"{FULL[0]}x{FULL[1]}+40+40"
        win.geometry.append(spec)
        return real_geometry(spec)

    monkeypatch.setattr(root, "geometry", geometry)

    def raised(hwnd):
        win.raised += 1
        win.state = "normal"
        return True

    def minimized(hwnd):
        win.minimized += 1
        win.state = "iconic"
        return True

    monkeypatch.setattr(overlay_mod, "raise_without_focus", raised)
    monkeypatch.setattr(overlay_mod, "minimize_without_focus", minimized)

    def build(overrides_path=None, on_popup_key=None, **popup):
        config = replace(make_config(), popup=PopupConfig(**popup))
        config = replace(config, hotkey=replace(config.hotkey, raise_overlay=False))
        ov = Overlay(root, Bus(), config, overrides_path=overrides_path, on_popup_key=on_popup_key)
        ov.win = win
        return ov

    yield build
    root.destroy()


def drain(ov, *events) -> None:
    for ev in events:
        ov._bus.put(ev)
    ov._drain_once()


def arrive(ov, lid="L1") -> None:
    v = view(lid)
    drain(ov, AlertsChanged((v,), new_alert=True, new_view=v))


def card_bottom(ov) -> int:
    """Where the alert card ends, worked out independently of the overlay:
    everything packed above it, plus its own height."""
    ov._root.update_idletasks()
    slaves = ov._root.pack_slaves()
    above = slaves[: slaves.index(ov._card)]
    return (
        sum(w.winfo_reqheight() for w in above)
        + 8  # the card's top padding
        + ov._card.winfo_reqheight()
    )


def popped(ov) -> bool:
    return ov.popup_showing.is_set()


def test_an_alert_pops_a_minimized_window_up_as_its_alert_card(make_ov):
    ov = make_ov()
    arrive(ov)
    assert popped(ov)
    assert ov.win.raised == 1  # brought back without activating (no focus)
    assert float(ov._root.attributes("-alpha")) == pytest.approx(0.8)
    assert ov.win.geometry[-1] == f"{FULL[0]}x{card_bottom(ov) + POPUP_GAP}"
    assert ov._root.minsize()[1] < MIN_SIZE[1]  # the cut can go below the usual minimum


def test_nothing_pops_when_the_window_is_on_screen_or_it_is_off(make_ov):
    on_screen = make_ov()
    on_screen.win.state = "normal"
    arrive(on_screen)
    assert not popped(on_screen) and on_screen.win.raised == 0
    off = make_ov(enabled=False)
    off.win.state = "iconic"
    arrive(off)
    assert not popped(off) and off.win.raised == 0


def test_the_teleport_hotkey_opens_the_whole_window(make_ov):
    ov = make_ov()
    arrive(ov)
    drain(ov, HotkeyPressed())
    assert not popped(ov)
    assert ov.win.geometry[-1] == f"{FULL[0]}x{FULL[1]}"
    assert float(ov._root.attributes("-alpha")) == 1.0
    assert ov._root.minsize() == MIN_SIZE


def test_a_travel_clicked_in_the_popup_opens_it_too(make_ov):
    ov = make_ov()
    arrive(ov)
    drain(ov, Traveled(view("L1")))
    assert not popped(ov)
    assert ov.win.geometry[-1] == f"{FULL[0]}x{FULL[1]}"


def test_the_close_key_minimizes_it_again_at_full_size(make_ov):
    ov = make_ov()
    arrive(ov)
    drain(ov, PopupKeyPressed())
    assert ov.win.minimized == 1  # never by activating another window
    assert not popped(ov)
    # back to full size and solid for when the user opens it by hand
    assert ov.win.geometry[-1] == f"{FULL[0]}x{FULL[1]}"
    assert float(ov._root.attributes("-alpha")) == 1.0
    arrive(ov, "L2")  # and the next alert pops up again
    assert popped(ov) and ov.win.raised == 2


def test_the_close_key_does_nothing_without_a_popup(make_ov):
    ov = make_ov()
    ov.win.state = "normal"
    drain(ov, PopupKeyPressed())
    assert ov.win.minimized == 0


def test_minimizing_by_hand_ends_the_popup(make_ov):
    ov = make_ov()
    arrive(ov)
    ov._popup_watch()
    assert popped(ov)  # still up
    ov.win.state = "iconic"  # the title bar's minimize button
    ov._popup_watch()
    assert not popped(ov)
    assert ov.win.geometry[-1] == f"{FULL[0]}x{FULL[1]}"


def test_the_full_size_waits_until_it_is_minimized(make_ov, monkeypatch):
    """The real minimize lands a moment later (it runs off the Tk thread);
    restoring the size before then would flash the whole window up."""
    ov = make_ov()
    arrive(ov)
    pending: list[int] = []
    monkeypatch.setattr(overlay_mod, "minimize_without_focus", lambda h: pending.append(h) or True)
    drain(ov, PopupKeyPressed())
    assert pending and not popped(ov)  # the key acts once
    assert ov.win.geometry[-1] != f"{FULL[0]}x{FULL[1]}"
    ov.win.state = "iconic"  # it lands
    ov._popup_watch()
    assert ov.win.geometry[-1] == f"{FULL[0]}x{FULL[1]}"
    assert ov._popup_full is None


def test_clicking_into_the_popup_opens_the_whole_window(make_ov):
    ov = make_ov()
    arrive(ov)
    ov._popup_focused(SimpleNamespace(widget=ov._root))
    assert not popped(ov)


def test_a_new_alert_recrops_an_open_popup(make_ov):
    ov = make_ov()
    arrive(ov)
    ov.win.geometry.clear()
    arrive(ov, "L2")
    ov._root.update()  # the re-crop waits for the layout pass
    assert ov.win.geometry == [f"{FULL[0]}x{card_bottom(ov) + POPUP_GAP}"]
    assert ov.win.raised == 1  # already up: not raised again


# ---------------------------------------------------------------- settings


def test_the_panel_toggle_and_fields_apply_and_save(make_ov, tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    keys: list[str] = []
    ov = make_ov(overrides_path=path, on_popup_key=lambda k: keys.append(k))
    ov._open_tuning()
    button, click = ov._popup_toggle
    assert button.cget("text") == "☑ On"
    click()
    assert button.cget("text") == "☐ Off" and ov._popup_enabled is False
    assert load_extra_overrides(path)["popup_enabled"] is False
    opacity, key = ov._popup_fields
    assert opacity.get() == "80" and key.get() == "esc"
    opacity.delete(0, "end")
    opacity.insert(0, "55")
    key.delete(0, "end")
    key.insert(0, "f3")
    ov._apply_settings_now()
    assert ov._popup_opacity == pytest.approx(0.55) and ov._popup_key == "f3"
    assert keys == ["f3"]  # rebinds the global key
    saved = load_extra_overrides(path)
    assert saved["popup_opacity"] == pytest.approx(0.55) and saved["popup_key"] == "f3"


@pytest.mark.parametrize(
    ("opacity", "key", "error"),
    [
        ("10", "esc", "Pop-up opacity must be 20-100"),
        ("80", "`", "must not be the hotkey's key"),  # the default hotkey's key
        ("80", "notakey", "is not a key"),
    ],
)
def test_the_panel_refuses_bad_popup_values(make_ov, opacity, key, error):
    keys: list[str] = []
    ov = make_ov(on_popup_key=lambda k: keys.append(k))
    ov._open_tuning()
    combo = next(
        w for w in _walk(ov._tune_window) if isinstance(w, tk.Entry) and w.get() == "ctrl+alt+t"
    )
    combo.delete(0, "end")
    combo.insert(0, "`")
    box_opacity, box_key = ov._popup_fields
    box_opacity.delete(0, "end")
    box_opacity.insert(0, opacity)
    box_key.delete(0, "end")
    box_key.insert(0, key)
    ov._apply_settings_now()
    note = next(
        w for w in _walk(ov._tune_window) if isinstance(w, tk.Label) and error in w.cget("text")
    )
    assert note is not None
    assert keys == [] and ov._popup_key == "esc" and ov._popup_opacity == 0.8


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)
