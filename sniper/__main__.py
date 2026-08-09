"""Entry point.

Thread model (DESIGN.md): tkinter owns the main thread; the websocket
server, price refresh, and alert expiry run on an asyncio loop in a daemon
thread; the overlay drains an immutable-event queue (sniper.bus). Milestone
4 adds the global hotkey thread.

`--headless` runs the asyncio side only (used by the replay acceptance).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import threading

from sniper import logging_setup
from sniper.alerts import AlertStore
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    GameStatus,
    PriceStatus,
    TabsChanged,
    Traveled,
)
from sniper.config import Config, ConfigError, load_config
from sniper.gamewindow import GameWatcher, focus_poe_window
from sniper.logging_setup import event
from sniper.models import ClickResult, Decision
from sniper.modrules import ModRules
from sniper.ninja import NinjaBackoff, NinjaClient
from sniper.prices import PriceBook
from sniper.server import SniperServer


class App:
    """Owns the asyncio-side objects; every method here runs on the asyncio
    thread unless noted."""

    def __init__(self, config: Config, bus: Bus | None):
        self.config = config
        self.bus = bus
        self.book = PriceBook(config)
        self.store = AlertStore(
            expiry_s=config.alerts.expiry_seconds,
            consume_mode=config.hotkey.consume,
            max_display=config.alerts.max_display,
        )
        self.server = SniperServer(
            config,
            self.book,
            ModRules(config.mod_warnings),
            on_decision=self._on_decision,
            on_click_result=self._on_click_result,
            on_tabs_changed=self._on_tabs_changed,
        )
        self.watcher = GameWatcher(config.game.process_names)

    # ------------------------------------------------------------ callbacks

    def _on_decision(self, decision: Decision) -> None:
        if decision.verdict != "alert":
            return
        if self.store.insert(decision):
            event(
                "alert_raised",
                listing_id=decision.listing.listing_id,
                key=decision.key,
                profit_div=round(decision.profit_div or 0, 2),
                active_alerts=len(self.store),
            )
            self._push_alerts(
                new_alert=True, new_view=self.store.view_of(decision.listing.listing_id)
            )

    def _on_click_result(self, result: ClickResult) -> None:
        if self.bus:
            self.bus.put(ClickOutcome(result.listing_id, result.ok, result.reason))

    def _on_tabs_changed(self) -> None:
        if self.bus:
            self.bus.put(TabsChanged(tuple(self.server.tab_snapshot())))

    def _push_alerts(self, new_alert: bool, new_view=None) -> None:
        if self.bus:
            self.bus.put(AlertsChanged(self.store.views(), new_alert=new_alert, new_view=new_view))

    # ------------------------------------------------------------ travel path

    async def hotkey_pressed(self) -> None:
        """One user press = at most one click_travel (hard ToS constraint).
        Runs on the asyncio loop, scheduled by the keyboard hook thread."""
        alert = self.store.consume_best()
        self._push_alerts(new_alert=False)
        if alert is None:
            event("hotkey_noop", reason="no_active_alert")
            if self.bus:
                self.bus.put(ClickOutcome("", False, "no active alert - press ignored"))
            return
        event(
            "hotkey_press",
            listing_id=alert.decision.listing.listing_id,
            key=alert.decision.key,
            profit_div=round(alert.decision.profit_div or 0, 2),
        )
        await self._travel(alert)

    async def travel_listing(self, listing_id: str) -> None:
        """Overlay click on a specific alert row: one user click = one
        click_travel for exactly that listing. Same game gate as the hotkey."""
        if not self.watcher.running.is_set():
            event("travel_click_ignored", reason="poe_not_running", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "PoE not running - click ignored"))
            return
        alert = self.store.consume(listing_id)
        self._push_alerts(new_alert=False)
        if alert is None:
            event("travel_click_noop", reason="alert_gone", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "alert expired or already used"))
            return
        event(
            "travel_click",
            listing_id=listing_id,
            key=alert.decision.key,
            profit_div=round(alert.decision.profit_div or 0, 2),
        )
        await self._travel(alert)

    async def _travel(self, alert) -> None:
        lid = alert.decision.listing.listing_id
        if self.bus:
            self.bus.put(Traveled(alert.view()))
        ok = await self.server.send_click_travel(lid)
        if not ok and self.bus:
            self.bus.put(ClickOutcome(lid, False, "tab gone - travel not sent"))
        # click first (latency-critical), then bring the game to front
        await asyncio.get_running_loop().run_in_executor(
            None, focus_poe_window, self.config.game.window_title
        )

    # ------------------------------------------------------------ live tuning

    def apply_scoring(self, scoring_config) -> None:
        """Swap difficulty scoring live (tuning panel). Runs on the asyncio
        thread via call_soon_threadsafe; affects listings from now on."""
        from sniper.modrules import ModScoring

        self.server.set_scoring(ModScoring(scoring_config))
        event(
            "scoring_updated",
            base_default=scoring_config.base_default,
            div_per_point=scoring_config.div_per_point,
            rules={
                r.label: {"min_base": r.min_base, "multiplier": r.multiplier}
                for r in scoring_config.rules
            },
        )

    # ----------------------------------------------------------- long tasks

    async def expiry_loop(self) -> None:
        while True:
            if self.store.prune():
                self._push_alerts(new_alert=False)
            await asyncio.sleep(0.25)

    async def tab_freshness_loop(self) -> None:
        """Push tab heartbeat ages every 10s so a silently dead tab shows as
        stale on the overlay before it costs a snipe (hello comes every 30s)."""
        while True:
            await asyncio.sleep(10)
            if self.bus and self.server.tabs:
                self.bus.put(TabsChanged(tuple(self.server.tab_snapshot())))

    async def price_refresh_loop(self, ninja: NinjaClient) -> None:
        interval_s = self.config.ninja.refresh_minutes * 60
        while True:
            try:
                stats = await self.book.refresh(ninja)
                event("price_refresh", **stats, status=self.book.status)
            except NinjaBackoff as e:
                event(
                    "ninja_backoff",
                    reason=str(e),
                    retry_in_s=round(max(ninja.backoff_remaining, 1), 1),
                )
            except Exception as e:  # refresh must never kill the app
                event("price_refresh_error", error=repr(e))
            if self.bus:
                self.bus.put(PriceStatus(self.book.status, self.book.league))
            await asyncio.sleep(max(interval_s, ninja.backoff_remaining + 5))

    async def run_async(self) -> None:
        def on_game_change(running: bool) -> None:
            if self.bus:
                self.bus.put(GameStatus(running))

        tasks = [
            asyncio.create_task(self.server.serve_forever(), name="ws-server"),
            asyncio.create_task(self.expiry_loop(), name="alert-expiry"),
            asyncio.create_task(self.watcher.watch(on_game_change), name="game-watch"),
            asyncio.create_task(self.tab_freshness_loop(), name="tab-freshness"),
        ]
        if self.config.ninja.enabled:
            ninja = NinjaClient(self.config.ninja.base_url)
            tasks.append(asyncio.create_task(self.price_refresh_loop(ninja), name="price-refresh"))
        else:
            event("ninja_disabled", note="manual price table only")
        await asyncio.gather(*tasks)


def run_with_overlay(config: Config, config_path: str) -> None:
    import tkinter as tk
    from pathlib import Path

    from sniper.config import SCORING_OVERRIDES_NAME
    from sniper.hotkey import Hotkey, HotkeyError
    from sniper.overlay import Overlay

    bus = Bus()
    app = App(config, bus)
    loop = asyncio.new_event_loop()

    def asyncio_thread() -> None:
        asyncio.set_event_loop(loop)
        loop.create_task(app.run_async())
        loop.run_forever()

    thread = threading.Thread(target=asyncio_thread, name="asyncio-loop", daemon=True)
    thread.start()

    try:
        hotkey = Hotkey(config.hotkey.combo, loop, app.hotkey_pressed, app.watcher.running)
    except HotkeyError as e:
        loop.call_soon_threadsafe(loop.stop)
        raise SystemExit(str(e)) from None

    # UI-thread callbacks marshal into the asyncio loop; nothing UI-side
    # touches App state directly.
    def on_travel(listing_id: str) -> None:
        asyncio.run_coroutine_threadsafe(app.travel_listing(listing_id), loop)

    def on_scoring_change(scoring_config) -> None:
        loop.call_soon_threadsafe(app.apply_scoring, scoring_config)

    root = tk.Tk()
    Overlay(
        root,
        bus,
        config,
        on_travel=on_travel,
        on_scoring_change=on_scoring_change,
        overrides_path=Path(config_path).with_name(SCORING_OVERRIDES_NAME),
    )
    try:
        root.mainloop()
    finally:
        hotkey.unregister()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sniper")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--headless", action="store_true", help="no overlay window (server + decisions only)"
    )
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as e:
        raise SystemExit(f"config error: {e}") from None
    listener = logging_setup.setup(config.logging)
    event(
        "startup",
        league=config.league,
        ws=f"{config.server.host}:{config.server.port}",
        headless=args.headless,
    )
    try:
        if args.headless:
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(App(config, bus=None).run_async())
        else:
            run_with_overlay(config, args.config)
    finally:
        event("shutdown")
        listener.stop()


if __name__ == "__main__":
    main()
