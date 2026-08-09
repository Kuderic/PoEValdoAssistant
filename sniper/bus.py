"""The only channel between the asyncio thread and the tkinter main thread:
a plain thread-safe queue of immutable UI events, drained by the overlay
via root.after polling."""

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


UiEvent = AlertsChanged | TabsChanged | PriceStatus | ClickOutcome | GameStatus | Traveled


class Bus:
    def __init__(self) -> None:
        self._q: queue.Queue[UiEvent] = queue.Queue()

    def put(self, event: UiEvent) -> None:
        self._q.put_nowait(event)

    def drain(self) -> list[UiEvent]:
        events: list[UiEvent] = []
        while True:
            try:
                events.append(self._q.get_nowait())
            except queue.Empty:
                return events
