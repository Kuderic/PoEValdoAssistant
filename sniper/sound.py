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
# Jackpot (big net AND big yield): an alarm, played JACKPOT_PLAYS times
# (once, 3.75 s). A routine alert arriving meanwhile does not cut it off.
JACKPOT_WAV = Path(r"C:\Windows\Media\Alarm02.wav")
JACKPOT_PLAYS = 1
# Super jackpot (the tier above): a different alarm, so the two tiers can be
# told apart by ear (once, 4.0 s).
SUPER_JACKPOT_WAV = Path(r"C:\Windows\Media\Alarm03.wav")
SUPER_JACKPOT_PLAYS = 1
# Super mega jackpot (the top tier): its own alarm again, in the Windows style.
MEGA_JACKPOT_WAV = Path(r"C:\Windows\Media\Alarm04.wav")
MEGA_JACKPOT_PLAYS = 1


# Built-in SHORT alert sounds (alerts.short_sounds, on by default): synthesised
# blips that hit full volume within a few ms and are over in 0.1-0.3s. The
# Windows sounds above run 1.4-4s, and a sound still playing quietly invites
# waiting for it to finish before pressing the hotkey. Each kind keeps its
# own shape so they are still told apart by ear:
#   valdo    one blip
#   generic  two rising notes
#   jackpot  two quick beeps and a higher one
#   super    a fast four-note run upward
# (frequency Hz, seconds) per note; 0 Hz is a silent gap.
SHORT_NOTES: dict[str, tuple[tuple[float, float], ...]] = {
    "valdo": ((1760, 0.11),),
    "generic": ((1175, 0.07), (1760, 0.10)),
}
# The soft jingle (alerts.sound_style: jingle): the routine kinds as
# overlapping bell notes - pure sines, a gentler attack, a long soft tail.
# Still loud within ~6ms and over in under half a second, so it does not
# bring back the wait-for-it problem; just less sharp than the blips.
# (frequency Hz, start s, ring s) per note.
JINGLE_NOTES: dict[str, tuple[tuple[float, float, float], ...]] = {
    "valdo": ((784, 0.0, 0.30), (1175, 0.07, 0.28)),
    "generic": ((1047, 0.0, 0.24), (1319, 0.06, 0.24), (1568, 0.12, 0.28)),
}
JINGLE_ATTACK_S = 0.006
# Jackpot and super jackpot FANFARES (the Short style's; Soft jingle has
# its own chime version below): one play, never a repeated beep. A fast
# rising run that rings out into a chord, with a touch of vibrato and a
# detuned shimmer the routine sounds do not have, so it cannot be mistaken
# for an ordinary alert. The first note is the loudest at its start: full
# level within ~2ms.
# (frequency Hz, start s, ring s, amplitude) per note.
FANFARE_NOTES: dict[str, tuple[tuple[float, float, float, float], ...]] = {
    "jackpot": (
        (1047, 0.00, 0.45, 1.0),
        (1319, 0.06, 0.45, 0.7),
        (1568, 0.12, 0.55, 0.7),
        (2093, 0.18, 0.80, 0.8),
    ),
    "super": (
        (784, 0.00, 0.40, 1.0),
        (1047, 0.05, 0.40, 0.65),
        (1319, 0.10, 0.45, 0.65),
        (1568, 0.15, 0.55, 0.65),
        (2093, 0.20, 0.90, 0.75),
        (2637, 0.20, 0.95, 0.45),
    ),
    # the top tier: a faster three-octave run into a bigger, higher chord
    "mega": (
        (523, 0.00, 0.30, 1.0),
        (659, 0.04, 0.30, 0.6),
        (784, 0.08, 0.30, 0.6),
        (1047, 0.12, 0.35, 0.65),
        (1319, 0.16, 0.40, 0.65),
        (1568, 0.20, 0.45, 0.65),
        (2093, 0.26, 1.00, 0.75),
        (2637, 0.26, 1.00, 0.5),
        (3136, 0.26, 1.00, 0.4),
    ),
}
FANFARE_ATTACK_S = 0.004
FANFARE_RELEASE_S = 0.03
# The Soft jingle style's jackpot fanfares: the same rising run into a
# chord, rung as bells (near-pure sines, the jingle's softer attack and long
# tail, no vibrato or shimmer), so a jackpot still stands out from the
# routine chimes without the brightness. Loud within JINGLE_ATTACK_S.
# (frequency Hz, start s, ring s, amplitude) per note.
CHIME_FANFARE_NOTES: dict[str, tuple[tuple[float, float, float, float], ...]] = {
    "jackpot": (
        (1047, 0.00, 0.80, 1.0),
        (1319, 0.07, 0.80, 0.8),
        (1568, 0.14, 0.90, 0.8),
        (2093, 0.21, 0.95, 0.9),
    ),
    "super": (
        (784, 0.00, 0.80, 1.0),
        (1047, 0.06, 0.80, 0.75),
        (1319, 0.12, 0.85, 0.75),
        (1568, 0.18, 0.90, 0.75),
        (2093, 0.24, 1.00, 0.85),
        (2637, 0.24, 1.00, 0.5),
    ),
    "mega": (
        (523, 0.00, 0.60, 1.0),
        (659, 0.05, 0.60, 0.7),
        (784, 0.10, 0.65, 0.7),
        (1047, 0.15, 0.70, 0.75),
        (1319, 0.20, 0.75, 0.75),
        (1568, 0.25, 0.80, 0.75),
        (2093, 0.30, 0.95, 0.85),
        (2637, 0.30, 0.95, 0.55),
        (3136, 0.30, 0.95, 0.4),
    ),
}
# alerts.sound_style: what an empty sound setting plays
SOUND_STYLES = ("short", "jingle", "windows")
SYNTH_RATE = 44100
SYNTH_ATTACK_S = 0.003  # fast, but not a click
SYNTH_RELEASE_S = 0.006  # fade the tail to zero: no click at the cut
SYNTH_PEAK = 0.95  # never louder than this, of full scale
# How loud each one is: RMS of its loudest 100ms, matched to the Windows
# sound it replaces (Exclamation 0.043, tada 0.036, Alarm02 0.130, Alarm03
# 0.090 - those files are mastered QUIET) and ~3dB over the routine ones,
# since a 0.1s sound reads quieter than a long one at the same level. So the
# volume setting means what it meant before; normalising to full scale
# instead made them ~18dB louder.
SHORT_LEVELS = {"valdo": 0.06, "generic": 0.06, "jackpot": 0.13, "super": 0.13, "mega": 0.13}
LEVEL_WINDOW_S = 0.1
# bump when synth_wav itself changes, so cached files are rebuilt
SYNTH_VERSION = 3


def synth_wav(notes, level: float = 0.06, rate: int = SYNTH_RATE) -> bytes:
    """A mono 16-bit PCM WAV of `notes` ((frequency, seconds), ...): each a
    tone with a touch of its octave for brightness, a near-instant attack
    and a decay, so it reads as a crisp blip rather than a beep."""
    import math

    values: list[float] = []
    for freq, seconds in notes:
        n = max(1, int(rate * seconds))
        if freq <= 0:
            values.extend([0.0] * n)
            continue
        attack = max(1, int(rate * SYNTH_ATTACK_S))
        release = max(1, int(rate * SYNTH_RELEASE_S))
        tau = n / 2  # still well up at the end: a short sound needs its energy
        for i in range(n):
            t = i / rate
            env = min(1.0, i / attack) * math.exp(-i / tau) * min(1.0, (n - i) / release)
            tone = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
            values.append(env * tone)
    return _render(values, level, rate)


def synth_jingle(notes, level: float = 0.06, rate: int = SYNTH_RATE) -> bytes:
    """A mono 16-bit PCM WAV of overlapping chime notes ((frequency, start
    s, ring s), ...): near-pure sines with a soft attack and a long bell
    decay, each still ringing as the next starts - a soft jingle rather
    than a blip, but loud within JINGLE_ATTACK_S all the same."""
    import math

    length = max(int(rate * start) + max(1, int(rate * ring)) for _, start, ring in notes)
    values = [0.0] * length
    attack = max(1, int(rate * JINGLE_ATTACK_S))
    release = max(1, int(rate * SYNTH_RELEASE_S))
    for freq, start, ring in notes:
        first, n = int(rate * start), max(1, int(rate * ring))
        tau = n / 4  # a bell: bright strike, long soft tail
        for i in range(n):
            t = i / rate
            env = min(1.0, i / attack) * math.exp(-i / tau) * min(1.0, (n - i) / release)
            tone = math.sin(2 * math.pi * freq * t) + 0.08 * math.sin(4 * math.pi * freq * t)
            values[first + i] += env * tone
    return _render(values, level, rate)


def synth_fanfare(notes, level: float = 0.13, rate: int = SYNTH_RATE) -> bytes:
    """A mono 16-bit PCM WAV of a jackpot fanfare ((frequency, start s,
    ring s, amplitude), ...): bright tones (a touch of octave, a detuned
    shimmer, vibrato easing in) that overlap into a ringing chord."""
    import math

    length = max(int(rate * start) + max(1, int(rate * ring)) for _, start, ring, _a in notes)
    values = [0.0] * length
    attack = max(1, int(rate * FANFARE_ATTACK_S))
    release = max(1, int(rate * FANFARE_RELEASE_S))
    for freq, start, ring, amp in notes:
        first, n = int(rate * start), max(1, int(rate * ring))
        tau = n / 3
        for i in range(n):
            t = i / rate
            env = min(1.0, i / attack) * math.exp(-i / tau) * min(1.0, (n - i) / release)
            vibrato = 1 + 0.004 * math.sin(2 * math.pi * 5.5 * t) * min(1.0, t / 0.15)
            phase = 2 * math.pi * freq * vibrato * t
            tone = (
                math.sin(phase)
                + 0.25 * math.sin(2 * phase)
                + 0.5 * math.sin(2 * math.pi * freq * 1.004 * t)
            )
            values[first + i] += amp * env * tone
    return _render(values, level, rate)


def synth_chime_fanfare(notes, level: float = 0.13, rate: int = SYNTH_RATE) -> bytes:
    """The Soft jingle's jackpot fanfare ((frequency, start s, ring s,
    amplitude), ...): synth_jingle's bells, overlapping into a chord."""
    import math

    length = max(int(rate * start) + max(1, int(rate * ring)) for _, start, ring, _a in notes)
    values = [0.0] * length
    attack = max(1, int(rate * JINGLE_ATTACK_S))
    release = max(1, int(rate * FANFARE_RELEASE_S))
    for freq, start, ring, amp in notes:
        first, n = int(rate * start), max(1, int(rate * ring))
        tau = n / 4  # bright strike, long soft tail
        for i in range(n):
            t = i / rate
            env = min(1.0, i / attack) * math.exp(-i / tau) * min(1.0, (n - i) / release)
            tone = math.sin(2 * math.pi * freq * t) + 0.08 * math.sin(4 * math.pi * freq * t)
            values[first + i] += amp * env * tone
    return _render(values, level, rate)


def _render(values: list[float], level: float, rate: int) -> bytes:
    """`values` as a mono 16-bit WAV, scaled so its loudest 100ms has RMS
    `level` (see SHORT_LEVELS) and never past SYNTH_PEAK."""
    import math

    win = max(1, int(rate * LEVEL_WINDOW_S))
    loudest = max(
        (
            math.sqrt(sum(v * v for v in values[i : i + win]) / win)
            for i in range(0, max(1, len(values) - win + 1), max(1, win // 4))
        ),
        default=0.0,
    )
    top = max((abs(v) for v in values), default=0.0)
    gain = min(level / loudest, SYNTH_PEAK / top) if loudest and top else 0.0
    samples = array.array("h", (int(v * gain * 32767) for v in values))
    if sys.byteorder == "big":
        samples.byteswap()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return out.getvalue()


def builtin_wav(kind: str, style: str = "short") -> Path | None:
    """The built-in sound for an alert kind in a style ("short" blips or the
    soft "jingle"; jackpots get a fanfare in each, bright or chimed), as a file
    (PlaySound plays files asynchronously, not memory). Written once to the
    temp folder under a name hashed from its recipe, so changing the recipe
    makes a new file. None for the "windows" style, an unknown kind, or when
    it cannot be written - the caller then falls back to the Windows
    sound."""
    if style not in ("short", "jingle"):
        return None
    if kind in FANFARE_NOTES and style == "jingle":
        style = "chime"
        notes = CHIME_FANFARE_NOTES[kind]
        make = synth_chime_fanfare
        shape = (JINGLE_ATTACK_S, FANFARE_RELEASE_S)
    elif kind in FANFARE_NOTES:
        style = "fanfare"
        notes = FANFARE_NOTES[kind]
        make = synth_fanfare
        shape = (FANFARE_ATTACK_S, FANFARE_RELEASE_S)
    elif style == "jingle":
        notes = JINGLE_NOTES.get(kind)
        make = synth_jingle
        shape = (JINGLE_ATTACK_S,)
    elif style == "short":
        notes = SHORT_NOTES.get(kind)
        make = synth_wav
        shape = (SYNTH_ATTACK_S,)
    if notes is None:
        return None
    level = SHORT_LEVELS.get(kind, 0.06)
    recipe = (
        style,
        notes,
        level,
        SYNTH_RATE,
        *shape,
        SYNTH_RELEASE_S,
        SYNTH_PEAK,
        SYNTH_VERSION,
    )
    digest = hashlib.sha1(repr(recipe).encode(), usedforsecurity=False).hexdigest()
    path = Path(tempfile.gettempdir()) / f"valdo-{style}-{kind}-{digest[:12]}.wav"
    try:
        if not path.is_file():
            partial = path.with_suffix(".part")
            partial.write_bytes(make(notes, level))
            partial.replace(path)
    except OSError:
        return None
    return path


def short_wav(kind: str) -> Path | None:
    """The built-in short blip for an alert kind (builtin_wav)."""
    return builtin_wav(kind, "short")


@dataclass(frozen=True)
class SoundCommand:
    """kind: "valdo" | "generic" | "jackpot" | "super" plays that alert's WAV; "stop"
    silences whatever is playing. A jackpot's repeats are baked into its
    WAV (repeat_wav), so every command is one async play."""

    kind: str


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


def repeat_wav(data: bytes, times: int) -> bytes:
    """`data` with its audio played `times` times back to back, as one WAV.
    One async PlaySound of it then plays N times and can still be cut off
    by the next command, which a play-N-times loop could not. Returns
    `data` unchanged when it is not a WAV we can read."""
    if times <= 1:
        return data
    try:
        with wave.open(io.BytesIO(data), "rb") as src:
            params = src.getparams()
            frames = src.readframes(params.nframes)
        out = io.BytesIO()
        with wave.open(out, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames * times)
        return out.getvalue()
    except (wave.Error, ValueError, EOFError, MemoryError):
        return data  # plays once rather than not at all


def wav_seconds(path: str | None) -> float | None:
    """Playing time of a WAV file, or None if it cannot be read."""
    if path is None:
        return None
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error, EOFError, ZeroDivisionError):
        return None


def alert_wav_path(
    path: str = "", volume: float = 1.0, default: Path | None = None, repeat: int = 1
) -> str | None:
    """Path to a WAV at the requested volume, its audio `repeat` times over,
    ready for SND_FILENAME.

    Full volume played once is the source file itself; anything else is
    built once into a temp file keyed by source identity + volume + repeat,
    so later alerts re-use it. Returns None when there is no source WAV.
    """
    src = source_path(path, default)
    if src is None:
        return None
    volume = max(0.0, min(1.0, volume))
    repeat = max(1, repeat)
    if volume >= 1.0 and repeat == 1:
        return str(src)
    try:
        stat = src.stat()
        # mtime and size in the key so editing the sound file invalidates it
        digest = hashlib.sha1(
            f"{src.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{volume:.3f}|x{repeat}".encode(),
            usedforsecurity=False,
        ).hexdigest()[:16]
        cached = Path(tempfile.gettempdir()) / f"valdo-alert-{digest}.wav"
        if not cached.is_file():
            scaled = repeat_wav(scale_wav(src.read_bytes(), volume), repeat)
            # write via a temp name then replace, so a half-written file is
            # never handed to the player
            partial = cached.with_suffix(".part")
            partial.write_bytes(scaled)
            partial.replace(cached)
        return str(cached)
    except OSError:
        return str(src)  # cannot cache: better loud than silent
