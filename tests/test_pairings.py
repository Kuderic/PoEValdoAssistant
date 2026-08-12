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
        (["Areas contain an Ultimatum Encounter", DELIRIOUS], "Ultimatum + delirium"),
        (["Areas contain an Ultimatum Encounter", NO_DMG], "Ultimatum + no damage"),
        (
            [
                "Area contains a Blight Encounter",
                "Monsters can only be Damaged while within 2 metres of a Player",
            ],
            "Blight + melee-range only",
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
