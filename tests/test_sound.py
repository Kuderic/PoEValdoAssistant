"""Volume scaling of the alert WAV (sniper/sound.py).

winsound has no volume control, so the WAV is rescaled before playback; a
bug here means either silence or blown-out noise on every alert.
"""

import array
import io
import wave

import pytest

from sniper.sound import load_wav, scale_wav


def make_wav(samples, sampwidth=2, nchannels=1, framerate=44100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(array.array("h" if sampwidth == 2 else "B", samples).tobytes())
    return buf.getvalue()


def read_samples(data: bytes, sampwidth=2):
    with wave.open(io.BytesIO(data), "rb") as w:
        frames = w.readframes(w.getnframes())
    out = array.array("h" if sampwidth == 2 else "B")
    out.frombytes(frames)
    return list(out)


def test_halves_16bit_amplitudes():
    wav = make_wav([1000, -1000, 32767, -32768, 0])
    assert read_samples(scale_wav(wav, 0.5)) == [500, -500, 16383, -16384, 0]


def test_volume_one_returns_input_untouched():
    wav = make_wav([1000, -1000])
    assert scale_wav(wav, 1.0) is wav


def test_zero_volume_is_silence():
    assert read_samples(scale_wav(make_wav([9000, -9000]), 0.0)) == [0, 0]


def test_volume_is_clamped_not_amplified():
    """>1 must not blow out the waveform into clipping distortion."""
    wav = make_wav([30000, -30000])
    assert read_samples(scale_wav(wav, 5.0)) == [30000, -30000]
    assert read_samples(scale_wav(wav, -1.0)) == [0, 0]


def test_8bit_scales_around_the_unsigned_midpoint():
    """8-bit PCM is unsigned and centred on 128, not 0."""
    wav = make_wav([228, 28, 128], sampwidth=1)
    assert read_samples(scale_wav(wav, 0.5), sampwidth=1) == [178, 78, 128]


def test_preserves_wav_format():
    wav = make_wav([100] * 64, nchannels=1, framerate=22050)
    with wave.open(io.BytesIO(scale_wav(wav, 0.5)), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)


def test_garbage_input_is_returned_unchanged_not_raised():
    """A malformed sound file must never break alerting."""
    junk = b"definitely not a wav"
    assert scale_wav(junk, 0.5) == junk


def test_load_wav_missing_path_falls_back(tmp_path):
    """A configured-but-missing file must not lose the alert sound."""
    missing = str(tmp_path / "nope.wav")
    from sniper import sound

    result = load_wav(missing)
    # falls through to the Windows default; None only when that is absent too
    assert result is None or result.startswith(b"RIFF")
    assert not sound.DEFAULT_WAV.is_file() or result is not None


@pytest.mark.skipif(not load_wav(), reason="no system alert WAV on this machine")
def test_scales_the_real_system_alert_wav():
    """End-to-end on the actual sound the app plays by default."""
    raw = load_wav()
    quiet = scale_wav(raw, 0.5)
    assert quiet != raw
    loud_peak = max(abs(s) for s in read_samples(raw))
    quiet_peak = max(abs(s) for s in read_samples(quiet))
    assert quiet_peak == pytest.approx(loud_peak / 2, rel=0.02)
