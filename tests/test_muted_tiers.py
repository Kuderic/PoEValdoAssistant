"""Alert tiers switched off in the settings: a listing of a muted tier is
shown in the recent listings (greyed, [off]) but never alerts - no alert
card, sound, pop-up or hotkey target."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest
from conftest import make_config
from test_jackpot import decision

from sniper.__main__ import App
from sniper.bus import AlertsChanged, Bus, ListingSeen
from sniper.config import (
    ALERT_TIERS,
    ConfigError,
    load_config,
    load_extra_overrides,
    save_scoring_overrides,
)

ROOT = Path(__file__).resolve().parent.parent

# (net, difficulty) per tier under BARS: jackpot 30/50, super 60/100,
# mega 150/150 (explicit, so each tier is reachable on its own)
BARS = (30, 50, 60, 100, 150, 150)
TIERS = {
    "normal": (10, 50),
    "jackpot": (40, 60),
    "super": (90, 80),
    "mega": (200, 100),
}


def app_with(bus: Bus) -> App:
    app = App(make_config(), bus)
    app.set_jackpot_thresholds(*BARS)
    return app


# ------------------------------------------------------------------ config


def test_every_tier_alerts_by_default(tmp_path):
    assert load_config(ROOT / "config.yaml", state_dir=tmp_path).alerts.muted_tiers == ()


def test_the_panel_choice_round_trips_and_wins(tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    save_scoring_overrides(make_config().mod_scoring, path, muted_tiers=("normal", "jackpot"))
    assert load_extra_overrides(path) == {"muted_tiers": ["normal", "jackpot"]}
    alerts = load_config(ROOT / "config.yaml", state_dir=tmp_path).alerts
    assert alerts.muted_tiers == ("normal", "jackpot")


def test_switching_every_tier_back_on_is_saved_too(tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    save_scoring_overrides(make_config().mod_scoring, path, muted_tiers=())
    assert load_extra_overrides(path) == {"muted_tiers": []}


@pytest.mark.parametrize("body", ["muted_tiers: [gold]", "muted_tiers: normal"])
def test_an_unknown_tier_is_refused(tmp_path, body):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"alerts:\n  {body}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="muted_tiers"):
        load_config(cfg, defaults=ROOT / "config.yaml", state_dir=tmp_path)


# --------------------------------------------------------------------- app


def drained(bus: Bus) -> list:
    return bus.drain()


@pytest.mark.parametrize("tier", list(TIERS))
def test_a_muted_tier_is_listed_but_never_alerts(tier):
    bus = Bus()
    app = app_with(bus)
    app.set_muted_tiers((tier,))
    net, difficulty = TIERS[tier]
    d = decision("L1", net, difficulty)
    assert app.store.tier_of(d) == tier
    app._on_decision(d)
    events = drained(bus)
    (seen,) = [e for e in events if isinstance(e, ListingSeen)]
    assert seen.entry.verdict == "muted"
    assert not (seen.entry.jackpot or seen.entry.super_jackpot or seen.entry.mega_jackpot)
    assert not [e for e in events if isinstance(e, AlertsChanged)]
    assert len(app.store) == 0  # nothing for the hotkey to take


def test_the_other_tiers_still_alert():
    bus = Bus()
    app = App(make_config(), bus)
    app.set_muted_tiers(("normal",))
    app._on_decision(decision("J1", *TIERS["jackpot"]))
    assert len(app.store) == 1
    assert [e for e in drained(bus) if isinstance(e, AlertsChanged)]


def test_a_search_outside_valdo_is_not_a_tier():
    from dataclasses import replace

    app = App(make_config(), Bus())
    app.set_muted_tiers(ALERT_TIERS)
    d = decision("G1", *TIERS["normal"])
    d = replace(d, listing=replace(d.listing, generic=True))
    app._on_decision(d)
    assert len(app.store) == 1


# ----------------------------------------------------------------- overlay


@pytest.fixture
def overlay(tmp_path):
    from sniper.overlay import Overlay

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    sent: list[tuple] = []
    ov = Overlay(
        root,
        Bus(),
        make_config(),
        on_muted_tiers=sent.append,
        overrides_path=tmp_path / "scoring_overrides.yaml",
    )
    yield ov, sent, tmp_path / "scoring_overrides.yaml"
    root.destroy()


def test_the_panel_switches_a_tier_off_and_on_saving_each_time(overlay):
    ov, sent, path = overlay
    ov._open_tuning()
    btns, toggle = ov._tier_toggles
    assert list(btns) == list(ALERT_TIERS)
    assert all(b.cget("text").startswith("☑") for b in btns.values())
    toggle("normal")
    assert btns["normal"].cget("text") == "☐ Normal"
    assert sent == [("normal",)]
    assert load_extra_overrides(path)["muted_tiers"] == ["normal"]
    toggle("mega")
    toggle("normal")
    assert sent[-1] == ("mega",)
    assert load_extra_overrides(path)["muted_tiers"] == ["mega"]


def test_a_muted_row_is_greyed_with_off(overlay):
    from sniper.bus import FeedEntry
    from sniper.overlay import FAINT

    ov, _sent, _path = overlay
    ov._bus.put(ListingSeen(FeedEntry("L1", "Foil Nimis", 80, "divine", 80.0, 50, "muted")))
    ov._drain_once()
    row = ov._feed_rows[0]
    assert "[off]" in row.cget("text")
    assert row.cget("fg") == FAINT
