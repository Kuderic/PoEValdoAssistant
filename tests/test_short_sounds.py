"""Built-in short alert sounds (alerts.short_sounds).

The Windows sounds they replace run 1.4-4s; a sound still playing invites
waiting for it to end before pressing the hotkey. These are over in at most
a third of a second and loud within a couple of milliseconds, at the same
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
from sniper.config import load_config
from sniper.sound import SHORT_LEVELS, SHORT_NOTES, short_wav, synth_wav

KINDS = ("valdo", "generic", "jackpot", "super")


def samples(path) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as w:
        assert (w.getsampwidth(), w.getcomptype()) == (2, "NONE")
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a, w.getframerate()


@pytest.mark.parametrize("kind", KINDS)
def test_every_alert_sound_is_short_and_loud_at_once(kind):
    a, rate = samples(short_wav(kind))
    assert len(a) / rate <= 0.35
    peak = max(abs(x) for x in a)
    first_half = next(i for i, x in enumerate(a) if abs(x) >= peak / 2)
    assert first_half / rate < 0.005  # no fade-in to wait through
    assert abs(a[0]) < 200 and abs(a[-1]) < 200  # starts and ends silent: no click


@pytest.mark.parametrize("kind", KINDS)
def test_each_is_as_loud_as_the_sound_it_replaces(kind):
    a, rate = samples(short_wav(kind))
    win = int(rate * sound.LEVEL_WINDOW_S)
    loudest = max(
        math.sqrt(sum(x * x for x in a[i : i + win]) / win) / 32767
        for i in range(0, len(a) - win + 1, win // 4)
    )
    assert loudest == pytest.approx(SHORT_LEVELS[kind], rel=0.1)
    assert max(abs(x) for x in a) / 32767 <= sound.SYNTH_PEAK


def test_the_kinds_still_sound_different():
    assert len({SHORT_NOTES[k] for k in KINDS}) == len(KINDS)
    assert len({short_wav(k).read_bytes() for k in KINDS}) == len(KINDS)


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


def test_short_sounds_are_the_default_and_can_be_turned_off(tmp_path):
    assert load_config("config.yaml").alerts.short_sounds is True
    cfg = tmp_path / "config.yaml"
    cfg.write_text("alerts:\n  short_sounds: false\n", encoding="utf-8")
    assert load_config(cfg).alerts.short_sounds is False


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


def test_switched_off_it_plays_the_windows_sounds(make_overlay):
    ov = make_overlay(short_sounds=False)
    expected = sound.source_path("", sound.GENERIC_WAV)
    assert ov._alert_wav("generic") == (None if expected is None else str(expected))


def test_a_sound_file_of_your_own_still_wins(make_overlay, tmp_path):
    mine = tmp_path / "mine.wav"
    mine.write_bytes(synth_wav(((440, 0.2),)))
    assert make_overlay(sound=str(mine))._alert_wav("valdo") == str(mine)
