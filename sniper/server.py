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
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from sniper import margin
from sniper.config import Config
from sniper.logging_setup import event
from sniper.models import ClickResult, Decision, FrameError, Hello, Listing, parse_frame
from sniper.modrules import ModRules, ModScoring
from sniper.paths import resource
from sniper.prices import PriceBook
from sniper.tablag import TabLagMonitor

_ROUTE_CAP = 1000
# The userscript version this build expects. A browser tab keeps running the
# script it loaded with until it is re-pasted AND hard-reloaded, so a stale
# tab silently loses whatever the new version added - new fields simply
# arrive missing, which reads as a broken feature rather than an old script.
# KEEP IN SYNC with @version in userscript/valdo-sniper.user.js.
EXPECTED_USERSCRIPT_VERSION = "0.8.5"

# The app serves its own userscript over plain HTTP on the websocket port, so
# installing is one click (Tampermonkey intercepts any *.user.js URL) and the
# script's @updateURL points back here: Tampermonkey then keeps every browser
# on exactly the version this running app expects.
INSTALL_PATH = "/valdo-sniper.user.js"
# bundled inside the exe when packaged (sniper.paths)
USERSCRIPT_FILE = resource("userscript/valdo-sniper.user.js")
_LANDING = (
    "<!doctype html><meta charset=utf-8><title>Valdo Sniper</title>"
    "<body style='font:15px Segoe UI,sans-serif;background:#0f1216;color:#e6eaef;padding:32px'>"
    "<h2>Valdo Sniper is running</h2>"
    "<p><b>Before installing</b>, enable user scripts, or Chrome runs nothing:</p><ol>"
    "<li>Install Tampermonkey from the Chrome Web Store.</li>"
    "<li>Open <code>chrome://extensions</code>, click Tampermonkey's <b>Details</b>,"
    " and turn on <b>Allow User Scripts</b>. On Chrome older than 138, turn on"
    " <b>Developer mode</b> (top-right) instead.</li>"
    f"<li><a style='color:#5fd069' href='{INSTALL_PATH}'>Install the browser userscript</a>"
    " and press <b>Install</b>.</li>"
    "<li>Reload your trade live-search tabs.</li></ol></body>"
)


def _http_response(status: int, reason: str, body: bytes, content_type: str) -> Response:
    headers = Headers(
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Connection", "close"),
        ]
    )
    return Response(status, reason, headers, body)


def process_http(_connection, request: Request) -> Response | None:
    """websockets `process_request` hook: plain-HTTP GETs for the installer
    and a landing page. Websocket upgrades (the tabs) pass straight through."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?", 1)[0]
    if path == INSTALL_PATH:
        try:
            body = USERSCRIPT_FILE.read_bytes()
        except OSError:
            return _http_response(404, "Not Found", b"userscript missing\n", "text/plain")
        event("userscript_served")
        return _http_response(200, "OK", body, "text/javascript; charset=utf-8")
    if path == "/":
        return _http_response(200, "OK", _LANDING.encode(), "text/html; charset=utf-8")
    return None  # anything else: websockets answers it as a failed handshake


@dataclass
class TabState:
    ws: websockets.ServerConnection
    search_id: str
    last_hello_monotonic: float
    search_reward: str | None = None
    search_kind: str = "valdo"
    version: str | None = None  # userscript @version this tab is running


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
        self._net_stats: dict[str, dict] = {}  # tab_id -> last capture counters
        self._tab_lag = TabLagMonitor()  # spots a tab that has gone slow
        # tabs whose page is a plain (non-live) search: not listed among the
        # searches, and anything they send is dropped
        self._static_tabs: set[str] = set()

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
                self._handle,
                self._config.server.host,
                self._config.server.port,
                process_request=process_http,
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
                    # capture counters change slowly; log only on change so
                    # the reason the network path is (not) forwarding is in
                    # the log without spamming it every heartbeat
                    if frame.net_stats and self._net_stats.get(tab_id) != frame.net_stats:
                        self._net_stats[tab_id] = frame.net_stats
                        event("capture_stats", tab_id=tab_id, **frame.net_stats)
                    if not frame.live:
                        self._tab_not_live(tab_id, frame.search_id)
                        continue
                    self._static_tabs.discard(tab_id)  # back on a live search
                    self.tabs[tab_id] = TabState(
                        ws=ws,
                        search_id=frame.search_id,
                        last_hello_monotonic=time.monotonic(),
                        search_reward=frame.search_reward,
                        search_kind=frame.search_kind,
                        version=frame.version,
                    )
                    # a reload starts the tab's delay history over; the
                    # app reconnecting to a page that stayed open does not
                    self._tab_lag.page_seen(tab_id, frame.page_id, frame.page_age_s)
                    if not known:
                        event(
                            "tab_connect",
                            tab_id=tab_id,
                            search_id=frame.search_id,
                            search_reward=frame.search_reward,
                            # valdo | any: which lane this tab's listings take.
                            # Received since 0.7.6 but never recorded, which
                            # made "the generic tab stopped alerting" invisible
                            # in the logs - the only trace was generic=False on
                            # its listings, with no way to see what the tab
                            # itself had reported.
                            search_kind=frame.search_kind,
                            # a tab runs the script it loaded with until it is
                            # reloaded; logged so a missed snipe can be traced
                            # to a stale userscript
                            userscript_version=frame.version,
                        )
                        self._notify_tabs()
                elif isinstance(frame, Listing):
                    if frame.tab_id in self._static_tabs:
                        # a plain search's rows were not pushed: never alert
                        event(
                            "listing_ignored",
                            listing_id=frame.listing_id,
                            tab_id=frame.tab_id,
                            reason="not_live",
                        )
                        continue
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
            if tab_id:
                self._static_tabs.discard(tab_id)
            if tab_id and self.tabs.get(tab_id) and self.tabs[tab_id].ws is ws:
                del self.tabs[tab_id]
                self._net_stats.pop(tab_id, None)
                self._tab_lag.forget(tab_id)
                event("tab_disconnect", tab_id=tab_id)
                self._notify_tabs()

    def _tab_not_live(self, tab_id: str, search_id: str) -> None:
        """The tab is on a plain search page (or navigated away from its
        live search): drop it from the searches until it goes live again."""
        if tab_id not in self._static_tabs:
            self._static_tabs.add(tab_id)
            event("tab_not_live", tab_id=tab_id, search_id=search_id)
        if self.tabs.pop(tab_id, None) is not None:
            self._net_stats.pop(tab_id, None)
            self._tab_lag.forget(tab_id)
            self._notify_tabs()

    # --------------------------------------------------------------- hot path

    def _handle_listing(self, listing: Listing) -> None:
        received = time.monotonic()
        if listing.reward:
            self._recent_rewards[listing.reward] = received
            # a tab's listings identify its search even when the page had no
            # visible rows to scrape at hello time
            tab = self.tabs.get(listing.tab_id)
            if tab is not None and tab.search_reward != listing.reward:
                tab.search_reward = listing.reward
                self._notify_tabs()
        elif listing.generic:
            # a reward-less listing proves the tab is a non-Valdo live search
            # even when its hello said otherwise (see models.parse_frame), so
            # the status strip shows it as one rather than "unidentified"
            tab = self.tabs.get(listing.tab_id)
            if tab is not None and tab.search_kind != "any":
                tab.search_kind = "any"
                self._notify_tabs()
        self._listing_tab[listing.listing_id] = listing.tab_id
        while len(self._listing_tab) > _ROUTE_CAP:
            self._listing_tab.popitem(last=False)

        event(
            "listing_received",
            listing_id=listing.listing_id,
            tab_id=listing.tab_id,
            item=listing.item_name,
            reward=listing.reward,
            amount=listing.price.amount,
            currency=listing.price.currency,
            seller=listing.seller,
            search_id=listing.search_id,
            detected_at=listing.detected_at,
            indexed_at=listing.indexed_at,
            capture=listing.capture,
            # why a listing did or did not take the generic (non-Valdo)
            # lane; without it a stale userscript that never sets the flag
            # is indistinguishable from a reward that failed to price
            generic=listing.generic,
            # how long it was already live before we saw it: the head start
            # every other sniper had. Null on DOM-captured listings.
            index_lag_ms=None if listing.index_lag_ms is None else round(listing.index_lag_ms),
            # seller's tab and cell (network capture only); logged so the
            # grid size the overlay assumes can be checked against reality
            stash=None
            if listing.stash is None
            else {"name": listing.stash.name, "x": listing.stash.x, "y": listing.stash.y},
            # index_lag_ms split up: GGG -> push, push -> the page's fetch,
            # the fetch itself, and whether the tab was in the background
            index_to_push_ms=None
            if listing.index_to_push_ms is None
            else round(listing.index_to_push_ms),
            push_to_fetch_ms=None if listing.timing is None else listing.timing.push_to_fetch_ms,
            fetch_ms=None if listing.timing is None else listing.timing.fetch_ms,
            tab_hidden=None if listing.timing is None else listing.timing.hidden,
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
            net_div=None if decision.net_div is None else round(decision.net_div, 2),
            margin=None if decision.margin is None else round(decision.margin, 4),
            threshold_div=decision.threshold,
            difficulty=decision.difficulty,
            difficulty_mods=list(decision.difficulty_mods),
            fail_chance=round(decision.fail_chance, 4),
            risks=[f"{chance:.0%}:{label}" for label, _note, chance in decision.risks],
            special_warnings=[f"{color}:{label}" for label, color in decision.special_warnings],
            required_net_div=None
            if decision.required_net_div is None
            else round(decision.required_net_div, 2),
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
        # after the alert is on its way: bookkeeping, not detection
        self._track_lag(listing)

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

    def _track_lag(self, listing: Listing) -> None:
        """Feed the slow-tab monitor; log and publish a change of verdict."""
        lag = listing.index_lag_ms
        if lag is None or listing.tab_id not in self.tabs:
            return
        changed = self._tab_lag.record(listing.tab_id, lag)
        if changed is None:
            return
        recent, base, _slow = self._tab_lag.status(listing.tab_id)
        event(
            "tab_slow" if changed else "tab_recovered",
            tab_id=listing.tab_id,
            search_reward=self.tabs[listing.tab_id].search_reward,
            recent_lag_ms=None if recent is None else round(recent),
            fresh_lag_ms=None if base is None else round(base),
        )
        self._notify_tabs()

    def tab_snapshot(self) -> list[dict]:
        now = time.monotonic()
        rows = []
        for tid, t in self.tabs.items():
            recent, base, slow = self._tab_lag.status(tid)
            rows.append(
                {
                    "tab_id": tid,
                    "search_id": t.search_id,
                    "search_reward": t.search_reward,
                    "search_kind": t.search_kind,
                    "version": t.version,
                    "hello_age_s": round(now - t.last_hello_monotonic, 1),
                    # listing delay: recent median, median when freshly
                    # loaded, and whether it has crept up enough to reload
                    "lag_ms": None if recent is None else round(recent),
                    "fresh_lag_ms": None if base is None else round(base),
                    "slow": slow,
                }
            )
        return rows

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
