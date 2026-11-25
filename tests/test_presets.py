"""Search presets: one click opens a proven set of live searches.

The trade site's search ids are the gzipped query itself, so each preset id
is decoded here to check it searches exactly what its label says.
"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import json
import tkinter as tk

import pytest
from conftest import make_config

from sniper import presets
from sniper.bus import Bus, PriceStatus, TabsChanged
from sniper.overlay import Overlay
from sniper.presets import HIGH_PROFIT, live_url, still_closed


def decode(search_id: str) -> dict:
    raw = base64.urlsafe_b64decode(search_id + "=" * (-len(search_id) % 4))
    return json.loads(gzip.decompress(raw))


def test_high_profit_is_the_six_rewards():
    assert HIGH_PROFIT.name == "High profit"
    assert HIGH_PROFIT.rewards == (
        "Foil Headhunter",
        "Foil Mageblood",
        "Foil Nimis",
        "Foil Progenesis",
        "Foil Sublime Vision",
        "Foil Wine of the Prophet",
    )


@pytest.mark.parametrize("preset", presets.PRESETS, ids=lambda p: p.name)
def test_every_id_searches_instant_buyout_maps_with_its_reward(preset):
    for reward, search_id in preset.searches:
        query = decode(search_id)
        assert query["status"] == {"option": "securable"}
        reward_filter = query["filters"]["map_filters"]["filters"]["map_completion_reward"]
        assert reward_filter == {"option": reward.removeprefix("Foil ")}
        # nothing else narrows it: no stat filters, no other filter groups
        assert query["stats"] == [{"type": "and", "filters": []}]
        assert set(query["filters"]) == {"map_filters"}


def test_live_url_names_the_league_and_ends_on_the_live_page():
    sid = HIGH_PROFIT.searches[0][1]
    assert live_url("Covens", sid) == f"https://www.pathofexile.com/trade/search/Covens/{sid}/live"
    assert "/Hardcore%20Covens/" in live_url("Hardcore Covens", sid)


def test_searches_already_open_are_skipped():
    todo = still_closed(HIGH_PROFIT, {"Foil Mageblood", "Foil Nimis", "Foil Kalandra's Touch"})
    assert [r for r, _ in todo] == [
        "Foil Headhunter",
        "Foil Progenesis",
        "Foil Sublime Vision",
        "Foil Wine of the Prophet",
    ]
    assert still_closed(HIGH_PROFIT, set(HIGH_PROFIT.rewards)) == []


# ---------------------------------------------------------------- overlay


@pytest.fixture
def overlay(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    opened: list[str] = []
    monkeypatch.setattr("sniper.overlay.browser.open_url", lambda url: opened.append(url) or "x")
    monkeypatch.setattr(presets, "TAB_GAP_MS", 0)
    bus = Bus()
    # "auto": the league comes from poe.ninja's PriceStatus, as shipped
    ov = Overlay(root, bus, dataclasses.replace(make_config(), league="auto"))
    ov.opened = opened
    ov.bus = bus
    yield ov
    root.destroy()


def pump(ov) -> None:
    ov._drain_once()
    ov._root.update()


def test_one_click_opens_every_live_search(overlay):
    overlay.bus.put(PriceStatus("live", "Covens"))
    pump(overlay)
    msg = overlay._open_preset(HIGH_PROFIT)
    pump(overlay)
    assert msg == "Opening 6 live searches in Chrome (Covens)."
    assert overlay.opened == [live_url("Covens", sid) for _, sid in HIGH_PROFIT.searches]


def test_open_tabs_are_skipped_and_a_repeat_click_opens_nothing(overlay):
    overlay.bus.put(PriceStatus("live", "Covens"))
    overlay.bus.put(
        TabsChanged(({"search_reward": "Foil Mageblood"}, {"search_reward": "Foil Nimis"}))
    )
    pump(overlay)
    assert overlay._open_preset(HIGH_PROFIT).startswith("Opening 4 live searches")
    pump(overlay)
    assert len(overlay.opened) == 4
    # the new tabs have not connected yet: a second click must not double them
    assert overlay._open_preset(HIGH_PROFIT).startswith("Already opening")
    pump(overlay)
    assert len(overlay.opened) == 4


def test_nothing_opens_while_the_league_is_unknown(overlay):
    msg = overlay._open_preset(HIGH_PROFIT)
    pump(overlay)
    assert msg.startswith("League not known yet")
    assert overlay.opened == []


def test_an_explicit_league_in_config_needs_no_poe_ninja(overlay):
    overlay._config = dataclasses.replace(overlay._config, league="Standard")
    overlay._open_preset(HIGH_PROFIT)
    pump(overlay)
    assert overlay.opened and all("/Standard/" in u for u in overlay.opened)


def test_all_open_says_so(overlay):
    overlay.bus.put(PriceStatus("live", "Covens"))
    overlay.bus.put(TabsChanged(tuple({"search_reward": r} for r in HIGH_PROFIT.rewards)))
    pump(overlay)
    assert overlay._open_preset(HIGH_PROFIT) == "All 6 High profit searches are already open."
    assert overlay.opened == []
