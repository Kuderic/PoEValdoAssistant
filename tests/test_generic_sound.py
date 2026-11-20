"""Non-Valdo alerts play their own sound.

These hits are rare and usually far more profitable than a routine Valdo,
so the point is telling them apart without looking at the screen.
"""

from __future__ import annotations

from pathlib import Path

from sniper import sound
from sniper.config import AlertsConfig


def test_the_two_defaults_are_different_files():
    assert sound.GENERIC_WAV != sound.DEFAULT_WAV


def test_generic_default_is_short_enough_to_not_outlive_its_alert():
    """Alerts expire in seconds and SND_ASYNC bursts cancel each other, so a
    5s Windows alarm would still be playing after the travel it announced."""
    import contextlib
    import wave

    if not sound.GENERIC_WAV.is_file():
        return  # not on Windows / stripped media folder
    with contextlib.closing(wave.open(str(sound.GENERIC_WAV), "rb")) as w:
        assert w.getnframes() / w.getframerate() < 2.5


def test_source_path_falls_back_to_the_requested_default():
    fake = Path("does-not-exist.wav")
    assert sound.source_path("", sound.GENERIC_WAV) == sound.GENERIC_WAV
    assert sound.source_path("", None) == sound.DEFAULT_WAV
    # a configured file still wins over either default
    assert sound.source_path(str(fake), sound.GENERIC_WAV) == sound.GENERIC_WAV


def test_a_configured_generic_sound_overrides_the_default(tmp_path):
    import wave

    custom = tmp_path / "custom.wav"
    with wave.open(str(custom), "wb") as w:
        w.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        w.writeframes(b"\x00\x01" * 100)
    assert sound.source_path(str(custom), sound.GENERIC_WAV) == custom


def test_alerts_config_carries_a_separate_generic_sound():
    assert AlertsConfig().generic_sound == ""
    assert AlertsConfig(generic_sound="x.wav").generic_sound == "x.wav"


def test_wav_cache_key_separates_the_kinds_when_both_are_unconfigured():
    """The regression this feature is most likely to lose silently: with
    `sound` and `generic_sound` both empty the configured paths are the SAME
    empty string, so a cache keyed on path+volume alone hands the generic
    alert whichever file was scaled first - the Valdo woop."""
    volume = 0.5
    alerts = AlertsConfig()
    keys = {
        (generic, alerts.generic_sound if generic else alerts.sound, round(volume, 3))
        for generic in (False, True)
    }
    assert len(keys) == 2

    paths = {
        sound.alert_wav_path("", volume, sound.GENERIC_WAV if generic else None)
        for generic in (False, True)
    }
    if None not in paths:  # both defaults present (Windows)
        assert len(paths) == 2
