"""Where the item sits in the seller's shop tab.

The fetch payload carries listing.stash {name, x, y}; the userscript
forwards it, and the alert card draws it so the item can be clicked in
Faustus' shop the moment the hideout loads.
"""

from __future__ import annotations

import pytest
from conftest import make_listing_frame

from sniper.alerts import AlertStore
from sniper.models import Listing, StashSpot, parse_frame
from sniper.overlay import _stash_spot


def parse(**overrides) -> Listing:
    listing = parse_frame(make_listing_frame(**overrides))
    assert isinstance(listing, Listing), listing
    return listing


def test_the_stash_spot_is_parsed():
    assert parse(stash={"name": "7", "x": 8, "y": 8}).stash == StashSpot("7", 8, 8)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "7",
        {"name": "7"},
        {"name": "7", "x": "8", "y": 8},
        {"name": "7", "x": 8.5, "y": 8},
        {"name": "7", "x": True, "y": 8},
        {"name": "7", "x": -1, "y": 8},
        {"name": "7", "x": 24, "y": 0},  # off even a quad tab
    ],
)
def test_a_missing_or_bad_stash_is_dropped_not_fatal(raw):
    """A position we cannot draw is left out; the listing still alerts."""
    assert parse(stash=raw).stash is None


def test_a_nameless_tab_keeps_its_position():
    assert parse(stash={"x": 0, "y": 11}).stash == StashSpot("", 0, 11)


def test_older_userscripts_without_stash_still_parse():
    frame = make_listing_frame()
    frame.pop("stash", None)
    assert parse_frame(frame).stash is None


def test_the_alert_view_carries_it_to_the_overlay():
    from conftest import make_config

    from sniper import margin
    from sniper.modrules import ModRules
    from sniper.prices import PriceBook

    config = make_config(min_yield=1.0)
    lst = parse(stash={"name": "Shop", "x": 3, "y": 10}, price={"amount": 10, "currency": "divine"})
    d = margin.evaluate(lst, PriceBook(config), config.thresholds, ModRules([]))
    store = AlertStore(expiry_s=60)
    store.insert(d)
    assert store.views()[0].stash == ("Shop", 3, 10)


@pytest.mark.parametrize(
    ("x", "y", "side", "words"),
    [
        (0, 0, 12, "top left"),
        (11, 0, 12, "top right"),
        (0, 11, 12, "bottom left"),
        (8, 8, 12, "bottom right"),  # the real capture in tests/fixtures
        (5, 5, 12, "centre"),
        (5, 0, 12, "top centre"),
        (0, 6, 12, "middle left"),
        (3, 3, 12, "top left"),
        (4, 4, 12, "centre"),
        (20, 2, 24, "top right"),  # only fits a quad tab
        (12, 12, 24, "centre"),
        (23, 23, 24, "bottom right"),
    ],
)
def test_the_spot_in_words(x, y, side, words):
    assert _stash_spot(x, y) == (side, words)
