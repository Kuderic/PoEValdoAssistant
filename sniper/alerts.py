"""AlertStore: the set of currently-actionable alerts.

ToS-critical semantics (DESIGN.md):
- best-value-wins: the hotkey consumes the non-expired alert with the
  highest profit per 100 difficulty (see _rank), not the biggest absolute
  profit;
- expiry: a stale listing can never be traveled to by reflex;
- single consumption: a consumed listing id can never alert or be clicked
  again, even if the tab re-pushes it;
- consume mode "all" clears runners-up too (a second press does nothing),
  "top" leaves them targetable.

Touched only from the asyncio thread -> needs no locks.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from sniper.margin import profit_per_100_difficulty
from sniper.models import Decision

_CONSUMED_CAP = 200


@dataclass(frozen=True)
class AlertView:
    """Immutable snapshot handed to the overlay via the bus."""

    listing_id: str
    key: str
    item_name: str
    amount: float
    currency: str
    profit_div: float
    required_profit_div: float
    difficulty: float
    margin: float
    seller: str
    mismatch: bool
    warn_labels: tuple[str, ...]
    special_warnings: tuple[tuple[str, str], ...]  # (label, color)
    # (mod text, scoring note e.g. "×1.8"/""); tooltip + traveled panel
    mods: tuple[tuple[str, str], ...]
    reference_amount: float
    reference_currency: str
    reference_source: str
    created_monotonic: float
    expires_at_monotonic: float


@dataclass
class Alert:
    decision: Decision
    created_monotonic: float
    expires_at_monotonic: float

    def view(self) -> AlertView:
        d = self.decision
        return AlertView(
            listing_id=d.listing.listing_id,
            key=d.key,
            item_name=d.listing.item_name,
            amount=d.listing.price.amount,
            currency=d.listing.price.currency,
            profit_div=d.profit_div or 0.0,
            required_profit_div=d.required_profit_div or 0.0,
            difficulty=d.difficulty,
            margin=d.margin or 0.0,
            seller=d.listing.seller,
            mismatch=d.currency_mismatch,
            warn_labels=tuple(h.label for h in d.mod_hits if h.severity == "warn"),
            special_warnings=d.special_warnings,
            mods=d.mods_annotated,
            reference_amount=d.reference.display_amount if d.reference else 0.0,
            reference_currency=d.reference.display_currency if d.reference else "",
            reference_source=d.reference.source if d.reference else "",
            created_monotonic=self.created_monotonic,
            expires_at_monotonic=self.expires_at_monotonic,
        )


class AlertStore:
    def __init__(self, expiry_s: float, consume_mode: str = "all", max_display: int = 3):
        self._expiry_s = expiry_s
        self._consume_mode = consume_mode
        self._max_display = max_display
        self._alerts: dict[str, Alert] = {}
        self._consumed: OrderedDict[str, float] = OrderedDict()

    # ------------------------------------------------------------- mutations

    def insert(self, decision: Decision) -> bool:
        """Add an alert-verdict decision. Returns False for duplicates and
        already-consumed listings (tab re-renders re-push rows).

        A re-listed item keeps its listing id but carries a new price. That
        is a genuinely new alert, so it REPLACES an active alert for the
        same id (otherwise the overlay would keep showing the stale price
        for the rest of the expiry window). A consumed listing still never
        re-alerts - single consumption is a hard guarantee.
        """
        lid = decision.listing.listing_id
        if decision.verdict != "alert" or lid in self._consumed:
            return False
        active = self._alerts.get(lid)
        if active is not None and active.decision.listing.price == decision.listing.price:
            return False  # same price re-pushed: not news
        now = time.monotonic()
        self._alerts[lid] = Alert(
            decision=decision,
            created_monotonic=now,
            expires_at_monotonic=now + self._expiry_s,
        )
        return True

    def prune(self) -> bool:
        """Drop expired alerts; True when anything changed."""
        now = time.monotonic()
        expired = [lid for lid, a in self._alerts.items() if a.expires_at_monotonic <= now]
        for lid in expired:
            del self._alerts[lid]
        return bool(expired)

    def consume_best(self) -> Alert | None:
        """Pop the highest-profit live alert for the click path. One call per
        hotkey press; a consumed id is permanently barred from re-alerting."""
        self.prune()
        best = self._best()
        if best is None:
            return None
        self._mark_consumed(best.decision.listing.listing_id)
        if self._consume_mode == "all":
            for lid in list(self._alerts):
                self._mark_consumed(lid)
        return best

    def consume(self, listing_id: str) -> Alert | None:
        """Pop one specific alert (overlay click-to-travel). Consumes only
        that alert - runners-up stay targetable regardless of consume mode."""
        self.prune()
        alert = self._alerts.get(listing_id)
        if alert is None:
            return None
        self._mark_consumed(listing_id)
        return alert

    # --------------------------------------------------------------- queries

    @staticmethod
    def _rank(a: Alert) -> float:
        """Ranking key: divine profit per 100 difficulty (the overlay's
        P/100D column, and the same unit as the threshold).

        Ranking on efficiency rather than absolute surplus means the hotkey
        takes the map that pays best for what it costs to run: a clean 25-
        difficulty map at +20 div (80/100D) outranks a Feared+VOID monster
        at +50 div (4/100D), even though the latter's absolute surplus is
        larger.
        """
        d = a.decision
        return profit_per_100_difficulty(d.profit_div, d.difficulty) or 0.0

    def _best(self) -> Alert | None:
        return max(self._alerts.values(), key=self._rank, default=None)

    def view_of(self, listing_id: str) -> AlertView | None:
        alert = self._alerts.get(listing_id)
        return None if alert is None else alert.view()

    def views(self) -> tuple[AlertView, ...]:
        """Top alerts by profit surplus, best first, capped for display."""
        ranked = sorted(self._alerts.values(), key=self._rank, reverse=True)
        return tuple(a.view() for a in ranked[: self._max_display])

    def __len__(self) -> int:
        return len(self._alerts)

    def _mark_consumed(self, lid: str) -> None:
        self._alerts.pop(lid, None)
        self._consumed[lid] = time.monotonic()
        while len(self._consumed) > _CONSUMED_CAP:
            self._consumed.popitem(last=False)
