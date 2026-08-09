"""scoring_overrides.yaml round-trip: tuning-panel save -> config-load merge."""

from sniper.config import (
    ModScoringConfig,
    ModScoringRule,
    apply_scoring_overrides,
    save_scoring_overrides,
)

BASE = ModScoringConfig(
    base_default=25,
    div_per_point=0.2,
    rules=(
        ModScoringRule(label="The Feared", match="area contains the feared", min_base=100),
        ModScoringRule(label="Reflect", match="reflect", multiplier=1.1),
        ModScoringRule(label="VOID", match="sent to the void", multiplier=2.0, warning="red"),
    ),
)


def test_missing_file_is_noop(tmp_path):
    assert apply_scoring_overrides(BASE, tmp_path / "nope.yaml") is BASE


def test_round_trip_preserves_patterns_and_warnings(tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    tuned = ModScoringConfig(
        base_default=30,
        div_per_point=0.5,
        rules=(
            ModScoringRule(label="The Feared", match="area contains the feared", min_base=150),
            ModScoringRule(label="Reflect", match="reflect", multiplier=1.25),
            ModScoringRule(label="VOID", match="sent to the void", multiplier=2.5, warning="red"),
        ),
    )
    save_scoring_overrides(tuned, path)

    merged = apply_scoring_overrides(BASE, path)
    assert merged.base_default == 30
    assert merged.div_per_point == 0.5
    by_label = {r.label: r for r in merged.rules}
    assert by_label["The Feared"].min_base == 150
    assert by_label["Reflect"].multiplier == 1.25
    assert by_label["VOID"].multiplier == 2.5
    # non-numeric fields come from the base config, not the overrides file
    assert by_label["VOID"].warning == "red"
    assert by_label["The Feared"].match == "area contains the feared"


def test_partial_overrides(tmp_path):
    path = tmp_path / "scoring_overrides.yaml"
    path.write_text('rules:\n  "Reflect": { multiplier: 1.5 }\n', encoding="utf-8")
    merged = apply_scoring_overrides(BASE, path)
    by_label = {r.label: r for r in merged.rules}
    assert by_label["Reflect"].multiplier == 1.5
    assert by_label["The Feared"].min_base == 100  # untouched
    assert merged.base_default == 25
