import pytest
from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.config import ManualPrice, ModWarningRule
from sniper.models import Listing, parse_frame
from sniper.modrules import ModRules
from sniper.prices import PriceBook

NO_RULES = ModRules([])
BLOCK_RULES = ModRules(
    [
        ModWarningRule(label="no flasks", severity="block", match="cannot use flasks"),
        ModWarningRule(label="no regen", severity="warn", match="cannot regenerate"),
    ]
)


def listing(**overrides) -> Listing:
    frame = parse_frame(make_listing_frame(**overrides))
    assert isinstance(frame, Listing), frame
    return frame


def decide(lst, config=None, rules=NO_RULES):
    config = config or make_config()
    return margin.evaluate(lst, PriceBook(config), config.thresholds, rules)


def test_alert_above_threshold():
    # ref 200 div, listed 100 div -> 100 div profit >= 20 div bar
    d = decide(listing(price={"amount": 100, "currency": "divine"}))
    assert d.verdict == "alert"
    assert d.profit_div == 100
    assert d.margin == (36000 - 18000) / 36000
    assert not d.currency_mismatch


def test_below_threshold():
    # ref 200 div, listed 190 div -> 10 div profit < 20 div bar
    d = decide(listing(price={"amount": 190, "currency": "divine"}))
    assert d.verdict == "below_threshold"
    assert d.profit_div == 10


def test_chaos_listing_normalized_and_mismatch_flagged():
    # 9000 chaos = 50 div -> 150 div profit, and currency differs from ref
    d = decide(listing(price={"amount": 9000, "currency": "chaos"}))
    assert d.verdict == "alert"
    assert d.listing_chaos == 9000
    assert d.profit_div == (36000 - 9000) / 180
    assert d.currency_mismatch  # chaos listing vs divine reference


def test_blocked_by_mod_even_with_huge_margin():
    d = decide(
        listing(
            price={"amount": 10, "currency": "divine"}, mods=["Players cannot use Flasks in area"]
        ),
        rules=BLOCK_RULES,
    )
    assert d.verdict == "blocked"
    assert d.profit_div is not None and d.profit_div > 100
    assert [h.label for h in d.mod_hits] == ["no flasks"]


def test_warn_mod_still_alerts():
    d = decide(
        listing(
            price={"amount": 100, "currency": "divine"},
            mods=["Players cannot Regenerate Life, Mana or Energy Shield"],
        ),
        rules=BLOCK_RULES,
    )
    assert d.verdict == "alert"
    assert [h.severity for h in d.mod_hits] == ["warn"]


def test_no_reference():
    d = decide(listing(reward="Foil Unknown Thing"))
    assert d.verdict == "no_reference"
    assert d.profit_div is None


def test_no_rate_for_unknown_currency():
    d = decide(listing(price={"amount": 3, "currency": "wisdom-scroll"}))
    assert d.verdict == "no_rate"


def test_per_map_threshold_override():
    config = make_config(
        per_map={"Foil Mageblood": 120}, prices={"Foil Mageblood": ManualPrice("divine", 200)}
    )
    d = decide(listing(price={"amount": 100, "currency": "divine"}), config=config)
    assert d.verdict == "below_threshold"  # 100 div profit < 120 div per-map bar
    assert d.threshold == 120


# ------------------------------------------------------- flat profit reduction
# A fixed divine toll off every listing: running any map costs time, so an
# easy map with a razor-thin margin is not actually worth doing.


def decide_with_reduction(amount: float, reduction: float):
    config = make_config(flat_profit_reduction=reduction, global_profit_div=0.0)
    listing = parse_frame(make_listing_frame(price={"amount": amount, "currency": "divine"}))
    return margin.evaluate(listing, PriceBook(config), config.thresholds, ModRules([]))


def test_flat_reduction_comes_off_the_profit():
    """Reference is 200 div; a 150 div listing is +50 before the toll."""
    assert decide_with_reduction(150, 0).profit_div == 50
    assert decide_with_reduction(150, 1).profit_div == 49
    assert decide_with_reduction(150, 5).profit_div == 45


def test_flat_reduction_can_push_a_thin_margin_below_the_cutoff():
    """The whole point: a barely-profitable map stops alerting."""
    assert decide_with_reduction(199.5, 0).verdict == "alert"  # +0.5 div
    assert decide_with_reduction(199.5, 1).verdict == "below_threshold"  # -0.5


def test_flat_reduction_flows_into_p100d():
    """Displayed P/100D is derived from the reduced profit, so the number on
    screen and the number the cutoff uses can never disagree."""
    from sniper.margin import profit_per_100_difficulty

    d = decide_with_reduction(150, 1)
    assert profit_per_100_difficulty(d.profit_div, d.difficulty) == pytest.approx(
        49 / d.difficulty * 100
    )


def test_flat_reduction_leaves_margin_percent_alone():
    """Margin is a pure price ratio; the time toll is not part of it."""
    assert decide_with_reduction(150, 1).margin == pytest.approx(0.25)
