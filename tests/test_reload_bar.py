"""The RELOAD bar: a slow tab is named with the extra delay a reload removes."""

from __future__ import annotations

import tkinter as tk

import pytest
from conftest import make_config

from sniper.bus import Bus, TabsChanged
from sniper.overlay import Overlay, _reload_notes


def tab(reward, lag, fresh=None, slow=False):
    return {"search_reward": reward, "lag_ms": lag, "fresh_lag_ms": fresh, "slow": slow}


def test_the_extra_delay_is_against_the_freshly_loaded_page():
    tabs = [tab("Foil Mageblood", 4300, 1600, slow=True), tab("Foil Nimis", 1500, 1500)]
    assert _reload_notes(tabs) == ["Mageblood (+2.7s lag)"]


def test_without_a_fresh_figure_the_healthy_tabs_stand_in():
    tabs = [
        tab("Foil Mageblood", 4300, None, slow=True),
        tab("Foil Nimis", 1500),
        tab("Foil Headhunter", 1700),
    ]
    assert _reload_notes(tabs) == ["Mageblood (+2.6s lag)"]


def test_no_figure_at_all_still_names_the_tab():
    assert _reload_notes([tab("Foil Mageblood", None, None, slow=True)]) == ["Mageblood"]
    assert _reload_notes([tab("Foil Nimis", 1500, 1500)]) == []


def test_the_bar_shows_while_a_tab_is_slow_and_clears_after_the_reload():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    bus = Bus()
    ov = Overlay(root, bus, make_config())
    slow = tab("Foil Mageblood", 4300, 1600, slow=True)
    bus.put(TabsChanged((slow, tab("Foil Nimis", 1500, 1500))))
    ov._drain_once()
    assert ov._reload_bar.winfo_manager() == "pack"
    assert ov._reload_label.cget("text") == "⟳  RELOAD  Mageblood (+2.7s lag)"
    bus.put(TabsChanged((tab("Foil Mageblood", 1600, 1600), tab("Foil Nimis", 1500, 1500))))
    ov._drain_once()
    assert ov._reload_bar.winfo_manager() == ""
    root.destroy()
