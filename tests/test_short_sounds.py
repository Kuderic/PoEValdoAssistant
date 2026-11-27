"""Built-in alert sounds (alerts.sound_style: short blips / soft jingle).

The Windows sounds they replace run 1.4-4s; a sound still playing invites
waiting for it to end before pressing the hotkey. These are over in at most
half a second and loud within a few milliseconds, at the same
loudness as the sound each replaces (those Windows files are mastered
quiet, so full-scale blips would be ~18dB louder at the same volume).
"""

from __future__ import annotations

import array
import math
import tkinter as tk
import wave
from dataclasses import replace

import pytest
from conftest import make_config

from sniper import sound
from sniper.config import (
    SCORING_OVERRIDES_NAME,
    ConfigError,
    ModScoringConfig,
    load_config,
    load_extra_overrides,
    save_scoring_overrides,
)
from sniper.sound import SHORT_LEVELS, SHORT_NOTES, builtin_wav, short_wav, synth_wav

KINDS = ("valdo", "generic", "jackpot", "super")
STYLES = ("short", "jingle")
# the longest each style may run: short enough not to wait for
MAX_S = {"short": 0.35, "jingle": 0.5}


def samples(path) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as w:
        assert (w.getsampwidth(), w.getcomptype()) == (2, "NONE")
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a, w.getframerate()


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("kind", KINDS)
def test_every_alert_sound_is_short_and_loud_at_once(kind, style):
    a, rate = samples(builtin_wav(kind, style))
    assert len(a) / rate <= MAX_S[style]
    peak = max(abs(x) for x in a)
    first_half = next(i for i, x in enumerate(a) if abs(x) >= peak / 2)
    assert first_half / rate < 0.006  # no fade-in to wait through
    assert abs(a[0]) < 200 and abs(a[-1]) < 200  # starts and ends silent: no click


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("kind", KINDS)
def test_each_is_as_loud_as_the_sound_it_replaces(kind, style):
    a, rate = samples(builtin_wav(kind, style))
    win = int(rate * sound.LEVEL_WINDOW_S)
    loudest = max(
        math.sqrt(sum(x * x for x in a[i : i + win]) / win) / 32767
        for i in range(0, len(a) - win + 1, win // 4)
    )
    assert loudest == pytest.approx(SHORT_LEVELS[kind], rel=0.1)
    assert max(abs(x) for x in a) / 32767 <= sound.SYNTH_PEAK


def test_the_kinds_still_sound_different():
    assert len({SHORT_NOTES[k] for k in KINDS}) == len(KINDS)
    assert len({sound.JINGLE_NOTES[k] for k in KINDS}) == len(KINDS)
    files = {builtin_wav(k, st).read_bytes() for k in KINDS for st in STYLES}
    assert len(files) == len(KINDS) * len(STYLES)


def test_the_windows_style_has_no_built_in_sound():
    assert builtin_wav("valdo", "windows") is None


def test_the_file_is_built_once_and_reused():
    first = short_wav("valdo")
    mtime = first.stat().st_mtime_ns
    assert short_wav("valdo") == first
    assert first.stat().st_mtime_ns == mtime


def test_an_unknown_kind_has_no_built_in_sound():
    assert short_wav("error") is None


def test_a_silent_recipe_does_not_divide_by_zero():
    a = array.array("h")
    with wave.open(__import__("io").BytesIO(synth_wav(((0, 0.05),))), "rb") as w:
        a.frombytes(w.readframes(w.getnframes()))
    assert set(a) == {0}


def test_short_is_the_default_style(tmp_path):
    assert load_config("config.yaml").alerts.sound_style == "short"


@pytest.mark.parametrize(
    ("body", "style"),
    [
        ("alerts:\n  sound_style: jingle\n", "jingle"),
        ("alerts:\n  sound_style: windows\n", "windows"),
        # 1.3.11's switch still works...
        ("alerts:\n  short_sounds: false\n", "windows"),
        # ...but an explicit style wins over it
        ("alerts:\n  short_sounds: false\n  sound_style: jingle\n", "jingle"),
    ],
)
def test_the_style_is_read_from_config(tmp_path, body, style):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    assert load_config(cfg).alerts.sound_style == style


def test_an_unknown_style_is_a_config_error(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("alerts:\n  sound_style: loud\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="sound_style"):
        load_config(cfg)


def test_the_panels_choice_is_saved_and_wins_over_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("alerts:\n  sound_style: windows\n", encoding="utf-8")
    save_scoring_overrides(
        ModScoringConfig(base_default=25.0, rules=()),
        tmp_path / SCORING_OVERRIDES_NAME,
        sound_style="jingle",
    )
    assert load_config(cfg).alerts.sound_style == "jingle"


@pytest.fixture
def make_overlay():
    from sniper.bus import Bus
    from sniper.overlay import Overlay

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()

    def build(**alerts) -> Overlay:
        config = make_config()
        config = replace(config, alerts=replace(config.alerts, volume=1.0, **alerts))
        return Overlay(root, Bus(), config)

    yield build
    root.destroy()


@pytest.mark.parametrize("kind", KINDS)
def test_the_overlay_plays_the_short_sound_three_times_by_default(make_overlay, kind):
    assert make_overlay()._alert_wav(kind) == str(builtin_wav(kind, "short", 3))


@pytest.mark.parametrize("kind", KINDS)
def test_the_jingle_style_plays_the_jingle(make_overlay, kind):
    ov = make_overlay(sound_style="jingle", sound_plays=1)
    assert ov._alert_wav(kind) == str(builtin_wav(kind, "jingle"))


# ------------------------------------------------------------------ repeats


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("kind", KINDS)
def test_more_plays_repeat_the_pattern_and_still_start_at_once(kind, style):
    once, rate = samples(builtin_wav(kind, style, 1))
    thrice, _ = samples(builtin_wav(kind, style, 3))
    gap = int(rate * sound.REPEAT_GAP_S)
    assert len(thrice) == 3 * len(once) + 2 * gap
    assert thrice[: len(once)] == once  # the first play is the same sound
    assert thrice[len(once) + gap : 2 * len(once) + gap] == once
    assert set(thrice[len(once) : len(once) + gap]) == {0}  # a clean gap


def test_plays_are_clamped():
    assert builtin_wav("valdo", "short", 0) == builtin_wav("valdo", "short", 1)
    top = sound.MAX_SOUND_PLAYS
    assert builtin_wav("valdo", "short", top + 3) == builtin_wav("valdo", "short", top)


def test_three_plays_is_the_default(tmp_path):
    assert load_config("config.yaml").alerts.sound_plays == 3


@pytest.mark.parametrize("value", ["0", "5", "'2'", "true", "1.5"])
def test_bad_plays_are_a_config_error(tmp_path, value):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"alerts:\n  sound_plays: {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="sound_plays"):
        load_config(cfg)


def test_the_windows_style_ignores_plays(make_overlay):
    ov = make_overlay(sound_style="windows", sound_plays=4)
    expected = sound.source_path("", sound.GENERIC_WAV)
    assert ov._alert_wav("generic") == (None if expected is None else str(expected))


def test_picking_plays_in_the_panel_switches_saves_and_plays_it(make_overlay, tmp_path):
    ov = make_overlay()
    ov._overrides_path = tmp_path / SCORING_OVERRIDES_NAME
    played: list[str] = []
    ov._play_sound = lambda kind="valdo": played.append(kind)
    ov._open_tuning()
    ov._plays_pick(1)
    assert ov._alert_wav("valdo") == str(short_wav("valdo"))
    assert played == ["valdo"]
    assert load_extra_overrides(tmp_path / SCORING_OVERRIDES_NAME)["sound_plays"] == 1
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    assert load_config(cfg).alerts.sound_plays == 1


def test_the_windows_style_plays_the_windows_sounds(make_overlay):
    ov = make_overlay(sound_style="windows")
    expected = sound.source_path("", sound.GENERIC_WAV)
    assert ov._alert_wav("generic") == (None if expected is None else str(expected))


def test_picking_a_style_in_the_panel_switches_saves_and_plays_it(make_overlay, tmp_path):
    ov = make_overlay()
    ov._config = replace(ov._config, state_dir=tmp_path)
    ov._overrides_path = tmp_path / SCORING_OVERRIDES_NAME
    played: list[str] = []
    ov._play_sound = lambda kind="valdo": played.append(kind)
    ov._open_tuning()
    ov._style_pick("jingle")
    assert ov._alert_wav("valdo") == str(builtin_wav("valdo", "jingle", 3))
    assert played == ["valdo"]  # heard at once
    assert load_extra_overrides(tmp_path / SCORING_OVERRIDES_NAME)["sound_style"] == "jingle"
    ov._style_pick("jingle")  # the current one again: replayed, nothing to save
    assert played == ["valdo", "valdo"]


def test_a_sound_file_of_your_own_still_wins(make_overlay, tmp_path):
    mine = tmp_path / "mine.wav"
    mine.write_bytes(synth_wav(((440, 0.2),)))
    assert make_overlay(sound=str(mine))._alert_wav("valdo") == str(mine)
