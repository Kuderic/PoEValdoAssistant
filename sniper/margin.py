"""Pure decision logic: listing + PriceBook + rules -> Decision. Hot path.

Alert condition: profit_div >= required_profit_div, where
required_profit_div = threshold * difficulty_score / 100.
The base threshold is calibrated for a 100-difficulty map: a 200-difficulty
map needs twice the profit, a 50-difficulty map only half.
"""

from __future__ import annotations

from sniper.config import ModScoringConfig, Thresholds
from sniper.models import Decision, Listing
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook


def profit_per_100_difficulty(profit_div: float | None, difficulty: float) -> float | None:
    """Divine profit normalised to a 100-difficulty map.

    The same unit as the configured threshold (which IS the profit a
    100-difficulty map must make), so this value is directly comparable to
    it: >= threshold is exactly the alert condition, restated. Used as the
    alert ranking key and shown as the overlay's P/100D column.

    None when profit is unknown (no reference/rate) or difficulty is zero.
    """
    if profit_div is None or not difficulty:
        return None
    return profit_div / difficulty * 100


def evaluate(
    listing: Listing,
    book: PriceBook,
    thresholds: Thresholds,
    rules: ModRules,
    scoring: ModScoring | None = None,
) -> Decision:
    key = listing.price_key
    threshold = thresholds.per_map.get(key, thresholds.global_profit_div)
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
            mods_annotated=annotated,
            required_profit_div=required,
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
            mods_annotated=annotated,
            required_profit_div=required,
        )

    # Flat reduction: every map costs time to run regardless of how easy it
    # is, so a fixed toll comes off the profit before anything else looks at
    # it. Applied here rather than at the display layer so the shown profit,
    # P/100D, the alert cutoff and the hotkey's ranking all agree - a map
    # displaying less profit than it alerts on would be a bug.
    profit_div = (reference.chaos_value - listing_chaos) / divine_rate
    profit_div -= thresholds.flat_profit_reduction
    margin = (reference.chaos_value - listing_chaos) / reference.chaos_value
    mismatch = listing.price.currency != reference.display_currency

    if blocked:
        verdict = "blocked"
    elif profit_div >= required:
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
        profit_div=profit_div,
        margin=margin,
        currency_mismatch=mismatch,
        mod_hits=mod_hits,
        difficulty=difficulty.score,
        difficulty_mods=difficulty.matched,
        special_warnings=difficulty.warnings,
        mods_annotated=annotated,
        required_profit_div=required,
    )
