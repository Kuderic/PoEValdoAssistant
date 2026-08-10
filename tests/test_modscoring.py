"""Difficulty scoring engine + its effect on the alert cutoff."""

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
        ("Area contains The Feared", "base 100", "yellow"),
        ("Players in Area are 100% Delirious", "×1.8", "red"),  # >1.4 -> red
        ("Monsters have 40% increased Attack Speed", "", "none"),
    )


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
