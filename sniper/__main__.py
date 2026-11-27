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
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from sniper import __version__, logging_setup, paths, pricecache
from sniper.alerts import AlertStore
from sniper.bus import (
    AlertsChanged,
    AreaEntered,
    Bus,
    ClickOutcome,
    ClientLogStatus,
    FeedEntry,
    GameStatus,
    HotkeyPressed,
    ListingSeen,
    PriceStatus,
    PricingHealth,
    RewardPrice,
    RewardPrices,
    TabsChanged,
    Traveled,
    UpdateStatus,
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
# After the warm-up, a reward with no trade price at all (a search opened
# mid-session for a reward never priced, or priced too long ago) holds its
# listings the same way, for at most this long: one paced fetch with the
# identified fallback takes about three minutes. Past it, listings are judged
# on the poe.ninja fallback, so a broken pricer cannot hide a reward forever.
MIDSESSION_HOLD_S = 300.0
# A traveled listing clicked again sooner than this is a double-click, not a
# request to travel again (see App.travel_listing).
RETRAVEL_GAP_S = 2.0
# traveled listings remembered for re-travel; matches the _recent cap
RECENT_CAP = 200


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
            jackpot_min_net=config.alerts.jackpot_min_net,
            jackpot_min_yield=config.alerts.jackpot_min_yield,
            super_jackpot_min_net=config.alerts.super_jackpot_min_net,
            super_jackpot_min_yield=config.alerts.super_jackpot_min_yield,
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
        # clickable too
        self._recent: OrderedDict[str, Decision] = OrderedDict()
        # listing id -> monotonic time of its last travel (hotkey or click).
        # Clicking a traveled listing again re-travels it (refresh + one
        # click in the tab), but not within RETRAVEL_GAP_S: a double-click
        # must not fire a second travel on top of the first.
        self._traveled_at: dict[str, float] = {}
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
        # reward -> when its first listing was held after the warm-up
        self._held_since: dict[str, float] = {}
        self._pricing_now: str | None = None  # reward being fetched right now
        # rewards the user asked to re-check now (the price panel's ↻ button):
        # they jump the queue on the price loop's next pass, still under the
        # trade API's rate limits. _price_wake cuts the loop's nap short.
        self._recheck: set[str] = set()
        self._price_wake = asyncio.Event()

    # ------------------------------------------------------------ callbacks

    def _hold_listing(self, decision: Decision) -> bool:
        """True when this listing must not surface yet: a reward with no
        primary price would be judged against the poe.ninja median (or
        nothing), producing exactly the inaccurate profit figures the hold
        exists to avoid. During the startup warm-up the hold lasts until the
        warm-up ends; after it, up to MIDSESSION_HOLD_S per reward. The listing is still
        logged, and its reward still registers with the server so the price
        loop picks it up."""
        # A generic (non-Valdo) listing has no reward to price, so the
        # warm-up has nothing to wait for - holding it would mean never
        # showing it at all.
        if decision.listing.generic:
            return False
        key = decision.key
        if not self.config.trade_pricing.enabled or self.book.has_primary_price(key):
            self._held_since.pop(key, None)
            return False
        if key in self._warmup_settled:
            return False  # the trade API has no listings: ninja is all there is
        if not self._warmup_active:
            # Mid-session: a ninja median once turned a 209d Headhunter into
            # a 342d "super jackpot". Hold until the fetch lands, capped.
            since = self._held_since.setdefault(key, time.monotonic())
            if time.monotonic() - since > MIDSESSION_HOLD_S:
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
                # the stall already waited for these: no second hold
                for reward in set(rewards) - set(ready):
                    self._held_since[reward] = float("-inf")
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
            elif reward in self._recheck:
                state = "queued"  # the user asked for a re-check; not started yet
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
        while len(self._recent) > RECENT_CAP:
            self._recent.popitem(last=False)
        if self.bus:  # live feed shows every listing, alerting or not
            self.bus.put(
                ListingSeen(
                    FeedEntry(
                        listing_id=decision.listing.listing_id,
                        key=decision.key,
                        amount=decision.listing.price.amount,
                        currency=decision.listing.price.currency,
                        net_div=decision.net_div,
                        difficulty=decision.difficulty,
                        verdict=decision.verdict,
                        mods=decision.mods_annotated,
                        received_monotonic=time.monotonic(),
                        pairings=decision.pairings,
                        index_lag_ms=decision.listing.index_lag_ms,
                        reference_source=(decision.reference.source if decision.reference else ""),
                        generic=decision.listing.generic,
                        # verdict-gated: a blocked/below-threshold row must
                        # never be painted as the one worth chasing
                        jackpot=decision.verdict == "alert" and self.store.is_jackpot(decision),
                        super_jackpot=decision.verdict == "alert"
                        and self.store.is_super_jackpot(decision),
                        risks=decision.risks,
                        fail_chance=decision.fail_chance,
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
                net_div=round(decision.net_div or 0, 2),
                jackpot=self.store.is_jackpot(decision),
                super_jackpot=self.store.is_super_jackpot(decision),
                active_alerts=len(self.store),
            )
            self._push_alerts(
                new_alert=True, new_view=self.store.view_of(decision.listing.listing_id)
            )

    def _on_click_result(self, result: ClickResult) -> None:
        # A failed travel hands back the runners-up the press cleared, so the
        # next-best alert takes the top slot instead of an empty overlay.
        # Pushed before the outcome so both land in one overlay drain: the
        # ClickOutcome is what drops the traveled pin, and the promoted alert
        # should already be in place when it does.
        if self.store.settle_travel(result.listing_id, result.ok):
            event(
                "travel_failed_alerts_restored",
                listing_id=result.listing_id,
                reason=result.reason,
                active_alerts=len(self.store),
            )
            self._push_alerts(new_alert=False)
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
        if self.bus:
            self.bus.put(HotkeyPressed())  # overlay comes forward if set to
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
            net_div=round(alert.decision.net_div or 0, 2),
        )
        await self._travel(alert)

    async def travel_listing(self, listing_id: str) -> None:
        """Overlay click on any listed row - active alert OR a grayed-out
        feed entry. One user click = one click_travel for exactly that
        listing, with the same game gate as the hotkey.

        Clicking a listing already traveled to re-travels it: the tab
        refreshes that row (the site disables a used Travel button until
        then) and clicks Travel once. Still one click = one travel; nothing
        repeats without another click."""
        import time

        from sniper.alerts import Alert

        if not self.watcher.running.is_set():
            event("travel_click_ignored", reason="poe_not_running", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "PoE not running - click ignored"))
            return
        traveled_at = self._traveled_at.get(listing_id)
        if traveled_at is not None:
            if time.monotonic() - traveled_at < RETRAVEL_GAP_S:
                event("travel_click_noop", reason="just_traveled", listing_id=listing_id)
                if self.bus:
                    self.bus.put(
                        ClickOutcome(
                            listing_id, False, "travel just sent - click again to re-travel"
                        )
                    )
                return
            decision = self._recent.get(listing_id)
            if decision is None:
                event("travel_click_noop", reason="listing_unknown", listing_id=listing_id)
                if self.bus:
                    self.bus.put(ClickOutcome(listing_id, False, "listing no longer tracked"))
                return
            now = time.monotonic()
            alert = Alert(decision=decision, created_monotonic=now, expires_at_monotonic=now)
            event(
                "travel_click",
                listing_id=listing_id,
                key=decision.key,
                verdict=decision.verdict,
                net_div=round(decision.net_div or 0, 2),
                retravel=True,
            )
            await self._travel(alert, retravel=True)
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
        event(
            "travel_click",
            listing_id=listing_id,
            key=alert.decision.key,
            verdict=alert.decision.verdict,
            net_div=round(alert.decision.net_div or 0, 2),
        )
        await self._travel(alert)

    async def _travel(self, alert, retravel: bool = False) -> None:
        import time

        lid = alert.decision.listing.listing_id
        self._traveled_at[lid] = time.monotonic()
        if len(self._traveled_at) > RECENT_CAP:
            self._traveled_at.pop(next(iter(self._traveled_at)))
        if self.bus:
            self.bus.put(Traveled(alert.view()))
        if retravel:
            ok = await self.server.send_click_travel(lid, retravel=True)
        else:
            ok = await self.server.send_click_travel(lid)
        if not ok:
            # the click never left the building, so no click_result is coming:
            # settle it here or the runners-up would be lost to a travel that
            # never happened
            if self.store.settle_travel(lid, False):
                self._push_alerts(new_alert=False)
            if self.bus:
                self.bus.put(ClickOutcome(lid, False, "tab gone - travel not sent"))
        # click first (latency-critical), then bring the game to front
        await asyncio.get_running_loop().run_in_executor(
            None, focus_poe_window, self.config.game.window_title
        )

    # ------------------------------------------------------------ live tuning

    def apply_tuning(
        self,
        scoring_config,
        min_yield: float,
        running_cost: float = 1.0,
        min_net: float | None = None,
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
                min_yield=min_yield,
                running_cost=running_cost,
                min_net=self.config.thresholds.min_net if min_net is None else min_net,
            )
        )
        event(
            "tuning_updated",
            min_yield=min_yield,
            min_net=min_net,
            running_cost=running_cost,
            base_default=scoring_config.base_default,
            rules={
                r.label: {"min_base": r.min_base, "multiplier": r.multiplier}
                for r in scoring_config.rules
            },
        )

    def set_jackpot_thresholds(
        self,
        min_net: float,
        min_yield: float,
        super_min_net: float | None = None,
        super_min_yield: float | None = None,
    ) -> None:
        """Settings panel jackpot + super jackpot bars; asyncio thread."""
        self.store.set_jackpot_thresholds(min_net, min_yield, super_min_net, super_min_yield)
        event(
            "jackpot_thresholds_updated",
            min_net=min_net,
            min_yield=min_yield,
            super_min_net=super_min_net,
            super_min_yield=super_min_yield,
        )

    def set_price_override(self, reward: str, divine: float | None) -> None:
        """Pin (or unpin, with None) a reward's reference price for this
        session - the settings panel's price editor.

        Runs on the asyncio thread via call_soon_threadsafe. Like the scoring
        panel it affects listings from now on; alerts already on the overlay
        keep the profit they were judged with. Never persisted: a correction
        for a momentarily wrong reference should not outlive the session.
        """
        self.book.set_override(reward, divine)
        event("price_override", reward=reward, divine=divine)
        # a pinned price counts as primary, so this can be what ends the
        # warm-up for a reward the trade API has no listings for
        self._update_warmup()
        self._push_reward_prices()

    def request_price_recheck(self, reward: str) -> None:
        """Price panel ↻ button: re-price this reward now instead of waiting
        for its scheduled refresh. Asyncio thread.

        One press queues one fetch. It goes through the same pricer as every
        scheduled re-price, so the trade API's rate-limit headers and 429
        backoff still apply - during a backoff the request waits for it to
        lift. A press while the reward is already queued or being fetched
        does nothing: that fetch is the answer.
        """
        if not self.config.trade_pricing.enabled:
            return
        if reward not in self.server.active_rewards():
            return
        if reward == self._pricing_now or reward in self._recheck:
            event("price_recheck_ignored", reward=reward, reason="already in progress")
            return
        self._recheck.add(reward)
        event("price_recheck_requested", reward=reward)
        self._update_warmup()  # the row shows "queued" straight away
        self._price_wake.set()

    async def _price_nap(self, seconds: float) -> None:
        """Sleep between price-loop passes, waking early for a re-check."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._price_wake.wait(), seconds)
        self._price_wake.clear()

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
        """Current reference per active reward -> Searching-line tooltip.

        Every active reward gets a row even when it has no reference yet.
        Skipping the unpriced ones meant hovering a reward named on the
        Searching line could produce nothing at all (no row, and with no
        reward priced, no tooltip whatsoever) - which reads as the hover
        being broken rather than as the price being on its way.
        """
        if not self.bus:
            return
        entries = []
        for reward in sorted(self.server.active_rewards()):
            ref = self.book.reference_for(reward)
            auto = self.book.auto_reference_for(reward)
            entries.append(
                RewardPrice(
                    reward=reward,
                    amount=None if ref is None else ref.display_amount,
                    currency="" if ref is None else ref.display_currency,
                    source="unpriced" if ref is None else ref.source,
                    age_s=self.book.price_age_s(reward),
                    override_div=self.book.override_for(reward),
                    auto_amount=None if auto is None else auto.display_amount,
                    auto_currency="" if auto is None else auto.display_currency,
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
                # a tab opened while we are stalled is still an active reward
                # the user can hover; without this its row never appears
                self._push_reward_prices()
                await self._price_nap(2 if self._warmup_active else 15)
                continue
            now = time.monotonic()
            active = sorted(self.server.active_rewards())
            # user-requested re-checks first; a request for a reward whose
            # search has since closed is dropped
            forced = [r for r in active if r in self._recheck]
            self._recheck.clear()
            due = forced + [
                r
                for r in active
                if r not in forced and now - last_priced.get(r, -interval_s) >= interval_s
            ]
            if due:
                # a pass can take minutes (each reward is paced for the rate
                # limit): show what is known now - last session's cached
                # prices, once the divine rate is in - rather than "—" rows
                # until the whole pass is done
                self._push_reward_prices()
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
                    # a re-check the user asked for still happens, just
                    # after the backoff lifts
                    self._recheck.update(r for r in due[due.index(reward) :] if r in forced)
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
                # and goes on screen now, not when the last reward is done
                self._push_reward_prices()
                await pricer.pause_between_rewards()
            # however the batch ended (done, backoff, bad reward), nothing is
            # in flight now - a stale "working" marker would be a lie
            self._pricing_now = None
            self._push_reward_prices()
            self._update_warmup()
            self._push_pricing_health(pricer)
            await self._price_nap(2 if self._warmup_active else 15)

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

    async def update_loop(self) -> None:
        """Self-update checks (sniper.updater), off the hot path. Never kills
        the app: an unexpected error is logged and the check retried later."""
        from sniper.updater import Updater

        def publish(status: UpdateStatus) -> None:
            event("update_status", state=status.state, version=status.version)
            if self.bus:
                self.bus.put(status)

        exe = Path(sys.executable) if paths.FROZEN else None
        while True:
            try:
                await Updater(self.config.updates, exe, publish).run()
                return  # disabled in config
            except Exception as e:
                event("update_error", error=repr(e))
                await asyncio.sleep(3600)

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
        if self.config.updates.enabled:
            tasks.append(asyncio.create_task(self.update_loop(), name="updates"))
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
                listing_status=self.config.trade_pricing.listing_status,
            )
            tasks.append(asyncio.create_task(self.trade_price_loop(pricer), name="trade-price"))
        await asyncio.gather(*tasks)


def run_with_overlay(config: Config, updated_from: str | None = None) -> bool:
    """Runs until the window closes. True when it closed to install a staged
    update (the caller swaps the exe and relaunches)."""
    import tkinter as tk

    from sniper.config import SCORING_OVERRIDES_NAME
    from sniper.hotkey import Hotkey, HotkeyError
    from sniper.overlay import Overlay

    bus = Bus()
    if updated_from:
        bus.put(UpdateStatus("installed", __version__, ""))
    # the cache lives with the other per-user state, like scoring_overrides.yaml
    app = App(config, bus, cache_path=config.state_dir / pricecache.CACHE_NAME)
    loop = asyncio.new_event_loop()

    def asyncio_thread() -> None:
        asyncio.set_event_loop(loop)
        loop.create_task(app.run_async())
        loop.run_forever()

    thread = threading.Thread(target=asyncio_thread, name="asyncio-loop", daemon=True)
    thread.start()

    # PoE's client log tells the shop marker when the seller's hideout loads;
    # its own thread, off the detection path entirely
    from sniper.clientlog import ClientLogWatcher

    client_log = ClientLogWatcher(
        config.game.process_names,
        on_area=lambda area: bus.put(AreaEntered(area)),
        on_status=lambda path: bus.put(ClientLogStatus(path)),
        override=config.shop_overlay.client_log,
    )
    client_log.start()

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
        scoring_config, min_yield: float, combo: str, running_cost: float, min_net: float
    ) -> str | None:
        """Returns an error string (shown in the panel) or None. The hotkey
        rebinds on this (UI) thread; scoring/threshold swap on the loop."""
        error = hotkey.rebind(combo)
        loop.call_soon_threadsafe(
            app.apply_tuning, scoring_config, min_yield, running_cost, min_net
        )
        return error

    def on_price_override(reward: str, divine: float | None) -> None:
        loop.call_soon_threadsafe(app.set_price_override, reward, divine)

    def on_price_recheck(reward: str) -> None:
        loop.call_soon_threadsafe(app.request_price_recheck, reward)

    def on_jackpot_change(
        min_net: float, min_yield: float, super_net: float, super_yield: float
    ) -> None:
        loop.call_soon_threadsafe(
            app.set_jackpot_thresholds, min_net, min_yield, super_net, super_yield
        )

    restart = {"requested": False}

    def on_update_install() -> None:
        """Update pill clicked with a staged update: close cleanly (hotkey,
        server, logs) so main() can swap the exe and start the new one."""
        if not paths.FROZEN:
            return
        event("update_install_requested")
        restart["requested"] = True
        root.destroy()

    # Our own taskbar identity: without it Windows groups the window under
    # python.exe and shows the Python logo instead of the app's icon.
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ValdoSniper.Overlay")
    root = tk.Tk()
    Overlay(
        root,
        bus,
        config,
        on_travel=on_travel,
        on_settings_change=on_settings_change,
        on_price_override=on_price_override,
        on_price_recheck=on_price_recheck,
        on_jackpot_change=on_jackpot_change,
        overrides_path=config.state_dir / SCORING_OVERRIDES_NAME,
        on_update_install=on_update_install,
    )
    try:
        root.mainloop()
    finally:
        hotkey.unregister()
        client_log.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
    return restart["requested"]


def _load(args: argparse.Namespace) -> Config:
    """The config to run with. Packaged: bundled defaults, with the user's
    optional config.yaml from the state folder (%LOCALAPPDATA%/ValdoSniper)
    layered over them, and per-user state there too. Source: config.yaml as
    before."""
    try:
        if paths.FROZEN:
            state = Path.cwd()  # prepare_frozen_dir ran from the state folder
            return load_config(
                args.config or state / paths.CONFIG_NAME,
                defaults=paths.default_config(),
                state_dir=state,
            )
        return load_config(args.config or paths.CONFIG_NAME)
    except ConfigError as e:
        raise SystemExit(f"config error: {e}") from None


def _self_check(args: argparse.Namespace) -> int:
    """`--self-check`: load everything a real start needs, then exit. Used by
    the release build to prove the packaged exe is complete. 0 = OK."""
    import tkinter  # noqa: F401 - must be bundled

    import keyboard  # noqa: F401
    import psutil  # noqa: F401

    from sniper import ninja, overlay, tradeprice, updater  # noqa: F401
    from sniper.server import USERSCRIPT_FILE

    _load(args)
    if "@version" not in USERSCRIPT_FILE.read_text(encoding="utf-8"):
        return 1
    if sys.platform == "win32":
        import win32gui  # noqa: F401
    return 0


def _install_staged_and_relaunch() -> bool:
    """Packaged only: swap in a staged update and start it. True when the
    new exe was started and this process should exit."""
    from sniper import updater

    exe = Path(sys.executable)
    try:
        version = updater.apply_staged(exe, __version__)
        if version is None:
            return False
        updater.relaunch(exe, sys.argv[1:], os.getpid(), updated_from=__version__)
    except OSError as e:
        event("update_install_failed", error=repr(e))
        return False
    event("update_installing", version=version, previous=__version__)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="sniper")
    parser.add_argument("--config", default=None, help="config file (default: config.yaml)")
    parser.add_argument(
        "--headless", action="store_true", help="no overlay window (server + decisions only)"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    # added by the self-updater's relaunch
    parser.add_argument("--wait-pid", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--updated-from", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_check:
        raise SystemExit(_self_check(args))
    if args.wait_pid:
        from sniper.updater import wait_for_exit

        wait_for_exit(args.wait_pid)  # the old version still holds the port
    migrated = paths.prepare_frozen_dir()
    if paths.FROZEN:
        from sniper.updater import cleanup_old

        cleanup_old(Path(sys.executable))
    config = _load(args)
    listener = logging_setup.setup(config.logging)
    event(
        "startup",
        version=__version__,
        packaged=paths.FROZEN,
        league=config.league,
        ws=f"{config.server.host}:{config.server.port}",
        headless=args.headless,
    )
    if args.updated_from:
        event("update_installed", version=__version__, previous=args.updated_from)
    if paths.FROZEN:
        event("state_dir", path=str(config.state_dir), migrated=migrated)
    restart = False
    try:
        # an update downloaded last session installs before anything binds
        if paths.FROZEN and config.updates.enabled and _install_staged_and_relaunch():
            restart = True
            return
        if args.headless:
            cache = config.state_dir / pricecache.CACHE_NAME
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(App(config, bus=None, cache_path=cache).run_async())
        else:
            restart = run_with_overlay(config, args.updated_from)
        if restart and not _install_staged_and_relaunch():
            restart = False
    finally:
        event("shutdown", restarting_for_update=restart)
        listener.stop()


def _run() -> None:
    """main(), plus a visible error for the packaged exe: it has no console,
    so a startup failure would otherwise vanish without a word."""
    if not paths.FROZEN:
        main()
        return
    try:
        main()
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            raise
        _fatal(str(e.code))
        raise SystemExit(1) from None
    except Exception:
        import traceback

        _fatal(traceback.format_exc())
        raise SystemExit(1) from None


def _fatal(message: str) -> None:
    with contextlib.suppress(Exception):
        Path("logs").mkdir(exist_ok=True)
        Path("logs", "crash.txt").write_text(message, encoding="utf-8")
    with contextlib.suppress(Exception):
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Valdo Sniper", f"Valdo Sniper could not start:\n\n{message}")
        root.destroy()


if __name__ == "__main__":
    _run()
