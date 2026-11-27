"""Minimum EV for void maps (thresholds.void_min_net).

A map with the VOID mod deletes the character on death; the user wants it
to clear a higher EV bar than other maps before it alerts at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.config import (
    SCORING_OVERRIDES_NAME,
    ConfigError,
    ModScoringConfig,
    load_config,
    save_scoring_overrides,
)
from sniper.models import parse_frame
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook

VOID = "Players who Die in area are sent to the Void"


def decide(amount: float, mods: list[str], **thresholds):
    # Mageblood's manual price is 200 divine; no scoring rules, so every map
    # is difficulty 25 and running cost 0: EV = 200 - amount
    config = make_config(min_yield=0.0)
    config = replace(config, thresholds=replace(config.thresholds, **thresholds))
    lst = parse_frame(make_listing_frame(price={"amount": amount, "currency": "divine"}, mods=mods))
    return margin.evaluate(
        lst, PriceBook(config), config.thresholds, ModRules([]), ModScoring(config.mod_scoring)
    )


def test_a_void_map_below_the_void_bar_does_not_alert():
    d = decide(190, [VOID], min_net=5, void_min_net=20)  # EV 10
    assert d.net_div == pytest.approx(10)
    assert d.verdict == "below_threshold"


def test_a_void_map_at_the_void_bar_alerts():
    assert decide(180, [VOID], min_net=5, void_min_net=20).verdict == "alert"  # EV 20


def test_other_maps_keep_the_normal_bar():
    assert decide(190, ["Area contains The Feared"], min_net=5, void_min_net=20).verdict == "alert"


def test_zero_or_a_lower_void_bar_changes_nothing():
    assert decide(190, [VOID], min_net=5, void_min_net=0).verdict == "alert"
    # the normal bar still applies to void maps when it is the higher one
    assert decide(190, [VOID], min_net=15, void_min_net=10).verdict == "below_threshold"


def test_the_void_mod_is_matched_whatever_the_case():
    assert margin.is_void_map(["players who die in area are SENT TO THE VOID"])
    assert not margin.is_void_map(["Area contains The Feared"])


def test_config_reads_it_and_the_panels_value_wins(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("thresholds:\n  void_min_net: 25\n", encoding="utf-8")
    assert load_config(cfg).thresholds.void_min_net == 25
    save_scoring_overrides(
        ModScoringConfig(base_default=25.0, rules=()),
        tmp_path / SCORING_OVERRIDES_NAME,
        void_min_net=40,
    )
    assert load_config(cfg).thresholds.void_min_net == 40


def test_it_defaults_to_off():
    assert load_config("config.yaml").thresholds.void_min_net == 0


def test_a_negative_void_bar_is_a_config_error(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("thresholds:\n  void_min_net: -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="void_min_net"):
        load_config(cfg)


def test_the_settings_panel_applies_and_saves_it(tmp_path):
    import tkinter as tk

    from sniper.bus import Bus
    from sniper.overlay import Overlay

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        calls = []

        def on_change(*args):
            calls.append(args)
            return None

        overrides = tmp_path / SCORING_OVERRIDES_NAME
        ov = Overlay(
            root, Bus(), make_config(), on_settings_change=on_change, overrides_path=overrides
        )
        ov._open_tuning()
        box = next(
            w
            for w in _walk(ov._tune_window)
            if isinstance(w, tk.Entry)
            and w.get() == "0"
            and _label_of(w) == "Void maps: alert at EV ≥ ⓘ"
        )
        box.delete(0, "end")
        box.insert(0, "30")
        ov._apply_settings_now()
        assert calls[-1][-1] == 30
        assert ov._current_void_min_net == 30
        cfg = tmp_path / "config.yaml"
        cfg.write_text("", encoding="utf-8")
        assert load_config(cfg).thresholds.void_min_net == 30
    finally:
        root.destroy()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _label_of(entry):
    """The row label beside a settings entry (same grid row, column 0)."""
    info = entry.grid_info()
    for sib in entry.master.grid_slaves(row=info["row"], column=0):
        return sib.cget("text")
    return None
