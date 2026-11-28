"""The map's layout (its Map Area property, e.g. "Strand") travels from the
userscript to the alert card, where it leads the line under the reward."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace

import pytest
from conftest import make_config, make_listing_frame
from test_jackpot_sound import view

from sniper import margin
from sniper.alerts import AlertStore
from sniper.bus import AlertsChanged, Bus
from sniper.models import Listing, parse_frame
from sniper.modrules import ModRules
from sniper.overlay import Overlay
from sniper.prices import PriceBook


@pytest.mark.parametrize(
    ("sent", "parsed"),
    [("Strand", "Strand"), ("  Mesa ", "Mesa"), ("", None), (None, None), (42, None)],
)
def test_the_frame_carries_the_map_area(sent, parsed):
    listing = parse_frame(make_listing_frame(map_area=sent))
    assert isinstance(listing, Listing)
    assert listing.map_area == parsed


def test_an_older_userscript_without_it_still_parses():
    frame = make_listing_frame()
    frame.pop("map_area", None)
    listing = parse_frame(frame)
    assert isinstance(listing, Listing) and listing.map_area is None


def test_the_alert_view_carries_it():
    config = make_config()
    listing = parse_frame(
        make_listing_frame(price={"amount": 100, "currency": "divine"}, map_area="Strand")
    )
    decision = margin.evaluate(listing, PriceBook(config), config.thresholds, ModRules([]))
    store = AlertStore(expiry_s=10)
    assert store.insert(decision)
    assert store.views()[0].map_area == "Strand"


@pytest.fixture
def ov():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield Overlay(root, Bus(), make_config())
    root.destroy()


def show(ov, v) -> str:
    ov._bus.put(AlertsChanged((v,), new_alert=True, new_view=v))
    ov._drain_once()
    return ov._subtitle.cget("text")


def test_the_alert_leads_with_the_layout(ov):
    text = show(ov, replace(view("L1"), map_area="Strand"))
    assert text.startswith("Map: Strand  ·  Reward worth 161d")


def test_without_a_layout_the_line_is_as_before(ov):
    assert show(ov, view("L1")).startswith("Reward worth 161d")


def test_the_layout_shows_even_before_the_reward_is_priced(ov):
    text = show(ov, replace(view("L1"), map_area="Strand", reference_amount=0))
    assert text == "Map: Strand"
