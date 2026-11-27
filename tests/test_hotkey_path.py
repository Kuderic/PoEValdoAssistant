"""App.hotkey_pressed: the ToS-critical one-press-one-click path, tested
with the real AlertStore and a stubbed click transport / window focus."""

import asyncio

from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.__main__ import App
from sniper.bus import AlertsChanged, Bus, ClickOutcome
from sniper.models import parse_frame
from sniper.modrules import ModRules


def build_app(monkeypatch, consume: str = "all"):
    config = make_config()
    object.__setattr__(config.hotkey, "consume", consume)
    bus = Bus()
    app = App(config, bus)

    clicks: list[str] = []
    focused: list[str] = []

    async def fake_send(listing_id: str, retravel: bool = False) -> bool:
        clicks.append(f"{listing_id} (retravel)" if retravel else listing_id)
        return True

    monkeypatch.setattr(app.server, "send_click_travel", fake_send)
    monkeypatch.setattr("sniper.__main__.focus_poe_window", lambda t: focused.append(t) or True)
    return app, bus, clicks, focused


def feed(app: App, listing_id: str, amount: float) -> None:
    lst = parse_frame(
        make_listing_frame(listing_id=listing_id, price={"amount": amount, "currency": "divine"})
    )
    decision = margin.evaluate(lst, app.book, app.config.thresholds, ModRules([]))
    app._on_decision(decision)


async def test_press_clicks_best_then_second_press_noops(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "worse", 120)
    feed(app, "best", 60)

    await app.hotkey_pressed()
    assert clicks == ["best"]
    assert focused == ["Path of Exile"]

    await app.hotkey_pressed()  # consume=all: store cleared, press is a no-op
    assert clicks == ["best"]

    outcomes = [e for e in bus.drain() if isinstance(e, ClickOutcome)]
    assert outcomes and "no active alert" in outcomes[-1].reason


async def test_consume_top_allows_runner_up_on_next_press(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch, consume="top")
    feed(app, "worse", 120)
    feed(app, "best", 60)

    await app.hotkey_pressed()
    await app.hotkey_pressed()
    assert clicks == ["best", "worse"]

    await app.hotkey_pressed()
    assert clicks == ["best", "worse"]  # store empty now


async def test_repushed_listing_cannot_be_clicked_twice(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "snipe", 100)
    await app.hotkey_pressed()
    feed(app, "snipe", 100)  # tab re-pushes the same row
    await app.hotkey_pressed()
    assert clicks == ["snipe"]


async def test_press_with_expired_alert_noops(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    object.__setattr__(app.store, "_expiry_s", 0.01)
    feed(app, "gone", 100)
    await asyncio.sleep(0.05)
    await app.hotkey_pressed()
    assert clicks == []
    assert focused == []


async def test_alert_views_pushed_after_consumption(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "a", 100)
    bus.drain()
    await app.hotkey_pressed()
    updates = [e for e in bus.drain() if isinstance(e, AlertsChanged)]
    assert updates and updates[-1].alerts == ()  # overlay cleared


async def test_click_travel_specific_listing(monkeypatch):
    """Overlay click travels to exactly the clicked listing, not the best."""
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "worse", 120)
    feed(app, "best", 60)

    await app.travel_listing("worse")
    assert clicks == ["worse"]
    # best is still alive and targetable by the hotkey afterwards
    await app.hotkey_pressed()
    assert clicks == ["worse", "best"]


async def test_click_travel_gated_on_game(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.clear()
    feed(app, "snipe", 100)
    await app.travel_listing("snipe")
    assert clicks == []
    outcomes = [e for e in bus.drain() if isinstance(e, ClickOutcome)]
    assert outcomes and "PoE not running" in outcomes[-1].reason


async def test_click_travel_consumed_or_expired_noops(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "snipe", 100)
    await app.travel_listing("snipe")
    await app.travel_listing("snipe")  # second click on the same row
    assert clicks == ["snipe"]


def let_the_double_click_window_pass(app, listing_id):
    from sniper.__main__ import RETRAVEL_GAP_S

    app._traveled_at[listing_id] -= RETRAVEL_GAP_S + 0.1


async def test_clicking_a_traveled_listing_again_re_travels_it(monkeypatch):
    """'already traveled' used to be a dead end. A new click past the
    double-click window asks the tab to refresh the row and travel once."""
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "snipe", 100)
    await app.travel_listing("snipe")
    let_the_double_click_window_pass(app, "snipe")
    await app.travel_listing("snipe")
    assert clicks == ["snipe", "snipe (retravel)"]
    # and each further re-travel needs its own click past the window
    await app.travel_listing("snipe")
    assert clicks == ["snipe", "snipe (retravel)"]
    outcomes = [e for e in bus.drain() if isinstance(e, ClickOutcome)]
    assert "click again to re-travel" in outcomes[-1].reason


async def test_a_hotkey_travel_can_be_re_traveled_by_click(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "snipe", 100)
    await app.hotkey_pressed()
    let_the_double_click_window_pass(app, "snipe")
    await app.travel_listing("snipe")
    assert clicks == ["snipe", "snipe (retravel)"]


async def test_the_hotkey_never_re_travels(monkeypatch):
    """One press takes a live alert or nothing: a traveled listing is gone
    from the store, so a later press cannot send it again."""
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "snipe", 100)
    await app.hotkey_pressed()
    let_the_double_click_window_pass(app, "snipe")
    await app.hotkey_pressed()
    assert clicks == ["snipe"]


async def test_travel_emits_traveled_view_with_mods(monkeypatch):
    from sniper.bus import Traveled

    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "snipe", 100)
    bus.drain()
    await app.hotkey_pressed()
    traveled = [e for e in bus.drain() if isinstance(e, Traveled)]
    assert len(traveled) == 1
    assert traveled[0].view.listing_id == "snipe"
    assert traveled[0].view.mods == (("Monsters fire 2 additional Projectiles", "", "none"),)


# ------------------------------------------------------- failed travels
# The teleport can fail after the click: the item sold, or the row is gone.
# The alert we cannot have must not take the runners-up down with it.


def click_result(app, listing_id, ok, reason=""):
    from sniper.models import ClickResult

    app._on_click_result(ClickResult(listing_id=listing_id, ok=ok, reason=reason))


async def test_failed_travel_promotes_the_next_alert(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "runner", 120)
    feed(app, "best", 60)
    await app.hotkey_pressed()
    assert clicks == ["best"]
    bus.drain()

    # the userscript's optimistic ok, then the row turning up sold
    click_result(app, "best", True)
    click_result(app, "best", False, "item_no_longer_available")

    views = [e for e in bus.drain() if isinstance(e, AlertsChanged)][-1].alerts
    assert [v.listing_id for v in views] == ["runner"]
    await app.hotkey_pressed()  # one more press takes it - still one per click
    assert clicks == ["best", "runner"]


async def test_successful_travel_still_clears_the_runners_up(monkeypatch):
    """consume: all keeps meaning what it says when the travel works."""
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "runner", 120)
    feed(app, "best", 60)
    await app.hotkey_pressed()
    click_result(app, "best", True)
    await app.hotkey_pressed()
    assert clicks == ["best"]


async def test_failed_travel_does_not_re_alert_the_failed_listing(monkeypatch):
    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "runner", 120)
    feed(app, "sold", 60)
    await app.hotkey_pressed()
    click_result(app, "sold", False, "item_no_longer_available")
    feed(app, "sold", 60)  # the tab re-pushes the row
    await app.hotkey_pressed()
    assert clicks == ["sold", "runner"]


async def test_travel_not_sent_gives_the_runners_up_back(monkeypatch):
    """A dead tab means the click never left the building; no click_result
    is coming, so the send failure has to settle it."""
    app, bus, clicks, focused = build_app(monkeypatch)

    async def dead_tab(listing_id: str) -> bool:
        return False

    monkeypatch.setattr(app.server, "send_click_travel", dead_tab)
    feed(app, "runner", 120)
    feed(app, "best", 60)
    await app.hotkey_pressed()
    views = [e for e in bus.drain() if isinstance(e, AlertsChanged)][-1].alerts
    assert [v.listing_id for v in views] == ["runner"]


async def test_overlay_click_failure_leaves_the_store_alone(monkeypatch):
    """A click travels only what was clicked, so there is no collateral to
    give back - and a stray result must not resurrect anything."""
    app, bus, clicks, focused = build_app(monkeypatch)
    app.watcher.running.set()
    feed(app, "runner", 120)
    feed(app, "clicked", 60)
    await app.travel_listing("clicked")
    click_result(app, "clicked", False, "item_no_longer_available")
    assert [v.listing_id for v in app.store.views()] == ["runner"]


async def test_every_decision_reaches_the_feed(monkeypatch):
    from sniper.bus import ListingSeen

    app, bus, clicks, focused = build_app(monkeypatch)
    feed(app, "good", 100)  # alert (100 div profit >= 20)
    feed(app, "meh", 195)  # below_threshold (5 div profit)
    seen = [e.entry for e in bus.drain() if isinstance(e, ListingSeen)]
    assert [(e.listing_id, e.verdict) for e in seen] == [
        ("good", "alert"),
        ("meh", "below_threshold"),
    ]
    assert seen[0].net_div == 100
    assert seen[1].net_div == 5
