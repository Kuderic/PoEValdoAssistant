"""A jackpot's alarm plays twice (a super jackpot's three times), and the
banner is not a button.

The alarm used to loop until the banner was clicked. Now its repeats are
baked into the WAV, and a routine alert arriving while it plays does not cut
it off.
"""

from __future__ import annotations

import io
import time
import tkinter as tk
import wave

import pytest
from conftest import make_config

from sniper import overlay as overlay_mod
from sniper import sound
from sniper.bus import AlertsChanged, Bus
from sniper.overlay import Overlay


@pytest.fixture
def ov():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    o = Overlay(root, Bus(), make_config())
    o.played = []
    o._play_sound = lambda kind="valdo": o.played.append(kind)
    yield o
    root.destroy()


def view(lid: str, *, jackpot=False, super_jackpot=False):
    now = time.monotonic()
    return overlay_mod.AlertView(
        listing_id=lid,
        key="Foil Nimis",
        item_name="",
        amount=80,
        currency="divine",
        net_div=80,
        required_net_div=1,
        difficulty=55,
        margin=0.5,
        seller="",
        mismatch=False,
        warn_labels=(),
        special_warnings=(),
        pairings=(),
        mods=(),
        reference_amount=161,
        reference_currency="divine",
        reference_source="trade",
        index_lag_ms=900.0,
        created_monotonic=now,
        expires_at_monotonic=now + 10,
        jackpot=jackpot or super_jackpot,
        super_jackpot=super_jackpot,
    )


def arrive(o: Overlay, alerts, new) -> None:
    o._bus.put(AlertsChanged(tuple(alerts), new_alert=True, new_view=new))
    o._drain_once()


def test_a_jackpot_plays_its_alarm_once_and_a_routine_alert_does_not_cut_it(ov):
    jp, meh = view("jp", jackpot=True), view("meh")
    arrive(ov, [jp], jp)
    arrive(ov, [jp, meh], meh)  # a second later: stays quiet
    assert ov.played == ["jackpot"]
    ov._jp_sound_until = 0.0  # the alarm has finished
    meh2 = view("meh2")
    arrive(ov, [jp, meh, meh2], meh2)
    assert ov.played == ["jackpot", "valdo"]


def test_a_super_jackpot_plays_over_a_jackpot_alarm(ov):
    jp, sup = view("jp", jackpot=True), view("sup", super_jackpot=True)
    arrive(ov, [jp], jp)
    arrive(ov, [sup, jp], sup)
    assert ov.played == ["jackpot", "super"]


def tone(seconds: float, rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return out.getvalue()


def test_repeat_wav_plays_the_sound_n_times_back_to_back():
    one = tone(0.5)
    with wave.open(io.BytesIO(sound.repeat_wav(one, 3))) as w:
        assert w.getnframes() == 3 * 4000
    assert sound.repeat_wav(one, 1) is one
    assert sound.repeat_wav(b"not a wav", 3) == b"not a wav"  # plays once, not never


def test_jackpot_wavs_hold_two_and_three_plays(tmp_path):
    src = tmp_path / "alarm.wav"
    src.write_bytes(tone(0.5))
    for plays, volume in ((2, 1.0), (3, 0.5)):
        path = sound.alert_wav_path(str(src), volume, repeat=plays)
        assert sound.wav_seconds(path) == pytest.approx(0.5 * plays)
    assert (sound.JACKPOT_PLAYS, sound.SUPER_JACKPOT_PLAYS) == (2, 3)


def test_routine_alerts_stay_quiet_for_the_whole_repeated_alarm(ov, monkeypatch):
    monkeypatch.setattr(overlay_mod, "wav_seconds", lambda path: 7.5)
    jp = view("jp", jackpot=True)
    arrive(ov, [jp], jp)
    assert ov._jp_sound_until - time.monotonic() == pytest.approx(7.5, abs=0.5)


def test_the_banner_has_no_silence_hint_and_no_click(ov):
    jp = view("jp", jackpot=True)
    arrive(ov, [jp], jp)
    assert "silence" not in ov._jp_left.cget("text")
    for widget in (ov._jackpot_banner, ov._jp_left, ov._jp_right):
        assert "<Button-1>" not in widget.bind()
