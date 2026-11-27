"""Pure decision logic: listing + PriceBook + rules -> Decision. Hot path.

Alert condition: net_div >= required_net_div AND net_div >= thresholds.min_net,
where required_net_div = threshold * difficulty_score / 100.
The base threshold is calibrated for a 100-difficulty map: a 200-difficulty
map needs twice the profit, a 50-difficulty map only half. min_net is a flat
floor on top: a trivial map's yield can clear the bar on a profit too small
to travel for.

Failure risks make net an EXPECTED value: a failed map pays nothing and the
asking price is gone, so
    net = (1 - fail_chance) x market price - asking price - running cost
"""

from __future__ import annotations

from sniper.config import ModScoringConfig, Thresholds
from sniper.models import Decision, Listing
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook

# Listing currencies that are never a currency mismatch, even against a
# divine reference. The scam guard exists for a price whose unit could be
# misread; chaos is the unit every price normalizes to (rate exactly 1) and
# alchemy is a cheap, liquid orb with a solid poe.ninja rate. A Valdo map
# priced in either is a seller's under-pricing, not bait: the chaos-converted
# cost on screen is already what you pay, so it gets no banner and no red
# price, and can be a jackpot or super jackpot like any divine listing.
SAFE_LISTING_CURRENCIES = frozenset({"chaos", "alch"})


def net_yield(net_div: float | None, difficulty: float) -> float | None:
    """Divine profit normalised to a 100-difficulty map.

    The same unit as the configured threshold (which IS the profit a
    100-difficulty map must make), so this value is directly comparable to
    it: >= threshold is exactly the alert condition, restated. Used as the
    alert ranking key and shown as the overlay's Yield column.

    None when profit is unknown (no reference/rate) or difficulty is zero.
    """
    if net_div is None or not difficulty:
        return None
    return net_div / difficulty * 100


def is_jackpot(
    net_div: float | None,
    difficulty: float,
    min_net: float | None,
    min_yield: float | None,
) -> bool:
    """A listing that is BOTH big money and cheap to run.

    Neither number alone is rare: a 1000-difficulty Nimis can net +300d at
    a mediocre yield, and a trivial map can show a huge yield on +5d. Both
    at once is what is worth dropping everything for (e.g. a half-price
    Nimis: +80d net, +144 yield, 55 difficulty). Disabled when either
    threshold is None.
    """
    if min_net is None or min_yield is None:
        return False
    ratio = net_yield(net_div, difficulty)
    return ratio is not None and net_div >= min_net and ratio >= min_yield


def _generic_decision(listing: Listing, thresholds: Thresholds) -> Decision:
    """A listing from a non-Valdo live search alerts on arrival.

    None of the Valdo machinery applies: there is no reward to look up a
    reference for, and the mod scoring reads MAP modifiers, so a ring's
    explicit mods would score as noise and invent a difficulty out of
    nothing. The user's live search already encodes what they want and what
    they will pay, so the only useful thing the app can add is speed - get
    it on screen and let them travel.

    difficulty stays 0, which makes net_yield return None,
    so these never pollute the Yield column or the Valdo ranking.
    """
    return Decision(
        listing=listing,
        key=listing.type_line or listing.item_name,
        verdict="alert",
        threshold=thresholds.min_yield,
        # mods pass through unscored so the overlay can still show them
        mods_annotated=tuple((m, "", "none") for m in listing.mods),
    )


def evaluate(
    listing: Listing,
    book: PriceBook,
    thresholds: Thresholds,
    rules: ModRules,
    scoring: ModScoring | None = None,
) -> Decision:
    if listing.generic:
        return _generic_decision(listing, thresholds)
    key = listing.price_key
    threshold = thresholds.per_map.get(key, thresholds.min_yield)
    mod_hits = rules.evaluate(listing.mods)
    blocked = any(h.severity == "block" for h in mod_hits)

    # no scoring engine -> neutral (difficulty 100 = exactly the threshold)
    scoring = scoring or ModScoring(ModScoringConfig(base_default=100.0))
    difficulty = scoring.evaluate(listing.mods)
    annotated = scoring.annotate(listing.mods)
    required = threshold * difficulty.score / 100.0

    reference = book.reference_for(key)
    if reference is None:
        return Decision(
            listing=listing,
            key=key,
            verdict="no_reference",
            threshold=threshold,
            mod_hits=mod_hits,
            difficulty=difficulty.score,
            difficulty_mods=difficulty.matched,
            special_warnings=difficulty.warnings,
            pairings=difficulty.pairings,
            mods_annotated=annotated,
            required_net_div=required,
            risks=difficulty.risks,
            fail_chance=difficulty.fail_chance,
        )

    listing_chaos = book.to_chaos(listing.price.amount, listing.price.currency)
    divine_rate = book.rate_chaos("divine")
    if listing_chaos is None or not divine_rate:
        return Decision(
            listing=listing,
            key=key,
            verdict="no_rate",
            threshold=threshold,
            reference=reference,
            mod_hits=mod_hits,
            difficulty=difficulty.score,
            difficulty_mods=difficulty.matched,
            special_warnings=difficulty.warnings,
            pairings=difficulty.pairings,
            mods_annotated=annotated,
            required_net_div=required,
            risks=difficulty.risks,
            fail_chance=difficulty.fail_chance,
        )

    # Flat reduction: every map costs time to run regardless of how easy it
    # is, so a fixed toll comes off the profit before anything else looks at
    # it. Applied here rather than at the display layer so the shown profit,
    # yield, the alert cutoff and the hotkey's ranking all agree - a map
    # displaying less profit than it alerts on would be a bug.
    # A map that fails pays nothing back while the asking price is still
    # spent, so only the surviving share of the market price counts. The
    # same expected value then drives yield, the cutoff, jackpots and the
    # hotkey ranking; a 100% risk can never come out positive.
    fail_chance = difficulty.fail_chance
    risk_cost_div = fail_chance * reference.chaos_value / divine_rate
    net_div = ((1.0 - fail_chance) * reference.chaos_value - listing_chaos) / divine_rate
    net_div -= thresholds.running_cost
    margin = (reference.chaos_value - listing_chaos) / reference.chaos_value
    mismatch = (
        listing.price.currency != reference.display_currency
        and listing.price.currency not in SAFE_LISTING_CURRENCIES
    )

    if blocked:
        verdict = "blocked"
    elif net_div >= required and net_div >= thresholds.min_net:
        verdict = "alert"
    else:
        verdict = "below_threshold"

    return Decision(
        listing=listing,
        key=key,
        verdict=verdict,
        threshold=threshold,
        reference=reference,
        listing_chaos=listing_chaos,
        net_div=net_div,
        margin=margin,
        currency_mismatch=mismatch,
        mod_hits=mod_hits,
        difficulty=difficulty.score,
        difficulty_mods=difficulty.matched,
        special_warnings=difficulty.warnings,
        pairings=difficulty.pairings,
        mods_annotated=annotated,
        required_net_div=required,
        risks=difficulty.risks,
        fail_chance=fail_chance,
        risk_cost_div=risk_cost_div,
    )
