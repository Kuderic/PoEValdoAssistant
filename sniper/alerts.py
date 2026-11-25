"""AlertStore: the set of currently-actionable alerts.

ToS-critical semantics (DESIGN.md):
- best-value-wins: the hotkey consumes the non-expired alert with the
  highest profit per 100 difficulty (see _rank), not the biggest absolute
  profit - except that a jackpot (big net AND big yield) outranks
  everything else, generic hits included, and a super jackpot (higher bars
  still) outranks a jackpot: it is the best deal on screen by both
  measures. Below the jackpots a generic (non-Valdo) hit outranks the
  remaining Valdo alerts, newest first;
- expiry: a stale listing can never be traveled to by reflex;
- single consumption: a consumed listing id can never alert or be clicked
  again, even if the tab re-pushes it;
- consume mode "all" clears runners-up too (a second press does nothing),
  "top" leaves them targetable;
- a travel that FAILED (item sold, row gone) gives the runners-up back, so
  the next-best alert is immediately targetable - see settle_travel.

Touched only from the asyncio thread -> needs no locks.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from sniper.margin import is_jackpot, net_yield
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
    net_div: float
    required_net_div: float
    difficulty: float
    margin: float
    seller: str
    mismatch: bool
    warn_labels: tuple[str, ...]
    special_warnings: tuple[tuple[str, str], ...]  # (label, color)
    # deadly mod pairings: (label, note, multiplier)
    pairings: tuple[tuple[str, str, float], ...]
    # (mod text, scoring note e.g. "×1.8"/""); tooltip + traveled panel
    mods: tuple[tuple[str, str], ...]
    reference_amount: float
    reference_currency: str
    reference_source: str
    # ms the listing was already live before we saw it; None when unknown
    # (DOM-captured, or an old userscript that does not send indexed_at)
    index_lag_ms: float | None
    created_monotonic: float
    expires_at_monotonic: float
    # from a non-Valdo live search: no net/yield/difficulty to show
    generic: bool = False
    # big net AND big yield (margin.is_jackpot): its own colour, top pick
    jackpot: bool = False
    # the tier above: a jackpot that also clears the super bars. Always
    # implies jackpot, so code that only knows jackpots still treats it as one
    super_jackpot: bool = False
    # failure risks: (label, note, chance); combined chance; its divine cost
    risks: tuple[tuple[str, str, float], ...] = ()
    fail_chance: float = 0.0
    risk_cost_div: float = 0.0
    # seller's tab name and the item's 0-based cell; None when unknown
    stash: tuple[str, int, int] | None = None


@dataclass
class Alert:
    decision: Decision
    created_monotonic: float
    expires_at_monotonic: float
    jackpot: bool = False
    super_jackpot: bool = False

    def view(self) -> AlertView:
        d = self.decision
        return AlertView(
            listing_id=d.listing.listing_id,
            key=d.key,
            item_name=d.listing.item_name,
            amount=d.listing.price.amount,
            currency=d.listing.price.currency,
            net_div=d.net_div or 0.0,
            required_net_div=d.required_net_div or 0.0,
            difficulty=d.difficulty,
            margin=d.margin or 0.0,
            seller=d.listing.seller,
            mismatch=d.currency_mismatch,
            warn_labels=tuple(h.label for h in d.mod_hits if h.severity == "warn"),
            special_warnings=d.special_warnings,
            pairings=d.pairings,
            mods=d.mods_annotated,
            reference_amount=d.reference.display_amount if d.reference else 0.0,
            reference_currency=d.reference.display_currency if d.reference else "",
            reference_source=d.reference.source if d.reference else "",
            index_lag_ms=d.listing.index_lag_ms,
            created_monotonic=self.created_monotonic,
            expires_at_monotonic=self.expires_at_monotonic,
            generic=d.listing.generic,
            jackpot=self.jackpot,
            super_jackpot=self.super_jackpot,
            risks=d.risks,
            fail_chance=d.fail_chance,
            risk_cost_div=d.risk_cost_div or 0.0,
            stash=None
            if d.listing.stash is None
            else (d.listing.stash.name, d.listing.stash.x, d.listing.stash.y),
        )


class AlertStore:
    def __init__(
        self,
        expiry_s: float,
        consume_mode: str = "all",
        max_display: int = 3,
        # both None (the default) disables the jackpot tier entirely
        jackpot_min_net: float | None = None,
        jackpot_min_yield: float | None = None,
        # both None disables the super tier
        super_jackpot_min_net: float | None = None,
        super_jackpot_min_yield: float | None = None,
    ):
        self._expiry_s = expiry_s
        self._jackpot_min_net = jackpot_min_net
        self._jackpot_min_yield = jackpot_min_yield
        self._super_min_net = super_jackpot_min_net
        self._super_min_yield = super_jackpot_min_yield
        self._consume_mode = consume_mode
        self._max_display = max_display
        self._alerts: dict[str, Alert] = {}
        self._consumed: OrderedDict[str, float] = OrderedDict()
        # The last hotkey travel awaiting its click_result:
        # (listing id, the runners-up that press cleared as collateral).
        # Held so a FAILED travel can hand them back - see settle_travel.
        self._in_flight: tuple[str, dict[str, Alert]] | None = None

    def set_jackpot_thresholds(
        self,
        min_net: float,
        min_yield: float,
        super_min_net: float | None = None,
        super_min_yield: float | None = None,
    ) -> None:
        """Settings panel. Applies to alerts from now on; a live alert keeps
        the flag it was raised with, so it never changes colour mid-alert.
        The super bars are left as they are when not given."""
        self._jackpot_min_net = min_net
        self._jackpot_min_yield = min_yield
        if super_min_net is not None and super_min_yield is not None:
            self._super_min_net = super_min_net
            self._super_min_yield = super_min_yield

    def is_jackpot(self, decision: Decision) -> bool:
        """Generic hits have no net/yield to judge. A currency mismatch is
        the scam guard's case: an implausibly good price in an unexpected
        currency is exactly the listing that must NOT be made louder and
        pushed to the top of the hotkey - it still alerts normally, with its
        mismatch banner."""
        if decision.listing.generic or decision.currency_mismatch:
            return False
        return is_jackpot(
            decision.net_div,
            decision.difficulty,
            self._jackpot_min_net,
            self._jackpot_min_yield,
        )

    def is_super_jackpot(self, decision: Decision) -> bool:
        """A jackpot that also clears the super bars. Requiring the jackpot
        too keeps every jackpot exclusion (generic, currency mismatch) and
        means a super bar set BELOW the jackpot bar cannot create a super
        jackpot that is not a jackpot."""
        return self.is_jackpot(decision) and is_jackpot(
            decision.net_div,
            decision.difficulty,
            self._super_min_net,
            self._super_min_yield,
        )

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
            jackpot=self.is_jackpot(decision),
            super_jackpot=self.is_super_jackpot(decision),
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
        hotkey press; a consumed id is permanently barred from re-alerting.

        In "all" mode the runners-up cleared alongside it are remembered
        (until a failure report or the next press) so a failed teleport can
        give them back - see settle_travel. The store's visible state is
        unchanged from before: while the travel is in flight they are gone,
        and a second press still does nothing.
        """
        self.prune()
        best = self._best()
        if best is None:
            return None
        lid = best.decision.listing.listing_id
        self._mark_consumed(lid)
        collateral: dict[str, Alert] = {}
        if self._consume_mode == "all":
            collateral = dict(self._alerts)
            for other in list(self._alerts):
                self._mark_consumed(other)
        self._in_flight = (lid, collateral)
        return best

    def settle_travel(self, listing_id: str, ok: bool) -> bool:
        """Resolve the travel a hotkey press started, from a click_result.
        True when the store changed and the overlay needs a push.

        A failed travel - the item sold out from under us, the row is gone -
        must not leave the user staring at an empty overlay holding the one
        alert they cannot have. The runners-up that press cleared as
        collateral come back with their ORIGINAL expiry (the clock protects
        against travelling to a stale row; a failed click does not make the
        others any fresher), so the next-best becomes the top pick and one
        further press can take it.

        `ok=True` settles NOTHING, deliberately. The userscript replies
        ok the moment it clicks the button, then keeps watching that row for
        ~3s and follows up with `item_no_longer_available` if the sale lands
        first - which is the exact case this exists for. Dropping the
        collateral on the optimistic reply would throw the runners-up away
        milliseconds before learning we needed them. They are held until a
        failure arrives or the next press replaces them; nothing shows them
        meanwhile, and they expire on their own clock regardless.

        The listing actually travelled to is never restored: single
        consumption stays a hard guarantee, and it is precisely the listing
        we just learned is unavailable.
        """
        if ok or self._in_flight is None or self._in_flight[0] != listing_id:
            return False
        _lid, collateral = self._in_flight
        self._in_flight = None
        now = time.monotonic()
        restored = False
        for lid, alert in collateral.items():
            if alert.expires_at_monotonic <= now:
                continue  # expired while the travel was in flight
            self._consumed.pop(lid, None)
            self._alerts[lid] = alert
            restored = True
        return restored

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
    def _rank(a: Alert) -> tuple[bool, bool, bool, float]:
        """Sort key, best first: (super jackpot, jackpot, generic, order).

        A jackpot holds the top slot over everything, generic hits included:
        it is the one that is ringing, so it is the one the hotkey takes. A
        super jackpot outranks a jackpot. (A generic hit is never a jackpot
        itself - see is_jackpot - so the two flags never meet.)

        Below the jackpots a generic (non-Valdo) hit comes first: it comes
        from a live search the user built by hand for one item, so it is the
        rare one worth interrupting for. Generic hits have no efficiency to
        compare, so among themselves the newest wins: the thing that just
        made a sound is the thing the hotkey takes.

        The remaining Valdo alerts rank on divine profit per 100 difficulty
        (the overlay's Yield column, same unit as the threshold) - efficiency
        rather than absolute surplus, so the hotkey takes the map that pays
        best for what it costs to run: a clean 25-difficulty map at +20 div
        (80/100D) outranks a Feared+VOID monster at +50 div (4/100D).
        """
        d = a.decision
        if d.listing.generic:
            return (False, False, True, a.created_monotonic)
        ratio = net_yield(d.net_div, d.difficulty) or 0.0
        return (a.super_jackpot, a.jackpot, False, ratio)

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


def alarm_kind(alerts: tuple[AlertView, ...], silenced: set[str]) -> str | None:
    """Which alarm should loop: "super" while any unsilenced super jackpot
    is live, else "jackpot" while any unsilenced jackpot is, else None.

    `alerts` is ranked best first. A jackpot outranks a generic hit, so a
    generic hit can only hold the top slot when no jackpot is live; the
    guard below keeps the alarm off in that case all the same."""
    if alerts and alerts[0].generic:
        return None
    ringing = jackpots_to_ring(alerts, silenced)
    if any(a.super_jackpot for a in alerts if a.listing_id in ringing):
        return "super"
    return "jackpot" if ringing else None


def jackpots_to_ring(alerts: tuple[AlertView, ...], silenced: set[str]) -> set[str]:
    """Listing ids of live jackpots the alarm should ring for: every jackpot
    still on screen that the user has not silenced. Empty -> alarm off.

    Travel consumes the alert and expiry drops it, so either one ends the
    alarm by itself; silencing is per listing, so a NEW jackpot arriving
    after you silenced one still rings."""
    return {a.listing_id for a in alerts if a.jackpot and a.listing_id not in silenced}
