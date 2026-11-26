"""Two things a hotkey press shows: the overlay can come to the front (a
setting), and a failed travel lands in a red bar at the top of the window."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest
from conftest import make_config

from sniper import overlay as overlay_mod
from sniper.bus import Bus, ClickOutcome, HotkeyPressed
from sniper.config import load_config, load_extra_overrides
from sniper.overlay import Overlay

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def make_ov(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    raised: list[int] = []
    monkeypatch.setattr(
        overlay_mod, "raise_without_focus", lambda hwnd: raised.append(hwnd) or True
    )

    def build(raise_overlay=False, overrides_path=None):
        from dataclasses import replace

        config = make_config()
        config = replace(config, hotkey=replace(config.hotkey, raise_overlay=raise_overlay))
        ov = Overlay(root, Bus(), config, overrides_path=overrides_path)
        ov.raised = raised
        return ov

    yield build
    root.destroy()


def drain(ov, *events) -> None:
    for ev in events:
        ov._bus.put(ev)
    ov._drain_once()


def test_the_setting_is_on_by_default_and_round_trips(tmp_path):
    from sniper.config import HotkeyConfig, save_scoring_overrides

    assert HotkeyConfig().raise_overlay is True
    assert load_config(ROOT / "config.yaml", state_dir=tmp_path).hotkey.raise_overlay is True
    # switching it off in the panel is saved, and wins over the default
    path = tmp_path / "scoring_overrides.yaml"
    save_scoring_overrides(make_config().mod_scoring, path, raise_overlay=False)
    assert load_extra_overrides(path) == {"raise_overlay": False}
    assert load_config(ROOT / "config.yaml", state_dir=tmp_path).hotkey.raise_overlay is False


def test_a_press_raises_the_overlay_only_when_set(make_ov):
    off = make_ov(raise_overlay=False)
    drain(off, HotkeyPressed())
    assert off.raised == []
    on = make_ov(raise_overlay=True)
    drain(on, HotkeyPressed())
    assert len(on.raised) == 1


def test_the_settings_toggle_applies_and_saves(make_ov, tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    ov = make_ov(overrides_path=path)
    ov._open_tuning()
    button, click = ov._raise_toggle
    assert button.cget("text") == "☐ Off"
    click()
    assert button.cget("text") == "☑ On"
    assert ov._raise_on_hotkey is True
    assert load_extra_overrides(path)["raise_overlay"] is True
    drain(ov, HotkeyPressed())
    assert len(ov.raised) == 1


def test_a_failed_travel_shows_at_the_top_in_plain_words(make_ov):
    ov = make_ov()
    drain(ov, ClickOutcome("x", False, "item_no_longer_available"))
    assert ov._fail_bar.winfo_manager() == "pack"
    assert ov._fail_bar.cget("text") == "✖  TRAVEL FAILED: item already sold"
    order = ov._root.pack_slaves()
    assert order.index(ov._fail_bar) < order.index(ov._card)


def test_it_stays_above_a_reload_bar_shown_later(make_ov):
    ov = make_ov()
    drain(ov, ClickOutcome("x", False, "row_gone"))
    ov._show_reload(["Mageblood (+2.7s lag)"])
    order = ov._root.pack_slaves()
    assert order.index(ov._fail_bar) < order.index(ov._reload_bar)


def test_the_bar_clears_on_the_next_press_or_a_travel_that_works(make_ov):
    ov = make_ov()
    drain(ov, ClickOutcome("x", False, "row_gone"))
    drain(ov, HotkeyPressed())
    assert ov._fail_bar.winfo_manager() == ""
    drain(ov, ClickOutcome("y", False, "tab gone - travel not sent"))
    assert ov._fail_bar.cget("text") == "✖  TRAVEL FAILED: tab gone - travel not sent"
    drain(ov, ClickOutcome("z", True, ""))
    assert ov._fail_bar.winfo_manager() == ""


def test_the_bar_clears_itself_after_a_while(make_ov, monkeypatch):
    monkeypatch.setattr(overlay_mod, "TRAVEL_FAIL_S", 0.05)
    ov = make_ov()
    drain(ov, ClickOutcome("x", False, "row_gone"))
    ov._root.after(150, ov._root.quit)
    ov._root.mainloop()
    assert ov._fail_bar.winfo_manager() == ""
