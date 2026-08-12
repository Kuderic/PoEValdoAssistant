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
from pathlib import Path

from sniper import logging_setup, pricecache
from sniper.alerts import AlertStore
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    FeedEntry,
    GameStatus,
    ListingSeen,
    PriceStatus,
    PricingHealth,
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

# How long the startup warm-up will wait WITHOUT PROGRESS before giving up
# and letting listings through on fallback prices. A stall timer, not a
# total budget: pricing paces itself off the API's rate-limit headers, so a
# large batch can legitimately take minutes, and a fixed deadline would
# degrade a run that was working fine.
WARMUP_STALL_S = 90.0


class App:
    """Owns the asyncio-side objects; every method here runs on the asyncio
    thread unless noted."""

    def __init__(self, config: Config, bus: Bus | None, cache_path: Path | None = None):
        self.config = config
        self.bus = bus
        self.book = PriceBook(config)
        # Seed last session's reward prices so the very first listing can be
        # judged instead of held (see _hold_listing). They are provisional -
        # source="cached" - until the background refresh replaces them.
        self._cache_path = cache_path
        if cache_path is not None:
            cached = pricecache.load(cache_path, config.trade_pricing.cache_max_age_minutes * 60)
            for reward, (divine, priced_at) in cached.items():
                self.book.set_trade_price(reward, divine, cached=True, priced_at=priced_at)
            if cached:
                event("price_cache_loaded", rewards=len(cached))
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
        self._warmup_deadline = time.monotonic() + WARMUP_STALL_S
        self._warmup_ready = 0  # rewards ready so far; progress resets the stall
        # rewards the trade API answered for but had no listings of: they can
        # never get a primary price, so they must not stall the warm-up
        self._warmup_settled: set[str] = set()
        self._pricing_now: str | None = None  # reward being fetched right now

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
        """Advance the startup warm-up AND publish pricing progress.

        Progress keeps publishing after the warm-up ends, so the same panel
        covers the periodic re-price and a reward that appears when a new
        live search is opened mid-session. Only the `calculating` flag -
        which is what holds listings back - is warm-up specific.
        """
        rewards = sorted(self.server.active_rewards())
        ready = [r for r in rewards if self.book.has_primary_price(r) or r in self._warmup_settled]
        if self._warmup_active:
            if len(ready) > self._warmup_ready:  # progress: give it more rope
                self._warmup_ready = len(ready)
                self._warmup_deadline = time.monotonic() + WARMUP_STALL_S
            timed_out = time.monotonic() >= self._warmup_deadline
            if (rewards and len(ready) == len(rewards)) or timed_out:
                self._warmup_active = False
                event("warmup_done", ready=len(ready), total=len(rewards), stalled=timed_out)
        if self.bus:
            self.bus.put(
                WarmupStatus(
                    self._warmup_active,
                    len(ready),
                    len(rewards),
                    self._pricing_rows(rewards),
                )
            )

    def _pricing_rows(self, rewards: list[str]) -> tuple[tuple[str, str, str, float | None], ...]:
        """Per-reward progress for the panel: what is priced, what is being
        fetched right now, and what is still queued - plus whatever
        price we have so far. A '~' marks a poe.ninja estimate rather than a
        settled trade-API average."""
        rows = []
        for reward in rewards:
            ref = self.book.reference_for(reward)
            primary = self.book.has_primary_price(reward)
            # "working" outranks "done": a scheduled re-price happens to a
            # reward that already HAS a price, and reporting it as done
            # would hide every refresh after the first
            if reward == self._pricing_now:
                state = "working"
            elif primary:
                state = "done"
            elif reward in self._warmup_settled:
                state = "unpriced"
            else:
                state = "pending"
            if ref is None:
                price = ""
            else:
                price = f"{ref.display_amount:g} {ref.display_currency}"
                if not primary:
                    price = f"~{price}"  # fallback estimate, not settled yet
            rows.append((reward, state, price, self.book.price_age_s(reward)))
        return tuple(rows)

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
                        pairings=decision.pairings,
                        index_lag_ms=decision.listing.index_lag_ms,
                        reference_source=(decision.reference.source if decision.reference else ""),
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

    def _save_price_cache(self) -> None:
        """Persist freshly fetched prices so the next start is not blind."""
        if self._cache_path is not None:
            pricecache.save(self._cache_path, self.book.trade_prices())

    def _push_pricing_health(self, pricer) -> None:
        """Tell the overlay whether trade pricing is actually working, so a
        rate-limited session never looks healthy while it quietly falls back
        to poe.ninja medians."""
        if not self.bus:
            return
        rewards = self.server.active_rewards()
        unpriced = [r for r in rewards if not self.book.has_primary_price(r)]
        self.bus.put(
            PricingHealth(
                backoff_s=round(pricer.backoff_remaining, 1),
                unpriced=len(unpriced),
                total=len(rewards),
            )
        )

    def _push_reward_prices(self) -> None:
        """Current reference per active reward -> Searching-line tooltip."""
        if not self.bus:
            return
        entries = []
        for reward in sorted(self.server.active_rewards()):
            ref = self.book.reference_for(reward)
            if ref is not None:
                entries.append(
                    (
                        reward,
                        ref.display_amount,
                        ref.display_currency,
                        ref.source,
                        self.book.price_age_s(reward),
                    )
                )
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
                self._update_warmup()  # keeps the stall timer honest
                self._push_pricing_health(pricer)  # keep the pill counting down
                await asyncio.sleep(2 if self._warmup_active else 15)
                continue
            now = time.monotonic()
            due = [
                r
                for r in sorted(self.server.active_rewards())
                if now - last_priced.get(r, -interval_s) >= interval_s
            ]
            for reward in due:
                self._pricing_now = reward  # so the startup panel can say so
                self._update_warmup()
                try:
                    listings, mode = await pricer.fetch_reward_listings(league, reward)
                except TradeBackoff as e:
                    # every further call would be refused locally until the
                    # backoff lifts, so stop the batch rather than spin. The
                    # unpriced rewards never got a last_priced stamp, so they
                    # are still due and go first on the next pass.
                    event(
                        "trade_backoff",
                        reason=str(e),
                        retry_in_s=round(max(pricer.backoff_remaining, 1), 1),
                        unpriced=len(due) - due.index(reward),
                    )
                    break
                except Exception as e:  # pricing must never kill the app
                    # only THIS reward is broken (bad payload, odd name);
                    # the rest of the batch is still worth pricing
                    event("trade_price_error", reward=reward, error=repr(e))
                    continue
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
                    self._save_price_cache()
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
                self._pricing_now = None
                self._update_warmup()
                await pricer.pause_between_rewards()
            # however the batch ended (done, backoff, bad reward), nothing is
            # in flight now - a stale "working" marker would be a lie
            self._pricing_now = None
            self._push_reward_prices()
            self._update_warmup()
            self._push_pricing_health(pricer)
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
    # the cache lives beside config.yaml, like scoring_overrides.yaml
    app = App(config, bus, cache_path=Path(config_path).with_name(pricecache.CACHE_NAME))
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
            cache = Path(args.config).with_name(pricecache.CACHE_NAME)
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(App(config, bus=None, cache_path=cache).run_async())
        else:
            run_with_overlay(config, args.config)
    finally:
        event("shutdown")
        listener.stop()


if __name__ == "__main__":
    main()
