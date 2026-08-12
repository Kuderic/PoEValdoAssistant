"""The only channel between the asyncio thread and the tkinter main thread:
a plain thread-safe queue of immutable UI events. The overlay registers a
waker so each put wakes the Tk drain immediately (a slow root.after poll
remains as the safety net)."""

from __future__ import annotations

import queue
from dataclasses import dataclass

from sniper.alerts import AlertView


@dataclass(frozen=True)
class AlertsChanged:
    alerts: tuple[AlertView, ...]
    new_alert: bool  # True -> the overlay plays the alert sound
    # The just-arrived alert (may rank below the display cut); carries its
    # own created_monotonic so the latency log never misattributes.
    new_view: AlertView | None = None


@dataclass(frozen=True)
class TabsChanged:
    tabs: tuple[dict, ...]


@dataclass(frozen=True)
class PriceStatus:
    status: str  # live | stale | manual
    league: str | None


@dataclass(frozen=True)
class ClickOutcome:
    listing_id: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class GameStatus:
    running: bool


@dataclass(frozen=True)
class Traveled:
    """A travel click just went out for this listing; the overlay pins its
    mods for a few seconds so they're readable during the loading screen."""

    view: AlertView


@dataclass(frozen=True)
class FeedEntry:
    """One live-feed row: every incoming listing, alerting or not."""

    listing_id: str
    key: str
    amount: float
    currency: str
    profit_div: float | None  # None when no reference/rate exists
    difficulty: float
    verdict: str  # alert | below_threshold | blocked | no_reference | no_rate
    # (mod text, scoring note e.g. "×1.8"/"") shown in the hover tooltip
    mods: tuple[tuple[str, str], ...] = ()
    received_monotonic: float = 0.0  # for the feed's minutes-ago column
    # "trade" | "manual" | "live" | "stale" - the last two mean the price
    # came from poe.ninja's fallback median, which the row flags as [est]
    reference_source: str = ""
    # deadly mod pairings: (label, note, multiplier), shown on hover
    pairings: tuple[tuple[str, str, float], ...] = ()
    # head start other snipers had on this listing; None when unknown
    index_lag_ms: float | None = None


@dataclass(frozen=True)
class ListingSeen:
    entry: FeedEntry


@dataclass(frozen=True)
class WarmupStatus:
    """Startup price warm-up. Until a reward has a primary (trade/manual)
    price, its listings are held back rather than judged against poe.ninja's
    inaccurate median - the overlay shows 'Calculating prices…' meanwhile."""

    calculating: bool
    priced: int
    total: int
    # per-reward progress for the startup panel:
    # (reward, state, price text, age_seconds) where state is
    # "done" | "working" | "pending" | "unpriced" and age_seconds is how
    # long ago that price was calculated (None when there is no price)
    rewards: tuple[tuple[str, str, str, float | None], ...] = ()


@dataclass(frozen=True)
class PricingHealth:
    """Trade-API pricing health. `backoff_s` > 0 means we are rate limited
    and every reward priced meanwhile falls back to poe.ninja's inaccurate
    median, so the header says so instead of showing a healthy pill."""

    backoff_s: float
    unpriced: int
    total: int


@dataclass(frozen=True)
class RewardPrices:
    """Current reference price per active reward, for the Searching tooltip:
    (reward, amount, currency, source, age_seconds) tuples. age_seconds is
    None when the price has no calculation time (manual or poe.ninja)."""

    entries: tuple[tuple[str, float, str, str, float | None], ...]


UiEvent = (
    AlertsChanged
    | TabsChanged
    | PriceStatus
    | ClickOutcome
    | GameStatus
    | Traveled
    | ListingSeen
    | RewardPrices
    | WarmupStatus
    | PricingHealth
)


class Bus:
    def __init__(self) -> None:
        self._q: queue.Queue[UiEvent] = queue.Queue()
        self._waker = None  # Callable[[], None] | None, set by the overlay

    def set_waker(self, waker) -> None:
        """Register a callback invoked after every put, from the producing
        thread. The overlay uses it to event_generate a Tk wake so events
        render immediately instead of on the next poll tick."""
        self._waker = waker

    def put(self, event: UiEvent) -> None:
        self._q.put_nowait(event)
        waker = self._waker
        if waker is not None:
            waker()

    def drain(self) -> list[UiEvent]:
        events: list[UiEvent] = []
        while True:
            try:
                events.append(self._q.get_nowait())
            except queue.Empty:
                return events
