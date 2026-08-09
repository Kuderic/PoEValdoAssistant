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

    async def fake_send(listing_id: str) -> bool:
        clicks.append(listing_id)
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
    assert traveled[0].view.mods == ("Monsters fire 2 additional Projectiles",)
