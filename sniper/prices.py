"""PriceBook: reference prices and currency normalization.

Layering per DESIGN.md: manual config entries always override; poe.ninja
fills the rest (marked stale when the last refresh is too old). Everything
is chaos-normalized at refresh time so the hot path is dict lookups only.
"""

from __future__ import annotations

import statistics
import time

from sniper.config import Config
from sniper.models import Reference
from sniper.ninja import NinjaClient

# Trade-site short currency ids (userscript scrapes these from the currency
# image alt) -> poe.ninja currencyTypeName. chaos is the unit.
TRADE_TO_NINJA = {
    "chaos": None,
    "divine": "Divine Orb",
    "exalted": "Exalted Orb",
    "mirror": "Mirror of Kalandra",
    "alch": "Orb of Alchemy",
    "annul": "Orb of Annulment",
    "vaal": "Vaal Orb",
    "regal": "Regal Orb",
    "chisel": "Cartographer's Chisel",
    "gcp": "Gemcutter's Prism",
    "fusing": "Orb of Fusing",
    "jewellers": "Jeweller's Orb",
    "chrome": "Chromatic Orb",
    "blessed": "Blessed Orb",
    "regret": "Orb of Regret",
    "scour": "Orb of Scouring",
    "alt": "Orb of Alteration",
    "chance": "Orb of Chance",
}


class PriceBook:
    def __init__(self, config: Config, fresh_ttl_s: float | None = None):
        self._config = config
        if fresh_ttl_s is None:
            fresh_ttl_s = max(config.ninja.refresh_minutes * 60 * 2, 300)
        self._fresh_ttl_s = fresh_ttl_s
        self._league: str | None = None if config.league in ("", "auto") else config.league
        self._ninja_rewards: dict[str, float] = {}  # reward -> median chaosValue
        self._live_rates: dict[str, float] = {}  # trade short id -> chaos
        self._last_refresh_monotonic: float | None = None
        # trade-API unid-unique averages: reward -> (chaos, monotonic set-time)
        self._trade_rewards: dict[str, tuple[float, float]] = {}
        self._trade_ttl_s = max(config.trade_pricing.refresh_minutes * 60 * 2, 300)

    # ------------------------------------------------------------------ state

    @property
    def league(self) -> str | None:
        return self._league

    @property
    def age_s(self) -> float | None:
        if self._last_refresh_monotonic is None:
            return None
        return time.monotonic() - self._last_refresh_monotonic

    @property
    def status(self) -> str:
        """For the overlay pill: live | stale | manual. Reflects whether ninja
        data has actually been fetched (when ninja is disabled, no refresh
        ever runs, so this stays "manual")."""
        if self._last_refresh_monotonic is None:
            return "manual"
        return "live" if self.age_s <= self._fresh_ttl_s else "stale"

    # ------------------------------------------------------------- refreshing

    async def refresh(self, ninja: NinjaClient) -> dict:
        """Fetch rates + Valdo overview. Off the hot path; raises NinjaBackoff
        (or httpx errors) for the refresh task to log and retry later."""
        if self._league is None:
            self._league = await ninja.current_league()

        currency = await ninja.currency_overview(self._league)
        by_name = {
            ln.get("currencyTypeName"): float(ln.get("chaosEquivalent") or 0)
            for ln in currency.get("lines", [])
        }
        live_rates = {"chaos": 1.0}
        for short_id, ninja_name in TRADE_TO_NINJA.items():
            if ninja_name is None:
                continue
            value = by_name.get(ninja_name)
            if value:
                live_rates[short_id] = value

        valdo = await ninja.valdo_overview(self._league)
        per_reward: dict[str, list[float]] = {}
        for ln in valdo.get("lines", []):
            reward = ln.get("variant")
            chaos = ln.get("chaosValue")
            if reward and isinstance(chaos, int | float) and chaos > 0:
                per_reward.setdefault(str(reward), []).append(float(chaos))
        # Median across the map-name lines sharing a reward: robust to a
        # single price-fixed listing.
        self._ninja_rewards = {r: statistics.median(v) for r, v in per_reward.items()}
        self._live_rates = live_rates
        self._last_refresh_monotonic = time.monotonic()
        return {
            "league": self._league,
            "rewards": len(self._ninja_rewards),
            "rates": len(live_rates),
        }

    # ---------------------------------------------------------------- lookups

    def rate_chaos(self, currency: str) -> float | None:
        """Chaos value of one unit of a trade-site currency id, or None."""
        currency = currency.lower()
        if currency == "chaos":
            return 1.0
        if self.status == "live" and currency in self._live_rates:
            return self._live_rates[currency]
        if currency in self._config.currency_rates:
            return float(self._config.currency_rates[currency])
        if currency in self._live_rates:  # stale ninja beats nothing
            return self._live_rates[currency]
        return None

    def to_chaos(self, amount: float, currency: str) -> float | None:
        rate = self.rate_chaos(currency)
        return None if rate is None else amount * rate

    def set_trade_price(self, reward: str, chaos_value: float) -> None:
        """Store a trade-API unid-unique average (App's trade refresh loop)."""
        self._trade_rewards[reward] = (chaos_value, time.monotonic())

    def _trade_reference(self, key: str) -> Reference | None:
        entry = self._trade_rewards.get(key)
        if entry is None:
            return None
        chaos, set_at = entry
        if time.monotonic() - set_at > self._trade_ttl_s:
            return None  # too old - fall through to ninja
        divine_rate = self.rate_chaos("divine")
        if divine_rate:
            display_amount, display_currency = round(chaos / divine_rate, 1), "divine"
        else:
            display_amount, display_currency = round(chaos, 1), "chaos"
        return Reference(
            key=key,
            display_amount=display_amount,
            display_currency=display_currency,
            chaos_value=chaos,
            source="trade",
        )

    def reference_for(self, key: str) -> Reference | None:
        manual = self._config.prices.get(key)
        if manual is not None:
            chaos = self.to_chaos(manual.amount, manual.currency)
            if chaos is None:
                return None
            return Reference(
                key=key,
                display_amount=manual.amount,
                display_currency=manual.currency.lower(),
                chaos_value=chaos,
                source="manual",
            )
        # trade-API unid average is the primary market source (poe.ninja's
        # per-map medians proved inaccurate); ninja remains the fallback
        # until the first trade fetch lands or when it goes stale.
        trade = self._trade_reference(key)
        if trade is not None:
            return trade
        chaos = self._ninja_rewards.get(key)
        if chaos is None:
            return None
        divine_rate = self.rate_chaos("divine")
        if divine_rate:
            display_amount, display_currency = round(chaos / divine_rate, 1), "divine"
        else:
            display_amount, display_currency = round(chaos, 1), "chaos"
        return Reference(
            key=key,
            display_amount=display_amount,
            display_currency=display_currency,
            chaos_value=chaos,
            source="live" if self.status == "live" else "stale",
        )
