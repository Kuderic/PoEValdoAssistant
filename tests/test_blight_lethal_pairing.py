"""Blight + increasingly lethal + instability: a half-price Foil Mageblood
(120 div, +122 div net) with these and void alerted as a super jackpot and
killed its runner on entry. The pairing must price it out; void alone, or
one of the killers alone, must not be touched."""

from __future__ import annotations

from pathlib import Path

import pytest

from sniper.config import load_config
from sniper.modrules import ModScoring

ROOT = Path(__file__).resolve().parent.parent

DEADLY_MAGEBLOOD = [
    "Area contains a Blight Encounter",
    "Durability of Ichor Pumps in Area is 1",
    "Must successfully defend all Ichor Pumps in Area to claim Reward",
    "Players gain Instability on Kill\nUnstable Players eventually detonate",
    "Players who Die in area are sent to the Void",
    "Only opens 1 Portal to Area",
    "Area becomes increasingly lethal",
    "All Magic and Normal Monsters in Area are in a Union of Souls",
]


@pytest.fixture
def scoring(tmp_path) -> ModScoring:
    return ModScoring(load_config(ROOT / "config.yaml", state_dir=tmp_path).mod_scoring)


def test_the_deadly_mageblood_no_longer_clears_the_bar(scoring):
    result = scoring.evaluate(DEADLY_MAGEBLOOD)
    assert "Blight + lethal + instability" in {label for label, _n, _m in result.pairings}
    # it scored 1123.2 before; instability on kill has since gone ×1.5 -> ×2.5
    # and increasingly lethal ×1.8 -> ×2.3
    assert result.score == pytest.approx(1123.2 / 1.5 * 2.5 * 3 / 1.8 * 2.3)
    # at the user's 4 div per 100 difficulty it needed 44.9 div; now 134.8,
    # above the +122.3 div it was listed at
    assert 4.0 * result.score / 100 > 122.33


@pytest.mark.parametrize(
    "drop",
    ["Area contains a Blight Encounter", "Area becomes increasingly lethal", "Players gain"],
)
def test_all_three_are_needed(scoring, drop):
    mods = [m for m in DEADLY_MAGEBLOOD if not m.startswith(drop)]
    pairings = {label for label, _n, _m in scoring.evaluate(mods).pairings}
    assert "Blight + lethal + instability" not in pairings
