"""The tabs pill names WHICH tabs are stale or run an old userscript, not just
how many - "2 stale" left the user checking every tab for the ones to fix."""

from __future__ import annotations

import tkinter as tk

import pytest
from conftest import make_config

from sniper.bus import Bus, TabsChanged
from sniper.overlay import Overlay, _tab_names
from sniper.server import EXPECTED_USERSCRIPT_VERSION as CURRENT


def tab(reward=None, *, age=5, version=CURRENT, kind="valdo"):
    return {
        "search_reward": reward,
        "hello_age_s": age,
        "version": version,
        "search_kind": kind,
    }


def test_names_drop_the_foil_prefix_and_count_repeats():
    picked = [tab("Foil Mageblood"), tab("Foil Headhunter"), tab("Foil Mageblood")]
    assert _tab_names(picked, 5) == "Headhunter, Mageblood ×2"


def test_tabs_without_a_reward_still_get_a_name():
    assert _tab_names([tab(kind="any")], 3) == "other search"
    assert _tab_names([tab()], 3) == "unknown tab"


def test_every_tab_reads_as_all():
    tabs = [tab("Foil Mageblood"), tab("Foil Nimis")]
    assert _tab_names(tabs, 2) == "all"
    assert _tab_names(tabs[:1], 1) == "Mageblood"  # a lone tab keeps its name


def test_the_pill_names_stale_and_outdated_tabs():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    bus = Bus()
    ov = Overlay(root, bus, make_config())
    tabs = (
        tab("Foil Mageblood", age=120),
        tab("Foil Nimis", version="0.8.4"),
        tab("Foil Headhunter"),
    )
    bus.put(TabsChanged(tabs))
    ov._drain_once()
    assert ov._tabs_label._label.cget("text") == "3 tabs · stale: Mageblood · update script: Nimis"
    root.destroy()
