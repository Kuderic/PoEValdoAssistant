"""Volume-scaled alert audio.

`winsound.PlaySound` has no volume control, and the system alias sounds
("SystemExclamation") cannot be scaled at all. So the alert WAV is read,
its PCM samples are scaled in place, and the result is played from memory
(`SND_MEMORY`). Scaling is pure, cached by the caller, and never runs on
the UI thread.

Anything we cannot safely rescale (compressed or exotic sample widths) is
returned untouched rather than corrupted into noise.
"""

from __future__ import annotations

import array
import io
import sys
import wave
from pathlib import Path

# What the SystemExclamation alias plays. Used when no custom sound is
# configured, so the default alert is volume-controllable too.
DEFAULT_WAV = Path(r"C:\Windows\Media\Windows Exclamation.wav")


def scale_wav(data: bytes, volume: float) -> bytes:
    """Return the PCM WAV image `data` with amplitudes scaled by `volume`
    (clamped to 0.0-1.0). Returns `data` unchanged when it is not a plain
    PCM WAV we know how to rescale."""
    volume = max(0.0, min(1.0, volume))
    if volume == 1.0:
        return data
    try:
        with wave.open(io.BytesIO(data), "rb") as src:
            params = src.getparams()
            frames = src.readframes(params.nframes)
        if params.comptype != "NONE":
            return data
        if params.sampwidth == 2:  # signed 16-bit: the usual case
            samples = array.array("h")
            samples.frombytes(frames)
            if sys.byteorder == "big":
                samples.byteswap()  # WAV sample data is little-endian
            for i, s in enumerate(samples):
                samples[i] = max(-32768, min(32767, int(s * volume)))
            if sys.byteorder == "big":
                samples.byteswap()
            frames = samples.tobytes()
        elif params.sampwidth == 1:  # unsigned 8-bit, centred on 128
            samples = array.array("B")
            samples.frombytes(frames)
            for i, s in enumerate(samples):
                samples[i] = max(0, min(255, int((s - 128) * volume) + 128))
            frames = samples.tobytes()
        else:
            return data
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames)
        return out.getvalue()
    except (wave.Error, ValueError, EOFError, MemoryError):
        return data  # never let a malformed sound file break alerting


def load_wav(path: str = "") -> bytes | None:
    """WAV bytes for the alert: the configured file if set and readable,
    otherwise the Windows default alert sound. None when neither exists -
    the caller then falls back to the system alias, which plays at full
    volume because an alias cannot be rescaled."""
    candidates = [Path(path)] if path else []
    candidates.append(DEFAULT_WAV)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_bytes()
        except OSError:
            continue
    return None
