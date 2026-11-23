"""Hover help and the reward price shown beside each reward.

The live lines in the help popups work a listing's numbers through ("160
worth - 52 price - 1 running = +107d"). A worked example that disagrees with
the number above it would mislead rather than explain, so each builder must
reproduce the figure on screen or say nothing.
"""

from types import SimpleNamespace

from sniper.bus import RewardPrice
from sniper.overlay import (
    HELP,
    _difficulty_breakdown,
    _money,
    _net_breakdown,
    _price_tag,
    _yield_breakdown,
)


def view(**kw):
    fields = dict(
        generic=False,
        net_div=107.0,
        amount=52.0,
        currency="divine",
        reference_amount=160.0,
        reference_currency="divine",
        fail_chance=0.0,
        difficulty=100.0,
        mods=(),
        pairings=(),
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


# ------------------------------------------------------------------- net


def test_net_breakdown_adds_up_to_the_net_shown():
    assert _net_breakdown(view(), 1.0) == "160 worth − 52 price − 1 running\n= +107.0d"


def test_net_breakdown_shows_the_failure_risk():
    # 160 * 0.7 - 52 - 1 = 59
    text = _net_breakdown(view(fail_chance=0.3, net_div=59.0), 1.0)
    assert text == "160 worth × 70% succeed − 52 price − 1 running\n= +59.0d"


def test_net_breakdown_says_nothing_when_the_sum_disagrees():
    """e.g. the running cost was changed after this alert was scored"""
    assert _net_breakdown(view(net_div=90.0), 1.0) == ""


def test_net_breakdown_says_nothing_across_currencies():
    """the chaos->divine conversion is not on screen, so the sum cannot be"""
    assert _net_breakdown(view(currency="chaos", amount=80.0), 1.0) == ""
    assert _net_breakdown(view(generic=True), 1.0) == ""


# ----------------------------------------------------------------- yield


def test_yield_breakdown_states_the_formula_and_the_verdict():
    text = _yield_breakdown(48.0, 105.6, 2.5)
    assert text.startswith("+48.0 net ÷ 105.6 difficulty × 100 = +45.5")
    assert "so this alerts" in text
    assert "below the bar" in _yield_breakdown(1.0, 100.0, 2.5)


def test_yield_breakdown_empty_without_a_net():
    assert _yield_breakdown(None, 100.0, 2.5) == ""


# ------------------------------------------------------------ difficulty


def test_difficulty_breakdown_rebuilds_the_score_from_the_mod_notes():
    mods = (
        ("Area contains The Feared", "base 80", "red"),
        ("Players have 40% less Recovery", "×1.3", "yellow"),
        ("Monsters gain a Power Charge on Hit", "", "none"),
    )
    pairings = (("Power charges + cull", "", 1.4),)
    v = view(mods=mods, pairings=pairings, difficulty=80 * 1.3 * 1.4)
    assert _difficulty_breakdown(v, 25.0) == "base 80 × 1.3 × 1.4 = 146"


def test_difficulty_breakdown_starts_from_the_clean_map_default():
    v = view(mods=(("Monsters fire 2 additional Projectiles", "×1.2", "yellow"),), difficulty=30)
    assert _difficulty_breakdown(v, 25.0) == "clean map 25 × 1.2 = 30"


def test_difficulty_breakdown_handles_a_combined_note():
    v = view(mods=(("Two-line combo", "base 40 · ×1.5", "yellow"),), difficulty=60)
    assert _difficulty_breakdown(v, 25.0) == "base 40 × 1.5 = 60"


def test_difficulty_breakdown_says_nothing_when_it_does_not_reproduce():
    v = view(mods=(("x", "×1.2", "yellow"), ("y", "×1.2", "yellow")), difficulty=30)
    assert _difficulty_breakdown(v, 25.0) == ""  # 25*1.44 = 36, not 30


# -------------------------------------------------------- price beside it


def price(**kw):
    fields = dict(reward="Foil Nimis", amount=160.0, currency="divine", source="trade", age_s=0)
    fields.update(kw)
    return RewardPrice(**fields)


def test_price_tag_shows_price_and_when_it_was_fetched():
    assert _price_tag(price(), 240.0) == "160d (4m ago)"
    assert _price_tag(price(currency="chaos", amount=40.0), 5.0) == "40c (5s ago)"


def test_price_tag_marks_an_unpriced_or_pinned_reward():
    assert _price_tag(price(amount=None, currency=""), None) == "(pricing…)"
    assert _price_tag(price(override_div=150.0, amount=150.0), None) == "150d (yours)"


def test_price_tag_without_a_fetch_time_shows_just_the_price():
    assert _price_tag(price(source="manual"), None) == "160d"


def test_money_is_compact_for_the_two_common_currencies():
    assert (_money(80, "divine"), _money(40, "chaos"), _money(3, "exalted")) == (
        "80d",
        "40c",
        "3 exalted",
    )


# ------------------------------------------------------------------ help


def test_every_help_entry_has_a_title_and_an_explanation():
    for key, (title, body) in HELP.items():
        assert title and len(body) > 20, key


def test_the_latency_help_says_what_the_delay_is_between():
    body = HELP["latency"][1]
    assert "trade site indexed" in body and "Chrome tab received" in body
