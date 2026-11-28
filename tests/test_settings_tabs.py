"""The settings window in tabs, and the Watching panel that replaced the
Watching line at the top of the overlay."""

from __future__ import annotations

import time
import tkinter as tk

import pytest
from conftest import make_config

from sniper.bus import Bus, RewardPrice, RewardPrices, TabsChanged
from sniper.config import load_extra_overrides
from sniper.overlay import Overlay


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield root
    root.destroy()


def shown(ov) -> list[str]:
    ov._tune_window.update_idletasks()
    return [s["name"] for s in ov._settings_sections if s["card"].winfo_manager()]


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def search_box(ov) -> tk.Entry:
    return next(w for w in walk(ov._tune_window) if isinstance(w, tk.Entry))


def test_settings_open_on_the_alerts_tab_and_switch(root):
    ov = Overlay(root, Bus(), make_config())
    ov._open_tuning()
    assert shown(ov) == ["When to alert", "Alert tiers", "Jackpots"]
    ov._settings_select("Map scoring")
    # (the test config has no risks or pairings, so just the difficulty cards)
    assert shown(ov) == ["Base difficulty", "Difficulty multipliers"]
    ov._settings_select("General")
    assert shown(ov) == ["Hotkey & sound", "Alert pop-up", "Shop marker (in game)", "Files"]


def test_the_last_tab_is_remembered_when_reopened(root):
    ov = Overlay(root, Bus(), make_config())
    ov._open_tuning()
    ov._settings_select("Searches")
    ov._tune_window.destroy()
    ov._open_tuning()
    assert shown(ov) == ["Live searches", "Reward prices"]


def test_search_looks_through_every_tab(root):
    ov = Overlay(root, Bus(), make_config())
    ov._open_tuning()
    search_box(ov).insert(0, "volume")
    (card,) = [s for s in ov._settings_sections if s["card"].winfo_manager()]
    assert card["name"] == "Hotkey & sound"
    assert card["title_label"].cget("text") == "Hotkey & sound   ·   General"
    # clearing it goes back to the tab that was open
    search_box(ov).delete(0, "end")
    assert shown(ov) == ["When to alert", "Alert tiers", "Jackpots"]


def test_a_search_with_no_match_says_so(root):
    ov = Overlay(root, Bus(), make_config())
    ov._open_tuning()
    search_box(ov).insert(0, "zzzz")
    assert shown(ov) == []
    texts = [w.cget("text") for w in walk(ov._tune_window) if isinstance(w, tk.Label)]
    assert "No setting matches “zzzz”." in texts


def test_editing_a_super_jackpot_bar_saves_it(root, tmp_path):
    """Both super jackpot boxes used to be left out of the auto-save."""
    path = tmp_path / "scoring_overrides.yaml"
    ov = Overlay(root, Bus(), make_config(), overrides_path=path)
    ov._open_tuning()
    jackpots = next(s for s in ov._settings_sections if s["name"] == "Jackpots")
    (_name, net_box, _yield_box) = next(w for t, w in jackpots["rows"] if t.startswith("super"))
    net_box.delete(0, "end")
    net_box.insert(0, "77")
    # Tk delivers a generated key event to the focused widget
    net_box.focus_force()
    root.update()
    net_box.event_generate("<KeyRelease>", keysym="7")
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert load_extra_overrides(path)["super_jackpot_min_net"] == 77.0


# ------------------------------------------------------- Watching panel


def panel_texts(ov) -> list[str]:
    return [w.cget("text") for w in walk(ov._price_panel) if isinstance(w, tk.Label)]


def test_the_watching_panel_lists_rewards_and_other_tabs(root):
    ov = Overlay(root, Bus(), make_config())
    tabs = (
        {"search_reward": "Foil Nimis"},
        {"search_reward": "Foil Mageblood"},  # tab open, no price pushed yet
        {"search_reward": None, "search_kind": "any"},
        {"search_reward": None},
    )
    for ev in (
        TabsChanged(tabs),
        RewardPrices((RewardPrice("Foil Nimis", 160.5, "divine", "trade", 240),)),
    ):
        ov._bus.put(ev)
    ov._drain_once()
    texts = panel_texts(ov)
    assert "WATCHING" in texts
    assert "Nimis" in texts and "160.5d" in texts and "4m ago" in texts
    assert "Mageblood" in texts and "pricing…" in texts  # shows before its price
    assert "⚡ 1 other live search (not Valdo maps)" in texts
    assert "1 tab still loading its search…" in texts
    assert not hasattr(ov, "_searches_label")  # the old Watching line is gone


def test_the_super_mega_bars_are_in_the_panel_and_save(root, tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    bars: list[tuple] = []
    ov = Overlay(
        root,
        Bus(),
        make_config(),
        overrides_path=path,
        on_jackpot_change=lambda *b: bars.append(b),
    )
    ov._open_tuning()
    jackpots = next(s for s in ov._settings_sections if s["name"] == "Jackpots")
    (_name, net_box, yield_box) = next(w for t, w in jackpots["rows"] if t.startswith("super mega"))
    assert (net_box.get(), yield_box.get()) == ("80", "20")
    net_box.delete(0, "end")
    net_box.insert(0, "90")
    ov._apply_settings_now()
    assert load_extra_overrides(path)["mega_jackpot_min_net"] == 90.0
    assert bars[-1][4:] == (90.0, 20.0)  # handed to the alert store too
