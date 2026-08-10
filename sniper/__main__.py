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
import time
from collections import OrderedDict

from sniper import logging_setup
from sniper.alerts import AlertStore
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    FeedEntry,
    GameStatus,
    ListingSeen,
    PriceStatus,
    RewardPrices,
    TabsChanged,
    Traveled,
    WarmupStatus,
)
from sniper.config import Config, ConfigError, load_config
from sniper.gamewindow import GameWatcher, focus_poe_window
from sniper.logging_setup import event
from sniper.models import ClickResult, Decision
from sniper.modrules import ModRules
from sniper.ninja import NinjaBackoff, NinjaClient
from sniper.prices import PriceBook
from sniper.server import SniperServer

# Longest the startup price warm-up may hold listings back. A reward the
# trade API has no listings for would otherwise stall the app indefinitely.
WARMUP_TIMEOUT_S = 120.0


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
        # every recent decision (any verdict) so grayed-out feed rows are
        # clickable too; _traveled_ids guards one-travel-per-listing
        self._recent: OrderedDict[str, Decision] = OrderedDict()
        self._traveled_ids: set[str] = set()
        # Startup warm-up: until a reward has a primary (trade/manual) price,
        # its listings are held rather than judged against poe.ninja's
        # inaccurate per-map median. Disabled outright when trade pricing is
        # off - then poe.ninja IS the intended source and there is nothing
        # to wait for.
        self._warmup_active = config.trade_pricing.enabled
        self._warmup_deadline = time.monotonic() + WARMUP_TIMEOUT_S
        # rewards the trade API answered for but had no listings of: they can
        # never get a primary price, so they must not stall the warm-up
        self._warmup_settled: set[str] = set()

    # ------------------------------------------------------------ callbacks

    def _hold_listing(self, decision: Decision) -> bool:
        """True when this listing must not surface yet: during the startup
        warm-up a reward with no primary price would be judged against the
        poe.ninja median (or nothing), producing exactly the inaccurate
        profit figures the warm-up exists to avoid. The listing is still
        logged, and its reward still registers with the server so the price
        loop picks it up."""
        if not self._warmup_active or self.book.has_primary_price(decision.key):
            return False
        event(
            "listing_held",
            listing_id=decision.listing.listing_id,
            key=decision.key,
            reason="awaiting_primary_price",
        )
        return True

    def _update_warmup(self) -> None:
        """Warm-up ends once every reward we know about has a primary price,
        or the timeout expires (a reward the trade API has no listings for
        must never stall the app forever)."""
        if not self._warmup_active:
            return
        rewards = sorted(self.server.active_rewards())
        ready = [r for r in rewards if self.book.has_primary_price(r) or r in self._warmup_settled]
        timed_out = time.monotonic() >= self._warmup_deadline
        if (rewards and len(ready) == len(rewards)) or timed_out:
            self._warmup_active = False
            event("warmup_done", ready=len(ready), total=len(rewards), timed_out=timed_out)
        if self.bus:
            self.bus.put(WarmupStatus(self._warmup_active, len(ready), len(rewards)))

    def _on_decision(self, decision: Decision) -> None:
        if self._hold_listing(decision):
            return
        self._recent[decision.listing.listing_id] = decision
        while len(self._recent) > 200:
            self._recent.popitem(last=False)
        if self.bus:  # live feed shows every listing, alerting or not
            self.bus.put(
                ListingSeen(
                    FeedEntry(
                        listing_id=decision.listing.listing_id,
                        key=decision.key,
                        amount=decision.listing.price.amount,
                        currency=decision.listing.price.currency,
                        profit_div=decision.profit_div,
                        difficulty=decision.difficulty,
                        verdict=decision.verdict,
                        mods=decision.mods_annotated,
                        received_monotonic=time.monotonic(),
                    )
                )
            )
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
        # a newly connected tab adds a reward to price before we are warm
        self._update_warmup()

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
        """Overlay click on any listed row - active alert OR a grayed-out
        feed entry. One user click = one click_travel for exactly that
        listing, once ever, with the same game gate as the hotkey."""
        import time

        from sniper.alerts import Alert

        if not self.watcher.running.is_set():
            event("travel_click_ignored", reason="poe_not_running", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "PoE not running - click ignored"))
            return
        if listing_id in self._traveled_ids:
            event("travel_click_noop", reason="already_traveled", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "already traveled to this listing"))
            return
        alert = self.store.consume(listing_id)
        self._push_alerts(new_alert=False)
        if alert is None:
            decision = self._recent.get(listing_id)
            if decision is None:
                event("travel_click_noop", reason="listing_unknown", listing_id=listing_id)
                if self.bus:
                    self.bus.put(ClickOutcome(listing_id, False, "listing no longer tracked"))
                return
            now = time.monotonic()
            alert = Alert(decision=decision, created_monotonic=now, expires_at_monotonic=now)
        self._traveled_ids.add(listing_id)
        event(
            "travel_click",
            listing_id=listing_id,
            key=alert.decision.key,
            verdict=alert.decision.verdict,
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

    def apply_tuning(
        self, scoring_config, global_profit_div: float, flat_profit_reduction: float = 1.0
    ) -> None:
        """Swap difficulty scoring + profit numbers live (settings panel).
        Runs on the asyncio thread via call_soon_threadsafe; affects listings
        from now on."""
        from dataclasses import replace

        from sniper.modrules import ModScoring

        self.server.set_scoring(ModScoring(scoring_config))
        self.server.set_thresholds(
            replace(
                self.config.thresholds,
                global_profit_div=global_profit_div,
                flat_profit_reduction=flat_profit_reduction,
            )
        )
        event(
            "tuning_updated",
            global_profit_div=global_profit_div,
            flat_profit_reduction=flat_profit_reduction,
            base_default=scoring_config.base_default,
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

    def _push_reward_prices(self) -> None:
        """Current reference per active reward -> Searching-line tooltip."""
        if not self.bus:
            return
        entries = []
        for reward in sorted(self.server.active_rewards()):
            ref = self.book.reference_for(reward)
            if ref is not None:
                entries.append((reward, ref.display_amount, ref.display_currency, ref.source))
        self.bus.put(RewardPrices(tuple(entries)))

    async def tab_freshness_loop(self) -> None:
        """Push tab heartbeat ages every 10s so a silently dead tab shows as
        stale on the overlay before it costs a snipe (hello comes every 30s)."""
        while True:
            await asyncio.sleep(10)
            if self.bus and self.server.tabs:
                self.bus.put(TabsChanged(tuple(self.server.tab_snapshot())))

    async def trade_price_loop(self, pricer) -> None:
        """Average the cheapest unid listings of each active reward's unique
        via the trade API (primary price source). Each reward is re-priced
        every refresh_minutes.

        The poll runs immediately and then fast while warming up, so the
        startup hold (see _hold_listing) lifts as soon as prices land rather
        than after a fixed delay; it relaxes once warm."""
        import time

        from sniper.tradeprice import TradeBackoff, select_representative

        interval_s = self.config.trade_pricing.refresh_minutes * 60
        last_priced: dict[str, float] = {}
        while True:
            league = self.book.league  # resolves via the first ninja refresh
            if league is None or pricer.in_backoff:
                self._update_warmup()  # keeps the timeout honest
                await asyncio.sleep(2 if self._warmup_active else 15)
                continue
            now = time.monotonic()
            due = [
                r
                for r in sorted(self.server.active_rewards())
                if now - last_priced.get(r, -interval_s) >= interval_s
            ]
            for reward in due:
                try:
                    listings, mode = await pricer.fetch_reward_listings(league, reward)
                except TradeBackoff as e:
                    event(
                        "trade_backoff",
                        reason=str(e),
                        retry_in_s=round(max(pricer.backoff_remaining, 1), 1),
                    )
                    break
                except Exception as e:  # pricing must never kill the app
                    event("trade_price_error", reward=reward, error=repr(e))
                    break
                last_priced[reward] = time.monotonic()
                div_prices = [
                    d
                    for amount, currency in listings
                    if (d := self.book.to_divine(amount, currency)) is not None
                ]
                avg_div, dropped = select_representative(
                    div_prices,
                    self.config.trade_pricing.max_listings,
                    self.config.trade_pricing.outlier_cutoff,
                )
                if avg_div is not None:
                    self.book.set_trade_price(reward, avg_div)
                    event(
                        "trade_price",
                        reward=reward,
                        listings=len(div_prices),
                        dropped_outliers=dropped,
                        avg_div=round(avg_div, 1),
                        mode=mode,
                    )
                else:
                    event(
                        "trade_price_empty",
                        reward=reward,
                        mode=mode,
                        note="no listings at any filter stage; ninja/manual fallback applies",
                    )
                    self._warmup_settled.add(reward)
                # each price landing may be the one that ends the warm-up
                self._update_warmup()
                await pricer.pause_between_rewards()
            self._push_reward_prices()
            self._update_warmup()
            await asyncio.sleep(2 if self._warmup_active else 15)

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
        if self.config.trade_pricing.enabled:
            from sniper.tradeprice import TradePricer

            pricer = TradePricer(
                base_url=self.config.trade_pricing.base_url,
                max_listings=self.config.trade_pricing.max_listings,
                corrupted_uniques=tuple(self.config.trade_pricing.corrupted_uniques),
                min_unid_listings=self.config.trade_pricing.min_unid_listings,
            )
            tasks.append(asyncio.create_task(self.trade_price_loop(pricer), name="trade-price"))
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

    def on_settings_change(
        scoring_config, global_profit_div: float, combo: str, flat_profit_reduction: float
    ) -> str | None:
        """Returns an error string (shown in the panel) or None. The hotkey
        rebinds on this (UI) thread; scoring/threshold swap on the loop."""
        error = hotkey.rebind(combo)
        loop.call_soon_threadsafe(
            app.apply_tuning, scoring_config, global_profit_div, flat_profit_reduction
        )
        return error

    root = tk.Tk()
    Overlay(
        root,
        bus,
        config,
        on_travel=on_travel,
        on_settings_change=on_settings_change,
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
