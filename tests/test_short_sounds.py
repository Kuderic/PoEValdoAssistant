"""Built-in alert sounds (alerts.sound_style: short blips / soft jingle,
and a jackpot fanfare in either).

The Windows sounds they replace run 1.4-4s; a sound still playing invites
waiting for it to end before pressing the hotkey. Routine alerts are over in
under half a second; the jackpot fanfares last about a second but are just
as loud just as fast, and are never a repeated beep. All match the loudness
of the sound each replaces (those Windows files are mastered quiet, so
full-scale blips would be ~18dB louder at the same volume).
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
from sniper.sound import SHORT_LEVELS, builtin_wav, short_wav, synth_wav

KINDS = ("valdo", "generic", "jackpot", "super")
ROUTINE = ("valdo", "generic")
JACKPOTS = ("jackpot", "super")
STYLES = ("short", "jingle")
# the longest each may run: routine alerts short enough not to wait for
MAX_S = {"short": 0.35, "jingle": 0.5, "fanfare": 1.3}


def longest(kind: str, style: str) -> float:
    return MAX_S["fanfare" if kind in JACKPOTS else style]


def loudest_rms(a, rate) -> float:
    win = int(rate * sound.LEVEL_WINDOW_S)
    return max(
        math.sqrt(sum(x * x for x in a[i : i + win]) / win)
        for i in range(0, len(a) - win + 1, win // 4)
    )


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
    assert len(a) / rate <= longest(kind, style)
    # no fade-in to wait through: it reaches its loudest-100ms level at once
    level = loudest_rms(a, rate)
    first_loud = next(i for i, x in enumerate(a) if abs(x) >= level)
    assert first_loud / rate < 0.006
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
    files = {(k, st): builtin_wav(k, st).read_bytes() for k in KINDS for st in STYLES}
    # every routine sound is its own; each jackpot fanfare is one sound
    # shared by both styles, unlike anything routine
    assert len({files[k, st] for k in ROUTINE for st in STYLES}) == 4
    for k in JACKPOTS:
        assert files[k, "short"] == files[k, "jingle"]
    assert len(set(files.values())) == 4 + 2


@pytest.mark.parametrize("kind", JACKPOTS)
def test_a_jackpot_plays_a_fanfare_not_a_repeated_beep(kind):
    """Overlapping notes that rise and ring together: the sound never
    falls back to silence between notes, as a repeated beep does."""
    notes = sound.FANFARE_NOTES[kind]
    freqs = [f for f, *_ in notes]
    assert freqs == sorted(freqs) and len(set(freqs)) == len(freqs)
    a, rate = samples(builtin_wav(kind, "short"))
    win = int(rate * 0.01)
    body = a[: int(rate * 0.5)]  # the run and the start of the chord
    quietest = min(max(abs(x) for x in body[i : i + win]) for i in range(0, len(body) - win, win))
    assert quietest > 0.2 * max(abs(x) for x in a)
    assert len(a) / rate >= 0.8  # rings out, unlike the routine blips


def test_the_routine_sounds_play_once():
    for kind in ROUTINE:
        a, rate = samples(builtin_wav(kind, "short"))
        assert len(a) / rate < 0.2


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
def test_the_overlay_plays_the_short_sound_by_default(make_overlay, kind):
    assert make_overlay()._alert_wav(kind) == str(short_wav(kind))


@pytest.mark.parametrize("kind", KINDS)
def test_the_jingle_style_plays_the_jingle(make_overlay, kind):
    ov = make_overlay(sound_style="jingle")
    assert ov._alert_wav(kind) == str(builtin_wav(kind, "jingle"))


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
    assert ov._alert_wav("valdo") == str(builtin_wav("valdo", "jingle"))
    assert played == ["valdo"]  # heard at once
    assert load_extra_overrides(tmp_path / SCORING_OVERRIDES_NAME)["sound_style"] == "jingle"
    ov._style_pick("jingle")  # the current one again: replayed, nothing to save
    assert played == ["valdo", "valdo"]


def test_an_old_sound_plays_key_is_ignored(tmp_path):
    """1.3.13's repeat setting was withdrawn; a config naming it still loads."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("alerts:\n  sound_plays: 3\n", encoding="utf-8")
    assert not hasattr(load_config(cfg).alerts, "sound_plays")


def test_the_panel_previews_both_fanfares(make_overlay):
    ov = make_overlay()
    played: list[str] = []
    ov._play_sound = lambda kind="valdo": played.append(kind)
    ov._open_tuning()
    ov._settings_select("General")  # clicks on a hidden tab are dropped
    ov._tune_window.update()
    buttons = [
        w for w in _walk(ov._tune_window) if isinstance(w, tk.Label) and w.cget("text") == "▶ Play"
    ]
    for b in buttons:
        b.event_generate("<Button-1>")
    ov._tune_window.update()
    assert sorted(played) == ["jackpot", "super"]


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def test_a_sound_file_of_your_own_still_wins(make_overlay, tmp_path):
    mine = tmp_path / "mine.wav"
    mine.write_bytes(synth_wav(((440, 0.2),)))
    assert make_overlay(sound=str(mine))._alert_wav("valdo") == str(mine)
