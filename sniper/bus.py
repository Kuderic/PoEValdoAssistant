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
class HotkeyPressed:
    """The travel hotkey was pressed (whatever it then does); lets the
    overlay come to the front when the user has asked for that."""


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
    net_div: float | None  # None when no reference/rate exists
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
    # from a non-Valdo live search: profit/P100D/difficulty are meaningless
    generic: bool = False
    # an alerting row that was big net AND big yield; the feed keeps it
    # highlighted so a missed one is unmistakable minutes later
    jackpot: bool = False
    super_jackpot: bool = False  # the tier above (implies jackpot)
    # failure risks (label, note, chance) and their combined chance
    risks: tuple[tuple[str, str, float], ...] = ()
    fail_chance: float = 0.0


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
class RewardPrice:
    """What the app currently thinks one active reward is worth.

    Carries both the price in force and, separately, the automatic one: the
    settings panel lets the user override a reference that is plainly wrong,
    and an override is only judgeable next to the figure it replaced.
    """

    reward: str
    amount: float | None  # display amount of the reference actually in force
    currency: str
    source: str  # override | manual | trade | cached | live | stale | unpriced
    age_s: float | None  # None when the price has no calculation time
    # divine value of the session override in force, None when there is none
    override_div: float | None = None
    # the price that applies with no override - what clearing one returns to
    auto_amount: float | None = None
    auto_currency: str = ""


@dataclass(frozen=True)
class RewardPrices:
    """Current reference price per active reward, for the Searching tooltip
    and the settings panel's price editor.

    There is one entry per ACTIVE reward, always - a reward with no price yet
    carries amount None (and source "unpriced") rather than being left out.
    Dropping them made the tooltip answer with silence for exactly the reward
    the user was hovering to ask about."""

    entries: tuple[RewardPrice, ...]


UiEvent = (
    AlertsChanged
    | TabsChanged
    | PriceStatus
    | ClickOutcome
    | GameStatus
    | Traveled
    | HotkeyPressed
    | ListingSeen
    | RewardPrices
    | WarmupStatus
    | PricingHealth
)


@dataclass(frozen=True)
class UpdateStatus:
    """A newer release (sniper.updater). state:
    "available"   - announce only (source checkout, or auto_download off);
    "downloading" - the packaged exe is fetching it in the background;
    "ready"       - verified and staged: clicking the pill restarts into it
                    (it also installs at the next launch);
    "failed"      - the download did not verify; retried later;
    "installed"   - this run IS the update (shown briefly after a restart).
    """

    state: str
    version: str
    url: str  # the release page
    detail: str = ""


class Bus:
    def __init__(self) -> None:
        self._q: queue.Queue[UiEvent] = queue.Queue()
        self._waker = None  # Callable[[], None] | None, set by the overlay

    def set_waker(self, waker) -> None:
        """Register a callback invoked after every put, from the producing
        thread - so it must never block (the producer is the detection
        path). The overlay's sets a flag that its waker thread turns into a
        Tk wake, so events render immediately instead of on the next poll
        tick."""
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
