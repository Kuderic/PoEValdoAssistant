"""Websocket server: accepts live-search tabs, validates frames, runs the
detection -> decision hot path, and routes click_travel back to the exact
tab that reported a listing."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import websockets

from sniper import margin
from sniper.config import Config
from sniper.logging_setup import event
from sniper.models import ClickResult, Decision, FrameError, Hello, Listing, parse_frame
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook

_ROUTE_CAP = 1000


@dataclass
class TabState:
    ws: websockets.ServerConnection
    search_id: str
    last_hello_monotonic: float
    search_reward: str | None = None


class SniperServer:
    def __init__(
        self,
        config: Config,
        book: PriceBook,
        rules: ModRules,
        scoring: ModScoring | None = None,
        on_decision: Callable[[Decision], None] | None = None,
        on_click_result: Callable[[ClickResult], None] | None = None,
        on_tabs_changed: Callable[[], None] | None = None,
    ):
        self._config = config
        self._book = book
        self._rules = rules
        self._scoring = scoring or ModScoring(config.mod_scoring)
        self._thresholds = config.thresholds
        self._on_decision = on_decision
        self._on_click_result = on_click_result
        self._on_tabs_changed = on_tabs_changed
        self.tabs: dict[str, TabState] = {}
        self._listing_tab: OrderedDict[str, str] = OrderedDict()  # listing_id -> tab_id
        self._recent_rewards: dict[str, float] = {}  # reward -> last seen monotonic

    def set_scoring(self, scoring: ModScoring) -> None:
        """Swap the difficulty engine (tuning panel). Call from the asyncio
        thread (via call_soon_threadsafe from the UI)."""
        self._scoring = scoring

    def set_thresholds(self, thresholds) -> None:
        """Swap profit thresholds live (tuning panel). Asyncio thread only."""
        self._thresholds = thresholds

    # ---------------------------------------------------------------- serving

    async def serve_forever(self) -> None:
        try:
            server = await websockets.serve(
                self._handle, self._config.server.host, self._config.server.port
            )
        except OSError as e:
            raise SystemExit(
                f"cannot bind ws://{self._config.server.host}:{self._config.server.port}"
                f" ({e.strerror or e}). Is another sniper or echo server still running?"
            ) from None
        async with server:
            event("server_started", host=self._config.server.host, port=self._config.server.port)
            await asyncio.Future()

    async def _handle(self, ws: websockets.ServerConnection) -> None:
        tab_id: str | None = None
        try:
            async for raw in ws:
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    event("frame_rejected", reason="not json", raw=str(raw)[:200])
                    continue
                frame = parse_frame(decoded)
                if isinstance(frame, FrameError):
                    event("frame_rejected", reason=frame.reason, raw=frame.raw)
                elif isinstance(frame, Hello):
                    tab_id = frame.tab_id
                    known = tab_id in self.tabs
                    self.tabs[tab_id] = TabState(
                        ws=ws,
                        search_id=frame.search_id,
                        last_hello_monotonic=time.monotonic(),
                        search_reward=frame.search_reward,
                    )
                    if not known:
                        event(
                            "tab_connect",
                            tab_id=tab_id,
                            search_id=frame.search_id,
                            search_reward=frame.search_reward,
                        )
                        self._notify_tabs()
                elif isinstance(frame, Listing):
                    try:
                        self._handle_listing(frame)
                    except Exception as e:  # one bad listing must not drop the tab
                        event("pipeline_error", listing_id=frame.listing_id, error=repr(e))
                elif isinstance(frame, ClickResult):
                    event(
                        "click_result",
                        listing_id=frame.listing_id,
                        ok=frame.ok,
                        reason=frame.reason,
                    )
                    if self._on_click_result:
                        self._on_click_result(frame)
        finally:
            if tab_id and self.tabs.get(tab_id) and self.tabs[tab_id].ws is ws:
                del self.tabs[tab_id]
                event("tab_disconnect", tab_id=tab_id)
                self._notify_tabs()

    # --------------------------------------------------------------- hot path

    def _handle_listing(self, listing: Listing) -> None:
        received = time.monotonic()
        if listing.reward:
            self._recent_rewards[listing.reward] = received
        self._listing_tab[listing.listing_id] = listing.tab_id
        while len(self._listing_tab) > _ROUTE_CAP:
            self._listing_tab.popitem(last=False)

        event(
            "listing_received",
            listing_id=listing.listing_id,
            item=listing.item_name,
            reward=listing.reward,
            amount=listing.price.amount,
            currency=listing.price.currency,
            seller=listing.seller,
            search_id=listing.search_id,
            detected_at=listing.detected_at,
            mods=list(listing.mods),  # raw wording, for tuning match patterns
        )

        decision = margin.evaluate(
            listing, self._book, self._thresholds, self._rules, self._scoring
        )

        event(
            "decision",
            listing_id=listing.listing_id,
            key=decision.key,
            verdict=decision.verdict,
            profit_div=None if decision.profit_div is None else round(decision.profit_div, 2),
            margin=None if decision.margin is None else round(decision.margin, 4),
            threshold_div=decision.threshold,
            difficulty=decision.difficulty,
            difficulty_mods=list(decision.difficulty_mods),
            special_warnings=[f"{color}:{label}" for label, color in decision.special_warnings],
            required_profit_div=None
            if decision.required_profit_div is None
            else round(decision.required_profit_div, 2),
            listing_chaos=decision.listing_chaos,
            reference_chaos=decision.reference.chaos_value if decision.reference else None,
            reference_source=decision.reference.source if decision.reference else None,
            currency_mismatch=decision.currency_mismatch,
            mod_hits=[f"{h.severity}:{h.label}" for h in decision.mod_hits],
            decide_ms=round((time.monotonic() - received) * 1000, 2),
            detected_to_decided_ms=_clock_delta_ms(listing.detected_at),
        )
        if self._on_decision:
            self._on_decision(decision)

    # ------------------------------------------------------------- click path

    async def send_click_travel(self, listing_id: str) -> bool:
        """Send exactly one click_travel for a listing. Caller (hotkey path)
        guarantees this happens only on a user press."""
        tab_id = self._listing_tab.get(listing_id)
        tab = self.tabs.get(tab_id) if tab_id else None
        if tab is None:
            event("click_send_failed", listing_id=listing_id, reason="tab_gone")
            return False
        try:
            await tab.ws.send(
                json.dumps(
                    {"type": "click_travel", "search_id": tab.search_id, "listing_id": listing_id}
                )
            )
        except websockets.ConnectionClosed:
            event("click_send_failed", listing_id=listing_id, reason="connection_closed")
            return False
        event("click_sent", listing_id=listing_id, tab_id=tab_id)
        return True

    # ------------------------------------------------------------------ misc

    def active_rewards(self) -> set[str]:
        """Rewards the trade pricer should track: what connected tabs search
        for, plus rewards seen in recent listings (fallback when a tab's
        reward scrape failed)."""
        now = time.monotonic()
        self._recent_rewards = {r: t for r, t in self._recent_rewards.items() if now - t < 1800}
        rewards = set(self._recent_rewards)
        rewards.update(t.search_reward for t in self.tabs.values() if t.search_reward)
        return rewards

    def tab_snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "tab_id": tid,
                "search_id": t.search_id,
                "search_reward": t.search_reward,
                "hello_age_s": round(now - t.last_hello_monotonic, 1),
            }
            for tid, t in self.tabs.items()
        ]

    def _notify_tabs(self) -> None:
        if self._on_tabs_changed:
            self._on_tabs_changed()


def _clock_delta_ms(detected_at: str) -> float | None:
    """Browser-clock to local-clock delta; approximate, logged for M5 stats."""
    try:
        detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        return round((datetime.now(UTC) - detected).total_seconds() * 1000, 1)
    except ValueError:
        return None
