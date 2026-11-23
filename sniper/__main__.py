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
import sys
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from sniper import bettertrading, browser, logging_setup, pricecache
from sniper.alerts import AlertStore
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    FeedEntry,
    GameStatus,
    ListingSeen,
    Notice,
    OpeningTabs,
    PriceStatus,
    PricingHealth,
    RewardPrice,
    RewardPrices,
    TabsChanged,
    Traveled,
    WarmupStatus,
)
from sniper.config import Config, ConfigError, load_config
from sniper.gamewindow import (
    BuyerWindow,
    GameWatcher,
    buyer_alive,
    focus_poe_window,
    mark_foreground,
)
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
# Opening imported live-search tabs. Every tab load is one trade SEARCH, and
# the search limit is per IP (live 2026-11-22: 5/10s, 15/60s with a 300s
# lockout, 30/300s with a 1800s lockout), shared with the pricer. The first
# SEARCH_BURST tabs open SEARCH_SPACING_S apart (10 in a minute), the rest
# SEARCH_SPACING_SLOW_S apart, so even 30 searches stay under 30 per 5 min;
# the pricer is paused for the whole run.
# ponytail: fixed schedule, not header-driven; tab loads never show us headers.
SEARCH_SPACING_S = 6.0
SEARCH_BURST = 10
SEARCH_SPACING_SLOW_S = 15.0
# On launch, give tabs restored by Chrome this long to reconnect before
# opening the saved searches that are still missing.
# (the browser script's reconnect backoff tops out at 30s plus jitter)
AUTO_OPEN_GRACE_S = 45.0
# A tab this app opened counts as open for this long even before it connects
# (Chrome's local-network prompt, a slow load), so a second click or the
# launch auto-open never stacks a duplicate set.
OPENED_GRACE_S = 120.0


class App:
    """Owns the asyncio-side objects; every method here runs on the asyncio
    thread unless noted."""

    def __init__(
        self,
        config: Config,
        bus: Bus | None,
        cache_path: Path | None = None,
        searches_path: Path | None = None,
    ):
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
        # the marked buyer client (two-client setups); None = any PoE window
        self.buyer: BuyerWindow | None = None
        self._buyer_lost = False
        # win32 calls on the press path get their own thread, so a busy
        # default executor (pricing, process scans) can never delay a press
        self._win_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="win32")
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
        self._setup_url = f"http://{config.server.host}:{config.server.port}/"
        self.searches_path = searches_path
        self._opening = False  # a live-search open run is in progress
        self._stop_opening = False  # Stop button / hotkey: open no more tabs
        self._opened_at: dict[str, float] = {}  # slug -> monotonic, this session
        self._pricer = None  # TradePricer once trade pricing starts
        self.reload_saved_rewards()

    # ------------------------------------------------------------ callbacks

    def _hold_listing(self, decision: Decision) -> bool:
        """True when this listing must not surface yet: during the startup
        warm-up a reward with no primary price would be judged against the
        poe.ninja median (or nothing), producing exactly the inaccurate
        profit figures the warm-up exists to avoid. The listing is still
        logged, and its reward still registers with the server so the price
        loop picks it up."""
        # A generic (non-Valdo) listing has no reward to price, so the
        # warm-up has nothing to wait for - holding it would mean never
        # showing it at all.
        if decision.listing.generic:
            return False
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
            # minute steps: the overlay redraws the panel whenever a row
            # changes, and a per-second age made it redraw every 2s poll
            age = self.book.price_age_s(reward)
            rows.append((reward, state, price, None if age is None else age // 60 * 60))
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

    def reload_saved_rewards(self) -> None:
        """Price every imported search's reward from now on (and so keep it
        auto-refreshed), whether or not its tab is open yet."""
        if self.searches_path is None:
            return
        self.server.saved_rewards = frozenset(s.reward for s in self._saved_searches() if s.reward)

    def _off_path(self) -> Path:
        return self.searches_path.with_name(bettertrading.OFF_NAME)

    def search_choices(self) -> list[tuple[bettertrading.SavedSearch, bool]]:
        """Every imported search and whether it is checked to open (and price)."""
        if self.searches_path is None:
            return []
        off = bettertrading.load_off(self._off_path())
        return [(s, s.slug not in off) for s in bettertrading.load(self.searches_path).searches]

    def set_search_enabled(self, slug: str, enabled: bool) -> None:
        off = bettertrading.load_off(self._off_path())
        bettertrading.save_off(self._off_path(), off - {slug} if enabled else off | {slug})
        self.reload_saved_rewards()

    def _saved_searches(self):
        """The checked searches: the only ones opened and priced."""
        return [s for s, on in self.search_choices() if on]

    def _missing_searches(self, searches) -> list:
        """Saved searches with no connected tab yet. A tab reports either the
        slug it loaded or, for a Better Trading 1.9 query slug, its reward.
        A reward only stands in for a search when no other saved search
        shares it: two Headhunter searches can't both be "the" Headhunter tab."""
        tabs = self.server.tabs.values()
        open_ids = {t.search_id for t in tabs}
        open_rewards = {t.search_reward for t in tabs if t.search_reward}
        per_reward = Counter(s.reward for s in self._saved_searches() if s.reward)
        now = time.monotonic()
        return [
            s
            for s in searches
            if s.slug not in open_ids
            and not (per_reward[s.reward] == 1 and s.reward in open_rewards)
            and now - self._opened_at.get(s.slug, -OPENED_GRACE_S) >= OPENED_GRACE_S
        ]

    async def open_live_searches(self, auto: bool = False) -> None:
        """Open every imported Better Trading search that has no live tab yet,
        in the sniper Chrome. Paced under the trade site's search limit (see
        SEARCH_SPACING_S), with reward pricing paused meanwhile so the two
        never add up to a lockout. A second click during a run is ignored."""
        if self._opening or self.searches_path is None:
            return
        searches = self._saved_searches()
        league = self.book.league
        if not searches:
            text = (
                "Every search is unchecked - tick some under Live searches"
                if self.search_choices()
                else "No searches imported yet - paste a Better Trading export first"
            )
            self._notice(text, False)
            return
        if league is None:
            if not auto:
                self._notice("League not known yet - try again in a few seconds", False)
            return
        if browser.is_first_run():
            if auto:  # Chrome only opens on a click until the profile exists
                return
            # no Tampermonkey there yet: live tabs would capture nothing
            await self._run_blocking(browser.open_url, self._setup_url)
            self._notice("First run: finish the setup page in the new Chrome, then click again")
            return
        todo = self._missing_searches(searches)
        if not todo:
            if not auto:
                self._notice(f"All {len(searches)} live searches are already open")
            return
        self._opening = True
        self._stop_opening = False
        if self.bus:
            self.bus.put(OpeningTabs(True))
        try:
            await self._open_paced(todo, league)
        finally:
            self._opening = False
            if self.bus:
                self.bus.put(OpeningTabs(False))

    async def stop_opening(self) -> None:
        """Stop button or hotkey: the current run opens no further tabs.
        A coroutine so the global hotkey can schedule it like the others."""
        if self._opening:
            self._stop_opening = True
            event("open_live_searches_stopped")

    async def _open_paced(self, todo, league: str) -> None:
        n = len(todo)
        event("open_live_searches", count=n, league=league)
        for i, search in enumerate(todo):
            if i:
                await asyncio.sleep(SEARCH_SPACING_S if i < SEARCH_BURST else SEARCH_SPACING_SLOW_S)
            if not self._missing_searches([search]):
                continue  # its tab connected while we were waiting
            while self._pricer is not None and self._pricer.in_backoff and not self._stop_opening:
                # the IP is locked out: a tab opened now would load empty
                wait = max(self._pricer.backoff_remaining, 1)
                self._notice(f"Trade site rate limit - waiting {wait:.0f}s", False)
                await asyncio.sleep(min(wait, 15))
            if self._stop_opening:
                self._notice(f"Stopped opening live searches at {i}/{n}")
                return
            await self._run_blocking(browser.open_url, bettertrading.live_url(search, league))
            self._opened_at[search.slug] = time.monotonic()
            self._notice(f"Opening live searches {i + 1}/{n}: {search.title}")
        self._notice(f"Opened the missing live searches ({league})")

    async def auto_open_searches(self) -> None:
        """On launch: once the league is known and restored tabs have had a
        moment to reconnect, open the saved searches that are still missing.
        Only when searches were already saved at launch: a first import
        opens on the user's click, after they have ticked what they want."""
        if not self._saved_searches():
            return
        await asyncio.sleep(AUTO_OPEN_GRACE_S)
        while self.book.league is None:
            await asyncio.sleep(2)
        await self.open_live_searches(auto=True)

    def _notice(self, text: str, ok: bool = True) -> None:
        if self.bus:
            self.bus.put(Notice(text, ok))

    async def _run_blocking(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def mark_buyer(self) -> None:
        """Mark-buyer key: remember the focused PoE client as the buyer.
        Local only - nothing is sent to the game or the trade site."""
        title = self.config.game.window_title
        try:
            buyer = await asyncio.get_running_loop().run_in_executor(
                self._win_pool, mark_foreground, title
            )
        except Exception as e:  # noqa: BLE001 - a press must always answer
            event("buyer_mark_failed", error=repr(e))
            buyer = None
        if buyer is not None:
            self.buyer = buyer
            self._buyer_lost = False
        if self.bus:
            self.bus.put(GameStatus(self.watcher.running.is_set(), self._buyer_state()))
            if buyer is None:
                self.bus.put(ClickOutcome("", False, "mark buyer: focus a PoE window first"))

    def _buyer_state(self) -> str:
        if self.buyer is None:
            return ""
        return "lost" if self._buyer_lost else "marked"

    async def _buyer_gate(self) -> bool:
        """With a buyer marked, travel only while THAT client is alive: any
        PoE process passing the gate would send account A to a hideout with
        only client B in game. Checked before anything is consumed."""
        if self.buyer is None:
            return True
        try:
            alive = await asyncio.get_running_loop().run_in_executor(
                self._win_pool, buyer_alive, self.buyer, self.config.game.window_title
            )
        except Exception as e:  # noqa: BLE001 - fail closed, but say so
            event("buyer_check_failed", error=repr(e))
            alive = False
        if alive:
            self._buyer_lost = False
            return True
        self._buyer_lost = True
        event("travel_blocked", reason="buyer_lost", pid=self.buyer.pid)
        if self.bus:
            self.bus.put(GameStatus(self.watcher.running.is_set(), "lost"))
            self.bus.put(ClickOutcome("", False, "buyer client lost - press mark key again"))
        return False

    async def hotkey_pressed(self) -> None:
        """One user press = at most one click_travel (hard ToS constraint).
        Runs on the asyncio loop, scheduled by the keyboard hook thread."""
        if not await self._buyer_gate():
            return
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
        listing, once ever, with the same game gate as the hotkey."""
        import time

        from sniper.alerts import Alert

        if not self.watcher.running.is_set():
            event("travel_click_ignored", reason="poe_not_running", listing_id=listing_id)
            if self.bus:
                self.bus.put(ClickOutcome(listing_id, False, "PoE not running - click ignored"))
            return
        if not await self._buyer_gate():
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
            net_div=round(alert.decision.net_div or 0, 2),
        )
        await self._travel(alert)

    async def _travel(self, alert) -> None:
        lid = alert.decision.listing.listing_id
        ok = await self.server.send_click_travel(lid)
        # pin + clipboard only for a click that actually left; a dead tab
        # must not overwrite the clipboard or pin a travel that never happened
        if ok and self.bus:
            self.bus.put(Traveled(alert.view()))
        if not ok:
            # the click never left the building, so no click_result is coming:
            # settle it here or the runners-up would be lost to a travel that
            # never happened
            if self.store.settle_travel(lid, False):
                self._push_alerts(new_alert=False)
            if self.bus:
                self.bus.put(ClickOutcome(lid, False, "tab gone - travel not sent"))
        # click first (latency-critical), then bring the game to front
        try:
            focused = await asyncio.get_running_loop().run_in_executor(
                self._win_pool, focus_poe_window, self.config.game.window_title, self.buyer
            )
        except Exception as e:  # noqa: BLE001
            event("focus_failed", reason=repr(e))
            focused = False
        if not focused and ok and self.bus:
            self.bus.put(ClickOutcome(lid, True, "travel sent - switch to the game yourself"))

    # ------------------------------------------------------------ live tuning

    def apply_tuning(self, scoring_config, min_yield: float, running_cost: float = 1.0) -> None:
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
            )
        )
        event(
            "tuning_updated",
            min_yield=min_yield,
            running_cost=running_cost,
            base_default=scoring_config.base_default,
            rules={
                r.label: {"min_base": r.min_base, "multiplier": r.multiplier}
                for r in scoring_config.rules
            },
        )

    def set_jackpot_thresholds(self, min_net: float, min_yield: float) -> None:
        """Settings panel jackpot bars; runs on the asyncio thread."""
        self.store.set_jackpot_thresholds(min_net, min_yield)
        event("jackpot_thresholds_updated", min_net=min_net, min_yield=min_yield)

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
                session_invalid=pricer.session_invalid,
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

        def age_s(reward: str, now: float) -> float:
            if reward in last_priced:
                return now - last_priced[reward]
            # a price restored from the cache is due when its ORIGINAL fetch
            # ages past the interval, not at once: re-pricing every reward
            # on each restart spent a whole pass of searches for nothing
            cached = self.book.trade_age_s(reward)
            return interval_s if cached is None else cached

        while True:
            league = self.book.league  # resolves via the first ninja refresh
            # tab loads spend the same search budget: price after they open
            if league is None or pricer.in_backoff or self._opening:
                if self._opening:  # a long run must not end the warm-up hold
                    self._warmup_deadline = time.monotonic() + WARMUP_STALL_S
                self._update_warmup()  # keeps the stall timer honest
                self._push_pricing_health(pricer)  # keep the pill counting down
                # a tab opened while we are stalled is still an active reward
                # the user can hover; without this its row never appears
                self._push_reward_prices()
                await asyncio.sleep(2 if self._warmup_active else 15)
                continue
            now = time.monotonic()
            # a reward that just had a listing goes first (Exilium prices on
            # demand): that is the price an alert is waiting on right now
            due = sorted(
                (r for r in self.server.active_rewards() if age_s(r, now) >= interval_s),
                key=lambda r: (-self.server.last_seen(r), r),
            )
            for reward in due:
                if self._opening:
                    break  # unpriced rewards stay due; resume after the tabs
                self._pricing_now = reward  # so the startup panel can say so
                self._update_warmup()
                try:
                    listings, mode = await pricer.fetch_reward_listings(league, reward)
                except TradeBackoff as e:
                    # every further call would be refused locally until the
                    # backoff lifts, so stop the batch rather than spin. The
                    # unpriced rewards never got a last_priced stamp, so they
                    # are still due and go first on the next pass.
                    if pricer.session_invalid:
                        event("trade_session_invalid", reason=str(e))
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
                    # an HTTP refusal for THIS reward (400/404) would repeat
                    # every 15s pass: retry it at its next refresh instead,
                    # and stop the warm-up waiting on it. Anything else (a
                    # bad payload) may be transient: retry soon.
                    if isinstance(e, httpx.HTTPStatusError):
                        last_priced[reward] = time.monotonic()
                        self._warmup_settled.add(reward)
                    self._pricing_now = None
                    # the failed request still spent budget: keep the pace
                    await pricer.pause_between_rewards()
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
                self.bus.put(GameStatus(running, self._buyer_state()))

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

            pricer = self._pricer = TradePricer(
                base_url=self.config.trade_pricing.base_url,
                max_listings=self.config.trade_pricing.max_listings,
                corrupted_uniques=tuple(self.config.trade_pricing.corrupted_uniques),
                min_unid_listings=self.config.trade_pricing.min_unid_listings,
                listing_status=self.config.trade_pricing.listing_status,
            )
            tasks.append(asyncio.create_task(self.trade_price_loop(pricer), name="trade-price"))
        if self.searches_path is not None:
            tasks.append(asyncio.create_task(self.auto_open_searches(), name="auto-open"))
        await asyncio.gather(*tasks)


def run_with_overlay(config: Config, config_path: str) -> None:
    import tkinter as tk
    from pathlib import Path

    from sniper.config import SCORING_OVERRIDES_NAME
    from sniper.hotkey import Hotkey, HotkeyError
    from sniper.overlay import Overlay

    bus = Bus()
    # the cache lives beside config.yaml, like scoring_overrides.yaml
    searches_path = Path(config_path).with_name(bettertrading.SEARCHES_NAME)
    app = App(
        config,
        bus,
        cache_path=Path(config_path).with_name(pricecache.CACHE_NAME),
        searches_path=searches_path,
    )
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
    mark_key = None
    if config.hotkey.mark_buyer.strip():
        try:
            mark_key = Hotkey(config.hotkey.mark_buyer, loop, app.mark_buyer, app.watcher.running)
        except HotkeyError as e:
            event("mark_buyer_unavailable", error=str(e))  # travel still works

    stop_key = None
    if config.hotkey.stop_opening.strip():
        always = threading.Event()
        always.set()  # stopping tab opens needs no game running
        try:
            stop_key = Hotkey(config.hotkey.stop_opening, loop, app.stop_opening, always)
        except HotkeyError as e:
            event("stop_opening_key_unavailable", error=str(e))  # the button still works

    # UI-thread callbacks marshal into the asyncio loop; nothing UI-side
    # touches App state directly.
    def on_travel(listing_id: str) -> None:
        asyncio.run_coroutine_threadsafe(app.travel_listing(listing_id), loop)

    def on_settings_change(
        scoring_config, min_yield: float, combo: str, running_cost: float
    ) -> str | None:
        """Returns an error string (shown in the panel) or None. The hotkey
        rebinds on this (UI) thread; scoring/threshold swap on the loop."""
        if combo.strip().lower() == config.hotkey.mark_buyer.strip().lower():
            error = f"{combo!r} is the mark-buyer key - pick another"
        else:
            error = hotkey.rebind(combo)
        loop.call_soon_threadsafe(app.apply_tuning, scoring_config, min_yield, running_cost)
        return error

    def on_price_override(reward: str, divine: float | None) -> None:
        loop.call_soon_threadsafe(app.set_price_override, reward, divine)

    def on_open_searches() -> None:
        future = asyncio.run_coroutine_threadsafe(app.open_live_searches(), loop)
        future.add_done_callback(
            lambda f: f.exception() and event("open_live_searches_error", error=repr(f.exception()))
        )

    def on_import_searches(text: str):
        """UI thread: validate + save the paste (a small local file) and
        price the new rewards. Tabs open from the import window's button,
        after the user has ticked which ones."""
        result = bettertrading.save(searches_path, text)
        event("searches_imported", count=len(result.searches), skipped=list(result.skipped))
        if result.searches:
            loop.call_soon_threadsafe(app.reload_saved_rewards)
        return result

    def on_jackpot_change(min_net: float, min_yield: float) -> None:
        loop.call_soon_threadsafe(app.set_jackpot_thresholds, min_net, min_yield)

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
        on_jackpot_change=on_jackpot_change,
        on_open_searches=on_open_searches,
        on_import_searches=on_import_searches,
        searches_count=lambda: len(bettertrading.load(searches_path).searches),
        search_choices=app.search_choices,
        on_stop_opening=lambda: asyncio.run_coroutine_threadsafe(app.stop_opening(), loop),
        stop_opening_key=config.hotkey.stop_opening,
        on_toggle_search=lambda slug, on: loop.call_soon_threadsafe(
            app.set_search_enabled, slug, on
        ),
        overrides_path=Path(config_path).with_name(SCORING_OVERRIDES_NAME),
    )
    try:
        root.mainloop()
    finally:
        hotkey.unregister()
        if mark_key is not None:
            mark_key.unregister()
        if stop_key is not None:
            stop_key.unregister()
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
