"""Deadly mod pairings: combinations far worse than the sum of their parts.

A pairing multiplies difficulty ON TOP of the individual rules and carries a
note explaining what actually goes wrong. It must not disturb how the mods
themselves are grouped for display - the individual rules still own those
lines.
"""

import pytest
from conftest import make_config

from sniper.config import ModPairing, ModScoringConfig, ModScoringRule
from sniper.modrules import ModScoring

FATAL = "Area becomes fatal after some time"
NO_DMG = "Players deal 10% less Damage per Equipped Item"
MINION_DMG = "Players' Minions deal 10% less Damage per Item Equipped by their Master"
DELIRIOUS = "Players in Area are 100% Delirious"

PAIRING = ModPairing(
    label="No damage + fatal timer",
    match_all=("less damage per equipped item", "becomes fatal"),
    multiplier=10.0,
    note="Impossible unless DPS-check build",
)


def scoring(**kw):
    return ModScoring(ModScoringConfig(base_default=25.0, pairings=(PAIRING,), **kw))


def test_pairing_needs_both_mods():
    sc = scoring()
    assert sc.evaluate([NO_DMG]).pairings == ()
    assert sc.evaluate([FATAL]).pairings == ()
    assert sc.evaluate([NO_DMG, FATAL]).pairings == (
        ("No damage + fatal timer", "Impossible unless DPS-check build", 10.0),
    )


def test_pairing_multiplies_on_top_of_the_individual_rules():
    """The point of a pairing: the combination costs more than either mod."""
    rules = (
        ModScoringRule(label="Less damage", match="less damage per equipped item", multiplier=2.0),
        ModScoringRule(label="Fatal", match="becomes fatal", multiplier=2.5),
    )
    sc = scoring(rules=rules)
    apart = sc.evaluate([NO_DMG]).score  # 25 * 2
    assert apart == 50
    together = sc.evaluate([NO_DMG, FATAL]).score
    assert together == pytest.approx(25 * 2.0 * 2.5 * 10.0)  # 1250, not 125


def test_pairing_does_not_regroup_the_mod_display():
    """The two-liner combo still owns its lines; a pairing must not steal
    them into one merged row."""
    rules = (
        ModScoringRule(
            label="Less damage per item",
            match_all=("less damage per equipped item", "less damage per item equipped"),
            multiplier=2.4,
        ),
    )
    rows = scoring(rules=rules).annotate([NO_DMG, MINION_DMG, FATAL])
    texts = [t for t, _n, _l in rows]
    assert len(texts) == 2  # the pair merged, fatal separate
    assert texts[0].split("\n") == [NO_DMG, MINION_DMG]
    assert texts[1] == FATAL


def test_regex_pairing_excludes_the_negated_ultimatum_line():
    sc = ModScoring(
        ModScoringConfig(
            base_default=25.0,
            pairings=(
                ModPairing(
                    label="Ultimatum + delirium",
                    match_all=(
                        "an ultimatum encounter|ultimatum encounter waves|ultimatum modifier",
                        "100% delirious",
                    ),
                    regex=True,
                    multiplier=2.0,
                    note="Almost certain to fail Protect the Altar",
                ),
            ),
        )
    )
    no_ult = ["Area has no chance to contain Ultimatum Encounters", DELIRIOUS]
    assert sc.evaluate(no_ult).pairings == ()
    real = ["Areas contain an Ultimatum Encounter", DELIRIOUS]
    assert len(sc.evaluate(real).pairings) == 1


# ------------------------------------------------------- the shipped config


def _shipped():
    from pathlib import Path

    from sniper.config import load_config

    return ModScoring(load_config(str(Path(__file__).parent.parent / "config.yaml")).mod_scoring)


@pytest.mark.parametrize(
    "mods,label",
    [
        ([NO_DMG, MINION_DMG, FATAL], "No damage + fatal timer"),
        ([DELIRIOUS, FATAL], "Delirium + fatal timer"),
        # Ultimatum + delirium/no damage moved to mod_risks (tests/test_risks.py)
        (
            [
                "Area contains a Blight Encounter",
                "Monsters can only be Damaged while within 2 metres of a Player",
            ],
            "Blight + melee-range only",
        ),
        (
            ["Area becomes increasingly lethal", "Players who Die in area are sent to the Void"],
            "Increasingly lethal + void",
        ),
        (
            [
                "Area contains an additional Legion Encounter",
                "Monsters can only be Damaged while within 2 metres of a Player",
            ],
            "Legion + melee-range only",
        ),
        (
            ["Area contains Sirus' Deatomisation Storms", "Players' Travel Skills are Disabled"],
            "Sirus storms + no travel skills",
        ),
        (["Area contains an additional Legion Encounter", NO_DMG], "Legion + less damage"),
        (["Area contains an additional Legion Encounter", MINION_DMG], "Legion + less damage"),
        (
            [
                "Players gain Instability on Kill\nUnstable Players eventually detonate",
                "Players who Die in area are sent to the Void",
            ],
            "Instability + void",
        ),
    ],
)
def test_shipped_pairings_fire_on_real_mod_text(mods, label):
    """Every pattern is checked against wording taken from logs/."""
    hit = [p for p, _note, _m in _shipped().evaluate(mods).pairings]
    assert label in hit, hit


def test_shipped_pairings_all_carry_a_note():
    """The note is why the pairing exists; a silent one teaches nothing."""
    config = make_config()  # noqa: F841 - keeps the conftest import meaningful
    for pairing in _shipped()._config.pairings:
        assert pairing.note, pairing.label


def test_a_clean_map_trips_no_pairing():
    assert _shipped().evaluate(["Monsters fire 2 additional Projectiles"]).pairings == ()


# ------------------------------------------------- the shipped Feared+Void

# Exact wording from real captures (grep listing_received in logs/).
FEARED = "Area contains The Feared"
VOID = "Players who Die in area are sent to the Void"


def shipped_scoring() -> ModScoring:
    from pathlib import Path

    from sniper.config import load_config

    return ModScoring(load_config(Path("config.yaml")).mod_scoring)


def test_feared_plus_void_is_configured_and_matches_the_real_wording():
    """Void alone is survivable - you just do not die. The Feared is what
    makes it lethal, so the penalty belongs on the COMBINATION, not on
    either rule."""
    scoring = shipped_scoring()
    result = scoring.evaluate([FEARED, VOID])
    assert [(label, mult) for label, _note, mult in result.pairings] == [("The Feared + void", 2.0)]


@pytest.mark.parametrize(
    "mods", [[FEARED], [VOID], [FEARED, "Monsters fire 2 additional Projectiles"]]
)
def test_neither_mod_alone_triggers_the_pairing(mods):
    assert shipped_scoring().evaluate(mods).pairings == ()


def test_the_pairing_multiplies_on_top_of_both_rules():
    """The pairing contributes exactly its own factor above whatever the two
    individual rules already score.

    Asserted as a RELATIONSHIP, not as absolute numbers: shipped_scoring()
    loads config.yaml with scoring_overrides.yaml merged in, so the VOID and
    The Feared values track whatever has been tuned in the panel. Pinning
    literals here would make this test fail every time a multiplier is
    adjusted, which is a setting, not a regression.
    """
    from dataclasses import replace

    scoring = shipped_scoring()
    together = scoring.evaluate([FEARED, VOID])
    (_label, _note, factor) = together.pairings[0]

    without = ModScoring(replace(scoring._config, pairings=()))
    assert together.score == pytest.approx(without.evaluate([FEARED, VOID]).score * factor)
    # and it really is above BOTH rules acting alone
    assert together.score > scoring.evaluate([FEARED]).score
    assert together.score > scoring.evaluate([VOID]).score


def test_the_pairing_does_not_merge_the_two_mod_lines():
    """Both mods keep their own annotated row - the pairing is shown as its
    own banner, not by collapsing the lines it matched."""
    rows = shipped_scoring().annotate([FEARED, VOID])
    assert len(rows) == 2  # two rows, not one combined line
    # each mod keeps its own row and its own note (values are user-tunable,
    # so assert the SHAPE of the note rather than the current number)
    assert FEARED in rows[0][0] and rows[0][1].startswith("base ")
    assert VOID in rows[1][0] and rows[1][1].startswith("×")


@pytest.mark.parametrize(
    ("mods", "label", "mult"),
    [
        (["Area becomes increasingly lethal"], "Increasingly lethal + void", 2.0),
        (["Players who Die in area are sent to the Void"], "Increasingly lethal + void", 2.0),
        (["Area contains an additional Legion Encounter"], "Legion + melee-range only", 2.0),
        (["Area contains Sirus' Deatomisation Storms"], "Sirus storms + no travel skills", 1.5),
        (["Area contains an additional Legion Encounter"], "Legion + less damage", 2.0),
        ([NO_DMG, MINION_DMG], "Legion + less damage", 2.0),
        (["Players gain Instability on Kill"], "Instability + void", 2.0),
        (["Players who Die in area are sent to the Void"], "Instability + void", 2.0),
    ],
)
def test_new_pairings_need_both_halves_and_carry_their_multiplier(mods, label, mult):
    sc = _shipped()
    assert label not in [p for p, _n, _m in sc.evaluate(mods).pairings]
    shipped = {p.label: p.multiplier for p in sc._config.pairings}
    assert shipped[label] == mult
