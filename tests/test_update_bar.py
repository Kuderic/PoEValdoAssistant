"""A newer release is announced in a bar at the top of the window."""

from __future__ import annotations

import tkinter as tk

import pytest
from conftest import make_config

from sniper import overlay as overlay_mod
from sniper.bus import Bus, ClickOutcome, UpdateStatus
from sniper.overlay import Overlay, _update_banner

URL = "https://github.com/Kuderic/PoEValdoAssistant/releases/tag/v9.0.0"


@pytest.fixture
def ov():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    o = Overlay(root, Bus(), make_config())
    o.opened = []
    o._open_link = o.opened.append
    yield o
    root.destroy()


def drain(o, *events) -> None:
    for ev in events:
        o._bus.put(ev)
    o._drain_once()


def test_every_announceable_state_has_a_headline():
    for state in ("available", "downloading", "ready", "failed"):
        title, hint, _bg = _update_banner(UpdateStatus(state, "9.0.0", URL), packaged=True)
        assert "v9.0.0" in title and hint
    assert _update_banner(UpdateStatus("installed", "9.0.0", ""), packaged=True) is None
    source = _update_banner(UpdateStatus("available", "9.0.0", URL), packaged=False)
    assert "git pull" in source[1]


def test_a_new_version_shows_at_the_very_top(ov):
    drain(ov, UpdateStatus("available", "9.0.0", URL))
    assert ov._update_bar.winfo_manager() == "pack"
    assert ov._update_title.cget("text") == "↑  NEW VERSION v9.0.0 IS OUT"
    # above a travel failure and a reload warning, whichever came first
    drain(ov, ClickOutcome("x", False, "row_gone"))
    ov._show_reload(["Mageblood (+2.7s lag)"])
    order = ov._root.pack_slaves()
    bars = [order.index(b) for b in (ov._update_bar, ov._fail_bar, ov._reload_bar)]
    assert bars == sorted(bars)
    assert bars[-1] < order.index(ov._searches_label)


def test_the_slot_order_holds_when_the_update_arrives_last(ov):
    ov._show_reload(["Mageblood (+2.7s lag)"])
    drain(ov, ClickOutcome("x", False, "row_gone"))
    drain(ov, UpdateStatus("available", "9.0.0", URL))
    order = ov._root.pack_slaves()
    bars = [order.index(b) for b in (ov._update_bar, ov._fail_bar, ov._reload_bar)]
    assert bars == sorted(bars)


def test_clicking_opens_the_release_page(ov):
    drain(ov, UpdateStatus("available", "9.0.0", URL))
    ov._update_clicked()
    assert ov.opened == [URL]


def test_dismissed_until_the_next_state_or_version(ov):
    drain(ov, UpdateStatus("downloading", "9.0.0", URL))
    ov._dismiss_update()
    assert ov._update_bar.winfo_manager() == ""
    drain(ov, UpdateStatus("downloading", "9.0.0", URL))  # same news: stays hidden
    assert ov._update_bar.winfo_manager() == ""
    drain(ov, UpdateStatus("ready", "9.0.0", URL))  # news: ready to restart
    assert ov._update_bar.winfo_manager() == "pack"
    assert ov._update_title.cget("text") == "↑  UPDATE v9.0.0 READY"


def test_a_failed_download_turns_amber_and_installed_hides_it(ov):
    drain(ov, UpdateStatus("failed", "9.0.0", URL))
    assert ov._update_bar.cget("bg") == overlay_mod.WARN
    drain(ov, UpdateStatus("installed", "9.0.0", ""))
    assert ov._update_bar.winfo_manager() == ""


def test_checks_hourly_by_default():
    from pathlib import Path

    from sniper.config import load_config

    root = Path(__file__).resolve().parent.parent
    assert load_config(root / "config.yaml").updates.check_interval_hours == 1
