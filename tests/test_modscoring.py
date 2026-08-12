"""Difficulty scoring engine + its effect on the alert cutoff."""

import pytest
from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.config import ModScoringConfig, ModScoringRule
from sniper.models import parse_frame
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook

RULES = (
    ModScoringRule(label="The Feared", match="area contains the feared", min_base=100),
    ModScoringRule(label="100% Delirious", match="100% delirious", multiplier=1.8),
    ModScoringRule(label="Porcupines", match="porcupine", min_base=20),
    ModScoringRule(label="The Twisted", match="area contains the twisted", min_base=50),
    ModScoringRule(
        label="3-4 Tormented Spirits",
        match="possessed by 3 to 4 tormented spirits",
        multiplier=2.2,
    ),
    ModScoringRule(label="VOID", match="sent to the void", multiplier=2.0, warning="red"),
    ModScoringRule(label="BISMUTH", match="bismuth", warning="yellow"),
    ModScoringRule(label="BLIGHT", match="blight", warning="yellow"),
    ModScoringRule(label="Einhar", match="einhar", min_base=80),
    ModScoringRule(label="Increasingly lethal", match="increasingly lethal", multiplier=1.7),
    ModScoringRule(label="Fatal after time", match="becomes fatal", multiplier=2.0),
    ModScoringRule(label="Reflect", match="reflect", multiplier=1.1),
    ModScoringRule(
        label="Invitation boss",
        match="area contains the (hidden|formed|forgotten)",
        regex=True,
        min_base=60,
    ),
)

SCORING = ModScoring(ModScoringConfig(base_default=25, rules=RULES))


def score(mods):
    return SCORING.evaluate(mods)


def test_clean_map_gets_base_default():
    r = score(["Monsters fire 2 additional Projectiles"])
    assert r.score == 25
    assert r.matched == ()
    assert r.warnings == ()


def test_feared_min_base():
    assert score(["Area contains The Feared"]).score == 100


def test_feared_plus_delirious_multiplies():
    r = score(["Area contains The Feared", "Players in Area are 100% Delirious"])
    assert r.score == 180  # 100 x 1.8
    assert set(r.matched) == {"The Feared", "100% Delirious"}


def test_min_bases_take_max_not_sum():
    r = score(["Area contains The Feared", "Area contains The Twisted"])
    assert r.score == 100  # max(100, 50), not 150


def test_multipliers_stack():
    r = score(
        [
            "Area contains The Feared",
            "Players in Area are 100% Delirious",
            "Players who Die in area are sent to the Void",
        ]
    )
    assert r.score == 360  # 100 x 1.8 x 2.0
    assert r.warnings == (("VOID", "red"),)


def test_multiplier_applies_to_base_default():
    # no min-base mod matched: 25 x 1.8
    assert score(["Players in Area are 100% Delirious"]).score == 45


def test_ghosts_multiplier():
    r = score(
        [
            "Rare and Unique Monsters in Area are Possessed by 3 to 4 "
            "Tormented Spirits and their Minions are Touched"
        ]
    )
    assert r.score == 55  # 25 x 2.2


def test_bismuth_warning_only():
    r = score(["Area contains a large Bismuth vein"])
    assert r.score == 25  # no score effect
    assert r.warnings == (("BISMUTH", "yellow"),)


def test_invitation_boss_regex():
    assert score(["Area contains The Forgotten"]).score == 60
    assert score(["Area contains The Hidden"]).score == 60


def test_reflect_and_lethal():
    r = score(["Monsters reflect 18% of Elemental Damage", "Area becomes increasingly lethal"])
    assert r.score == round(25 * 1.1 * 1.7, 2)


# --- difficulty flowing into the margin verdict -----------------------------


def decide_with_scoring(amount: float, mods: list[str]):
    config = make_config(scoring_rules=RULES)
    lst = parse_frame(
        make_listing_frame(
            listing_id="x", price={"amount": amount, "currency": "divine"}, mods=mods
        )
    )
    return margin.evaluate(
        lst,
        PriceBook(config),
        config.thresholds,
        ModRules([]),
        ModScoring(config.mod_scoring),
    )


def test_hard_map_needs_more_profit():
    # ref 200 div; Feared + Delirious -> diff 180 -> need 20 * 180/100 = 36 div
    mods = ["Area contains The Feared", "Players in Area are 100% Delirious"]

    cheap = decide_with_scoring(80, mods)  # 120 div profit
    assert cheap.required_profit_div == 36
    assert cheap.verdict == "alert"

    pricey = decide_with_scoring(175, mods)  # 25 div profit < 36
    assert pricey.verdict == "below_threshold"


def test_clean_map_needs_proportionally_less():
    # clean map: diff 25 -> need 20 * 25/100 = 5 div (threshold is calibrated
    # for a 100-difficulty map; easier maps need proportionally less)
    d = decide_with_scoring(170, [])  # 30 div profit
    assert d.required_profit_div == 5
    assert d.verdict == "alert"


def test_special_warning_carried_on_decision():
    d = decide_with_scoring(100, ["Players who Die in area are sent to the Void"])
    assert d.special_warnings == (("VOID", "red"),)
    assert d.difficulty == 50  # 25 x 2.0
    assert d.verdict == "alert"  # 100 div profit >= 20 * 50/100 = 10


def test_annotate_marks_scoring_mods():
    annotated = SCORING.annotate(
        [
            "Area contains The Feared",
            "Players in Area are 100% Delirious",
            "Monsters have 40% increased Attack Speed",
        ]
    )
    assert annotated == (
        ("Area contains The Feared", "base 100", "red"),  # base >= 100 -> red
        ("Players in Area are 100% Delirious", "×1.8", "red"),  # >1.4 -> red
        ("Monsters have 40% increased Attack Speed", "", "none"),
    )


def test_base_difficulty_colour_boundary():
    """Base mods go red only once they alone reach the reference difficulty
    of 100; lesser ones (Einhar 80, The Twisted 50) stay yellow."""
    from sniper.config import ModScoringConfig, ModScoringRule
    from sniper.modrules import ModScoring

    tiers = ModScoring(
        ModScoringConfig(
            base_default=25,
            rules=(
                ModScoringRule(label="just under", match="einhar", min_base=99),
                ModScoringRule(label="at the line", match="feared", min_base=100),
                ModScoringRule(label="well over", match="twisted", min_base=150),
            ),
        )
    )
    rows = tiers.annotate(["einhar mod", "feared mod", "twisted mod"])
    assert [level for _, _, level in rows] == ["yellow", "red", "red"]


def test_annotate_unmatched_rule_not_shown():
    # Delirious rule exists but this map has no delirium: no annotation
    annotated = SCORING.annotate(["Monsters deal extra damage"])
    assert annotated == (("Monsters deal extra damage", "", "none"),)


def test_annotate_severity_tiers():
    from sniper.config import ModScoringConfig, ModScoringRule
    from sniper.modrules import ModScoring

    tiers = ModScoring(
        ModScoringConfig(
            base_default=25,
            rules=(
                ModScoringRule(label="free", match="cull", multiplier=1.0),
                ModScoringRule(label="mild", match="maven", multiplier=1.2),
                ModScoringRule(label="edge", match="petrif", multiplier=1.4),
                ModScoringRule(label="harsh", match="cannot block", multiplier=2.5),
            ),
        )
    )
    annotated = tiers.annotate(
        ["cull mod", "maven mod", "petrif mod", "cannot block mod", "plain mod"]
    )
    assert [(note, level) for _, note, level in annotated] == [
        ("×1", "none"),  # ×1.0: annotated but uncolored
        ("×1.2", "yellow"),
        ("×1.4", "yellow"),  # boundary: 1.4 is still yellow
        ("×2.5", "red"),
        ("", "none"),
    ]


def test_annotate_stamps_warning_emoji_on_mod_text():
    void = SCORING.annotate(["Players who Die in area are sent to the Void"])
    assert void == (("❗ Players who Die in area are sent to the Void", "×2", "red"),)
    # warning-only rules color their row too, not just the chip
    bismuth = SCORING.annotate(["Area contains a Bismuth Ore Deposit"])
    assert bismuth == (("⚠️ Area contains a Bismuth Ore Deposit", "", "yellow"),)
    blight = SCORING.annotate(["Area contains a Blight Encounter"])
    assert blight == (("⚠️ Area contains a Blight Encounter", "", "yellow"),)


def test_combo_pair_merges_into_one_display_row():
    from sniper.config import ModScoringConfig, ModScoringRule
    from sniper.modrules import ModScoring

    scoring = ModScoring(
        ModScoringConfig(
            base_default=25,
            rules=(
                ModScoringRule(
                    label="Less damage per item",
                    match_all=("less damage per equipped item", "less damage per item equipped"),
                    multiplier=2.3,
                ),
                ModScoringRule(label="Reflect", match="reflect", multiplier=1.1),
            ),
        )
    )
    rows = scoring.annotate(
        [
            "Players deal 10% less Damage per Equipped Item",
            "Monsters reflect 18% of Elemental Damage",
            "Players' Minions deal 10% less Damage per Item Equipped by their Master",
        ]
    )
    # pair merged into ONE row, ×2.3 shown once; unrelated line untouched
    assert rows == (
        (
            # one row, one multiplier - but the two mod texts keep their
            # own lines rather than running together
            "Players deal 10% less Damage per Equipped Item\n"
            "Players' Minions deal 10% less Damage per Item Equipped by their Master",
            "×2.3",
            "red",
        ),
        ("Monsters reflect 18% of Elemental Damage", "×1.1", "yellow"),
    )


def test_half_pair_line_stays_single_when_combo_not_matched():
    from sniper.config import ModScoringConfig, ModScoringRule
    from sniper.modrules import ModScoring

    scoring = ModScoring(
        ModScoringConfig(
            base_default=25,
            rules=(ModScoringRule(label="pair", match_all=("aaa", "bbb"), multiplier=2.0),),
        )
    )
    rows = scoring.annotate(["aaa only line"])  # combo requires both -> no match
    assert rows == (("aaa only line", "", "none"),)


# --------------------------------------------------------- shipped config.yaml
# The real config.yaml is loaded at startup and rewritten by the settings
# panel; a typo there is a SystemExit, not a test failure, so guard it here.
# Mod wordings below are verbatim from logs/ captures.

BREACH_LINES = [
    "All Unstable Breaches must be Stabilised and Closed to claim Reward",
    "Unstable Breaches in Area contain a Boss",
    "Area contains 2 additional unstable Breaches",
]


def _shipped_scoring():
    from pathlib import Path

    from sniper.config import load_config

    config = load_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
    return ModScoring(config.mod_scoring)


def _rule(scoring, label):
    """The shipped rule with this label - values are user-tuned, so tests
    read them rather than hard-coding a number that drifts."""
    return next(c.rule for c in scoring._rules if c.rule.label == label)


def test_shipped_config_breach_is_one_merged_row():
    """All three breach lines always ship together: one rule, one multiplier,
    one row - with the three texts still on their own lines."""
    scoring = _shipped_scoring()
    rule = _rule(scoring, "BREACH")
    result = scoring.evaluate(BREACH_LINES)
    assert ("BREACH", "yellow") in result.warnings
    assert result.score == scoring._config.base_default * rule.multiplier
    rows = [r for r in scoring.annotate(BREACH_LINES) if "Breach" in r[0]]
    assert len(rows) == 1  # merged, not three separate rows
    text, note, level = rows[0]
    assert note == f"×{rule.multiplier:g}"
    assert level == "yellow"  # warning rules color the row, not just the chip
    assert text.startswith("⚠️ ")
    assert text.count("\n") == 2  # three lines, not one run-on string


def test_shipped_config_breach_variants_all_match():
    """Only the count line varies ('an' / 2 / 3 additional)."""
    scoring = _shipped_scoring()
    for count_line in (
        "Area contains an additional unstable Breach",
        "Area contains 2 additional unstable Breaches",
        "Area contains 3 additional unstable Breaches",
    ):
        mods = BREACH_LINES[:2] + [count_line]
        assert "BREACH" in scoring.evaluate(mods).matched, count_line


def test_shipped_config_shroud_walker():
    scoring = _shipped_scoring()
    result = scoring.evaluate(["Players have Shroud Walker"])
    assert "Shroud Walker" in result.matched
    expected = scoring._config.base_default * _rule(scoring, "Shroud Walker").multiplier
    assert result.score == pytest.approx(expected)


def test_shipped_config_feared_is_red():
    scoring = _shipped_scoring()
    base = _rule(scoring, "The Feared").min_base
    rows = scoring.annotate(["Area contains The Feared"])
    assert rows == (("Area contains The Feared", f"base {base:g}", "red"),)


def test_shipped_config_lesser_base_mods_stay_yellow():
    scoring = _shipped_scoring()
    (_, _, level) = scoring.annotate(["Area contains Einhar, Beastmaster"])[0]
    assert level == "yellow"


THORNS_LINES = [
    "Rare Monsters have Physical Thorns reflecting 4000 Physical Damage",
    "Rare Monsters have Elemental Thorns reflecting 4000 Elemental Damage",
]


def test_shipped_config_thorns_is_one_modifier_on_two_lines():
    """The physical/elemental thorns pair always ships together: one rule,
    one multiplier, one row - but each line keeps its own line."""
    scoring = _shipped_scoring()
    rule = _rule(scoring, "Thorns")
    result = scoring.evaluate(THORNS_LINES)
    # exactly one rule: the lines say "...Thorns reflecting...", so a rule
    # matching bare "reflect" would double up on the same mod
    assert result.matched == ("Thorns",)
    assert result.score == pytest.approx(scoring._config.base_default * rule.multiplier)
    rows = scoring.annotate(THORNS_LINES)
    assert len(rows) == 1
    text, note, _level = rows[0]
    assert note == f"×{rule.multiplier:g}"
    assert text.split("\n") == THORNS_LINES


def test_shipped_config_has_no_reflect_rule():
    """Thorns replaced the reflect mods; a leftover reflect rule would
    double-count the thorns lines ("...Thorns reflecting...")."""
    scoring = _shipped_scoring()
    assert not [c for c in scoring._rules if "reflect" in c.rule.label.lower()]
