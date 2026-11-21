"""Volume-scaled alert audio.

`winsound.PlaySound` has no volume control, and the system alias sounds
("SystemExclamation") cannot be scaled at all. So the alert WAV is read and
its PCM samples scaled, then written to a cached temp FILE and played with
`SND_FILENAME | SND_ASYNC`.

The file matters: winsound refuses `SND_MEMORY | SND_ASYNC` outright
("Cannot play asynchronously from memory"), and playback must stay async -
a synchronous call blocks its thread for the whole sound and cannot be
interrupted by the next alert.

Anything we cannot safely rescale (compressed or exotic sample widths) is
returned untouched rather than corrupted into noise.
"""

from __future__ import annotations

import array
import hashlib
import io
import sys
import tempfile
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# What the SystemExclamation alias plays. Used when no custom sound is
# configured, so the default alert is volume-controllable too.
DEFAULT_WAV = Path(r"C:\Windows\Media\Windows Exclamation.wav")
# Non-Valdo live-search hits are rare and hand-picked, so they get their own
# sound - the whole point is telling them apart from the routine Valdo woop
# without looking at the screen. Deliberately short (1.6s): alerts expire in
# seconds and the next one cancels this one, so a 5s alarm would still be
# playing after the travel it announced.
GENERIC_WAV = Path(r"C:\Windows\Media\tada.wav")


@dataclass(frozen=True)
class SoundCommand:
    """kind: "valdo" | "generic" plays that alert's WAV; "stop" silences
    whatever is playing. `loop` repeats until the next command."""

    kind: str
    loop: bool = False


class SoundWorker:
    """Runs PlaySound calls on ONE background thread, newest command wins.

    PlaySound blocks its caller while it loads audio (~100-500ms when it
    interrupts a playing sound), so it must stay off the UI thread. One
    worker rather than a thread per alert keeps the calls ordered: a later
    sound (or a stop) can never be overtaken by an earlier one.

    Pending commands coalesce: a burst only plays its last one. Nothing is
    lost by that - each PlaySound cancels the previous anyway.
    """

    def __init__(self, backend: Callable[[SoundCommand], None], on_error=None):
        self._backend = backend
        self._on_error = on_error
        self._cond = threading.Condition()
        self._pending: SoundCommand | None = None
        self._busy = False
        threading.Thread(target=self._run, name="alert-sound", daemon=True).start()

    def submit(self, command: SoundCommand) -> None:
        with self._cond:
            self._pending = command
            self._cond.notify()

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until nothing is pending or running (tests)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending is None and not self._busy, timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending is not None)
                command, self._pending = self._pending, None
                self._busy = True
            try:
                self._backend(command)
            except Exception as e:  # a silent alert is a missed snipe
                if self._on_error is not None:
                    self._on_error(e)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()


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


def source_path(path: str = "", default: Path | None = None) -> Path | None:
    """The WAV to alert with: the configured file if set and readable,
    otherwise `default` (the Windows alert sound unless overridden). None
    when neither exists - the caller then falls back to the system alias,
    which plays at full volume because an alias cannot be rescaled."""
    candidates = [Path(path)] if path else []
    candidates.append(DEFAULT_WAV if default is None else default)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_wav(path: str = "", default: Path | None = None) -> bytes | None:
    """Raw bytes of the alert WAV (see source_path)."""
    src = source_path(path, default)
    try:
        return None if src is None else src.read_bytes()
    except OSError:
        return None


def alert_wav_path(path: str = "", volume: float = 1.0, default: Path | None = None) -> str | None:
    """Path to a WAV at the requested volume, ready for SND_FILENAME.

    Full volume plays the source file directly; anything quieter is scaled
    once into a temp file keyed by source identity + volume, so repeated
    alerts re-use it. Returns None when there is no source WAV at all.
    """
    src = source_path(path, default)
    if src is None:
        return None
    volume = max(0.0, min(1.0, volume))
    if volume >= 1.0:
        return str(src)
    try:
        stat = src.stat()
        # mtime and size in the key so editing the sound file invalidates it
        digest = hashlib.sha1(
            f"{src.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{volume:.3f}".encode(),
            usedforsecurity=False,
        ).hexdigest()[:16]
        cached = Path(tempfile.gettempdir()) / f"valdo-alert-{digest}.wav"
        if not cached.is_file():
            scaled = scale_wav(src.read_bytes(), volume)
            # write via a temp name then replace, so a half-written file is
            # never handed to the player
            partial = cached.with_suffix(".part")
            partial.write_bytes(scaled)
            partial.replace(cached)
        return str(cached)
    except OSError:
        return str(src)  # cannot cache: better loud than silent
