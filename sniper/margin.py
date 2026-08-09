"""Pure decision logic: listing + PriceBook + rules -> Decision. Hot path.

Alert condition: profit_div >= required_profit_div, where
required_profit_div = threshold + difficulty_score * div_per_point.
Harder maps (per config mod_scoring rules) must be cheaper to alert.
"""

from __future__ import annotations

from sniper.config import ModScoringConfig, Thresholds
from sniper.models import Decision, Listing
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook


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

    # no scoring engine -> neutral (score 0, no cutoff shift)
    scoring = scoring or ModScoring(ModScoringConfig(base_default=0.0, div_per_point=0.0))
    difficulty = scoring.evaluate(listing.mods)
    required = threshold + difficulty.score * scoring.div_per_point

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
            required_profit_div=required,
        )

    profit_div = (reference.chaos_value - listing_chaos) / divine_rate
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
        required_profit_div=required,
    )
