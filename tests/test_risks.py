"""Failure risks: mods that can make the map FAIL, losing what you paid.

A risk turns net into an expected value,
    net = (1 - fail_chance) x market price - asking price - running cost
so it flows through yield, the alert cutoff, jackpots and hotkey ranking.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.config import (
    SCORING_OVERRIDES_NAME,
    ConfigError,
    ModRisk,
    ModScoringConfig,
    load_config,
    save_scoring_overrides,
)
from sniper.models import parse_frame
from sniper.modrules import ModRules, ModScoring
from sniper.prices import PriceBook

NO_DMG = "Players deal 10% less Damage per Equipped Item"
DELIRIOUS = "Players in Area are 100% Delirious"
ULTIMATUM = "Areas contain an Ultimatum Encounter"
NO_ULTIMATUM = "Area has no chance to contain Ultimatum Encounters"
BREACH = "All Unstable Breaches must be Stabilised and Closed to claim Reward"
BISMUTH = "Area contains a Bismuth Ore Deposit"
FATAL = "Area becomes fatal after some time"
BLIGHT = "Area contains a Blight Encounter"
RARES = "Rare Monsters each have 2 additional Modifiers"
RARES_ONE = "Rare Monsters each have 1 additional Modifier"
LEGION = "Area contains an additional Legion Encounter"
MELEE = "Monsters can only be Damaged while within 2 metres of a Player"


def shipped() -> ModScoring:
    return ModScoring(load_config(Path(__file__).parent.parent / "config.yaml").mod_scoring)


def risk(label, *patterns, chance, regex=False) -> ModRisk:
    return ModRisk(label=label, match_all=patterns, fail_chance=chance, regex=regex)


# ------------------------------------------------------- the shipped config


@pytest.mark.parametrize(
    ("mods", "label", "chance"),
    [
        ([ULTIMATUM, DELIRIOUS], "Ultimatum + less damage/delirium", 0.25),
        ([ULTIMATUM, NO_DMG], "Ultimatum + less damage/delirium", 0.25),
        ([BREACH, NO_DMG], "Breach + less damage/delirium", 1.0),
        ([BREACH, DELIRIOUS], "Breach + less damage/delirium", 1.0),
        ([BISMUTH], "Bismuth", 0.30),
        ([ULTIMATUM], "Ultimatum", 0.10),  # even a clean one can brick
        ([ULTIMATUM, FATAL], "Ultimatum + fatal timer", 0.80),
        ([BLIGHT, RARES], "Blight + rares extra modifiers", 0.50),
        ([BLIGHT, RARES_ONE], "Blight + rares extra modifiers", 0.50),
        # every league mechanic can fail: a 5% floor
        ([BLIGHT], "Blight", 0.05),
        ([LEGION], "Legion", 0.05),
        ([BREACH], "Breach", 0.05),
        ([LEGION, MELEE], "Legion + melee-range only", 0.25),
        (["Only opens 1 Portal to Area"], "1 portal", 0.03),
    ],
)
def test_shipped_risks_fire_on_real_mod_text(mods, label, chance):
    """Wording taken from logs/."""
    found = {lbl: c for lbl, _note, c in shipped().evaluate(mods).risks}
    assert found.get(label) == chance, found


@pytest.mark.parametrize(
    "mods",
    [
        [NO_ULTIMATUM, DELIRIOUS],  # the "no chance of Ultimatum" line
        [NO_DMG, DELIRIOUS],  # the penalties alone are difficulty, not failure
        [FATAL],  # the timer alone is difficulty, not failure
        [RARES],  # tougher rares alone are difficulty, not failure
    ],
)
def test_shipped_risks_stay_quiet_otherwise(mods):
    assert shipped().evaluate(mods).risks == ()


@pytest.mark.parametrize(
    ("mods", "label", "chance"),
    [
        ([BLIGHT, RARES], "Blight + rares extra modifiers", 0.50),
        ([BREACH, NO_DMG], "Breach + less damage/delirium", 1.0),
        ([BREACH, DELIRIOUS], "Breach + less damage/delirium", 1.0),
        ([LEGION, MELEE], "Legion + melee-range only", 0.25),
    ],
)
def test_the_5_percent_floor_steps_aside_for_a_bigger_risk(mods, label, chance):
    """A floor, not an extra: Blight + rares stays 50%, not 52.5%."""
    result = shipped().evaluate(mods)
    assert [lbl for lbl, _n, _c in result.risks] == [label]
    assert result.fail_chance == pytest.approx(chance)


def test_each_mechanic_carries_its_own_floor():
    """Two mechanics on one map are two chances to fail."""
    result = shipped().evaluate([BLIGHT, LEGION, ULTIMATUM])
    assert {lbl for lbl, _n, _c in result.risks} == {"Blight", "Legion", "Ultimatum"}
    assert result.fail_chance == pytest.approx(1 - 0.95 * 0.95 * 0.90)


@pytest.mark.parametrize("penalty", [NO_DMG, DELIRIOUS])
def test_plain_ultimatum_steps_aside_for_the_penalty_combo(penalty):
    """`unless` keeps the two Ultimatum risks from stacking: with less
    damage or delirium it is 25%, not 1 - 0.9 x 0.75 = 32.5%."""
    result = shipped().evaluate([ULTIMATUM, penalty])
    assert [label for label, _n, _c in result.risks] == ["Ultimatum + less damage/delirium"]
    assert result.fail_chance == pytest.approx(0.25)


@pytest.mark.parametrize(
    "mods", [[ULTIMATUM, FATAL], [ULTIMATUM, FATAL, NO_DMG], [ULTIMATUM, FATAL, DELIRIOUS]]
)
def test_fatal_timer_replaces_the_other_ultimatum_risks(mods):
    """80% flat, never stacked with the 10% or 25% entries."""
    result = shipped().evaluate(mods)
    assert [label for label, _n, _c in result.risks] == ["Ultimatum + fatal timer"]
    assert result.fail_chance == pytest.approx(0.80)


def test_unless_parses_from_a_string_or_list(tmp_path):
    for unless in ('"delirious"', '["delirious", "less damage"]'):
        path = write(
            tmp_path, f'  - {{label: U, match: "ultimatum", unless: {unless}, fail_chance: 0.1}}'
        )
        sc = ModScoring(load_config(path).mod_scoring)
        assert sc.evaluate([ULTIMATUM]).fail_chance == pytest.approx(0.1)
        assert sc.evaluate([ULTIMATUM, DELIRIOUS]).risks == ()


def test_shipped_risks_all_carry_a_note():
    for r in load_config(Path(__file__).parent.parent / "config.yaml").mod_scoring.risks:
        assert r.note, r.label


# ------------------------------------------------------------------- maths


def test_independent_risks_combine():
    """Two risks are 1 - (1-a)(1-b), not a + b: 30% and 25% is 47.5%."""
    sc = ModScoring(
        ModScoringConfig(
            risks=(risk("a", "bismuth", chance=0.30), risk("b", "ultimatum", chance=0.25))
        )
    )
    result = sc.evaluate([BISMUTH, ULTIMATUM])
    assert len(result.risks) == 2
    assert result.fail_chance == pytest.approx(0.475)


def test_zero_chance_switches_a_risk_off():
    sc = ModScoring(ModScoringConfig(risks=(risk("a", "bismuth", chance=0.0),)))
    assert sc.evaluate([BISMUTH]).risks == ()


def decide(mods, risks, amount=100.0):
    config = make_config(min_yield=1.0)  # Mageblood reference: 200 divine
    frame = make_listing_frame(price={"amount": amount, "currency": "divine"}, mods=mods)
    return margin.evaluate(
        parse_frame(frame),
        PriceBook(config),
        config.thresholds,
        ModRules([]),
        ModScoring(ModScoringConfig(base_default=100.0, risks=risks)),
    )


def test_net_becomes_an_expected_value():
    """200d reward, 100d price, 30% fail: 0.7 x 200 - 100 = +40, and the
    risk costs 30% of the reward's 200 = 60."""
    d = decide([BISMUTH], (risk("Bismuth", "bismuth", chance=0.30),))
    assert d.fail_chance == pytest.approx(0.30)
    assert d.net_div == pytest.approx(40.0)
    assert d.risk_cost_div == pytest.approx(60.0)
    assert d.risks == (("Bismuth", "", 0.30),)
    assert d.verdict == "alert"


def test_no_risk_leaves_net_unchanged():
    d = decide(["Monsters have 100% increased Area of Effect"], ())
    assert d.net_div == pytest.approx(100.0)
    assert d.fail_chance == 0.0 and d.risk_cost_div == 0.0


def test_a_certain_failure_never_alerts_however_cheap():
    """Unclearable breach: a 1d listing of a 200d reward still loses money."""
    breach = risk("Breach", "unstable breaches", "less damage", chance=1.0)
    d = decide([BREACH, NO_DMG], (breach,), amount=1)
    assert d.net_div < 0
    assert d.verdict == "below_threshold"


def test_risk_can_drop_a_listing_below_the_threshold():
    """Without the risk this alerts at +100; a 60% fail turns it into
    0.4 x 200 - 100 = -20."""
    d = decide([BISMUTH], (risk("Bismuth", "bismuth", chance=0.60),))
    assert d.net_div == pytest.approx(-20.0)
    assert d.verdict == "below_threshold"


# ------------------------------------------------------------------ config


BASE = """
mod_risks:
{entries}
"""


def write(tmp_path, entries: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(BASE.format(entries=entries), encoding="utf-8")
    return path


def test_single_match_string_is_accepted(tmp_path):
    cfg = load_config(write(tmp_path, '  - {label: B, match: "bismuth", fail_chance: 0.3}'))
    assert cfg.mod_scoring.risks[0].match_all == ("bismuth",)


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ('  - {label: B, match: "bismuth", fail_chance: 30}', "0.3 means 30%"),
        ('  - {label: B, match: "bismuth"}', "fail_chance"),
        ("  - {label: B, fail_chance: 0.3}", "match"),
        (
            '  - {label: B, match: "x", fail_chance: 0.1}\n'
            '  - {label: B, match: "y", fail_chance: 0.1}',
            "two entries",
        ),
    ],
)
def test_bad_risks_raise_precise_errors(tmp_path, entry, needle):
    with pytest.raises(ConfigError, match=needle):
        load_config(write(tmp_path, entry))


def test_settings_panel_chances_round_trip(tmp_path):
    """The ⚙ panel saves each risk's chance; the next start uses it."""
    path = write(tmp_path, '  - {label: Bismuth, match: "bismuth", fail_chance: 0.3}')
    scoring = load_config(path).mod_scoring
    edited = ModScoringConfig(
        base_default=scoring.base_default,
        rules=scoring.rules,
        risks=(risk("Bismuth", "bismuth", chance=0.12),),
    )
    save_scoring_overrides(edited, tmp_path / SCORING_OVERRIDES_NAME)
    assert load_config(path).mod_scoring.risks[0].fail_chance == pytest.approx(0.12)
