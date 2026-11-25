"""Always-on-top tkinter overlay. Runs on the MAIN thread only; all state
arrives as immutable events from the bus. Each publish wakes the drain via
a <<BusWake>> Tk event, sent from a small waker thread so the asyncio thread
never blocks on Tcl (see _wake_loop); a DRAIN_MS root.after poll remains as
safety net. The decision -> render delay is event-driven, not
poll-quantized.

Latency contract: sound + render happen inside the same drain pass that
delivers AlertsChanged(new_alert=True); the frame->UI delta is logged as
`alert_shown` for the <100 ms acceptance check.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import font as tkfont

from sniper import __version__, browser, paths
from sniper import icon as app_icon
from sniper.alerts import AlertView, alarm_kind, jackpots_to_ring
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    GameStatus,
    ListingSeen,
    PriceStatus,
    PricingHealth,
    RewardPrices,
    TabsChanged,
    Traveled,
    UpdateStatus,
    WarmupStatus,
)
from sniper.config import Config
from sniper.logging_setup import event
from sniper.margin import net_yield
from sniper.server import EXPECTED_USERSCRIPT_VERSION, INSTALL_PATH
from sniper.sound import (
    GENERIC_WAV,
    JACKPOT_WAV,
    SUPER_JACKPOT_WAV,
    SoundCommand,
    SoundWorker,
    alert_wav_path,
)

try:
    import winsound
except ImportError:  # non-Windows dev machine
    winsound = None

DRAIN_MS = 50  # fallback poll only; <<BusWake>> makes the drain event-driven
TICK_MS = 100
FEED_MIN_HEIGHT = 150  # px floor for the scrollable history area
SCROLLBAR_HIDE_MS = 900  # overlay scrollbar lingers this long after scrolling

# The headline numbers, left to right. The price caption is rewritten per
# listing to name the currency (a hard requirement: price AND currency must
# be prominent), the rest are fixed.
STAT_COLUMNS = (
    ("price", "PRICE · DIVINE"),
    ("profit", "NET"),
    ("ratio", "YIELD"),
    ("difficulty", "DIFFICULTY"),
)
# yield is the number the alert decision and the hotkey's pick both turn
# on, so its cell is tinted where the other three are plain.
TINTED_STAT = "ratio"
# Difficulty colouring, tiered off the reference difficulty of 100 that the
# threshold is calibrated for: at or under it is an easy map, 3x it is brutal.
DIFF_WARN, DIFF_BAD = 100.0, 300.0
# Head start (ms) other snipers had on a listing before it reached us. The
# cutoffs come from this app's own logs: travels clicked within ~2s of
# detection lost ~26% of the time, 4-10s lost ~52%.
LAG_OK_MS, LAG_BAD_MS = 2000.0, 5000.0
# A listing cannot reach us before the trade site indexed it, so a negative
# lag is never real: it means detected_at and indexed_at came off clocks that
# disagree. GGG truncates indexed_at to whole seconds, so up to ~1s negative
# is just rounding; past that it is genuine skew.
#
# This matters far beyond the ugly number. While the clocks disagree EVERY
# latency reading is wrong by the offset, in whichever direction the drift
# runs - and a PC clock running fast would make stale listings read as fresh,
# which is the one error that costs travels. Measured 2026-08-14: an ~19s
# Windows clock drift ran for 8 hours and rendered as "latency -18000ms" in
# GREEN, i.e. "we saw this the instant it listed, go race it".
CLOCK_SKEW_MS = -1000.0
# Hover help: what each part of the overlay means, as (title, body). Shown
# the instant the pointer arrives (a delay read as a sluggish UI); parts
# whose numbers can be worked through for the listing on screen add a third,
# live line (see the _*_help methods).
HELP_WRAP = 340
HELP = {
    "tabs": (
        "Live-search tabs",
        "Chrome tabs on the trade site running the Valdo Sniper script. Each one is a "
        "live search: the trade site pushes new listings into the tab and the script "
        "forwards them here the same instant. Green = connected. “stale” = a tab "
        "stopped checking in (reload it). “reload Mageblood” = that tab's listings "
        "now arrive seconds later than when it was freshly loaded - a busy tab slows "
        "down as its results pile up over hours, and a reload makes it fast again. "
        "Red = none connected; click to install the script.",
    ),
    "game": (
        "Game client",
        "Whether Path of Exile is running. The teleport hotkey only works while it "
        "is, so a stray key press can never travel you.",
    ),
    "prices": (
        "Reward prices",
        "Reward values are the average of the cheapest instant-buyout listings on "
        "the trade site. Green = all priced. “Est.” = some rewards fell back to "
        "poe.ninja's median, which can be well off. RATE LIMITED = the trade site "
        "asked us to slow down; prices resume by themselves.",
    ),
    "mute": ("Sound", "Mute or unmute alert sounds, including the jackpot alarm."),
    "settings": (
        "Settings",
        "Alert bars, hotkey, how hard each mod is, deadly pairings and reward prices.",
    ),
    "watching": (
        "Watching",
        "The reward of each Valdo live search you have open, with what it sells for "
        "right now and how long ago that price was fetched from the trade site. Prices "
        "refresh in the background; NET and YIELD are worked out from them.",
    ),
    "latency": (
        "Listing delay",
        "How long this listing had already been up when your Chrome tab received it: "
        "from the moment the trade site indexed the seller's listing to the moment the "
        "live search delivered it. It is the head start every other sniper had, not "
        "your reaction time. Green under 2s is worth racing, amber 2-5s, red over 5s "
        "is probably already sold.",
    ),
    "price": (
        "Asking price",
        "What the seller wants, in the currency they listed it in. You pay it at "
        "Faustus in their hideout. Turns red when that currency is not the one the "
        "reward is priced in, a common bait (e.g. 80 chaos dressed up as 80 divine).",
    ),
    "net": (
        "Net profit",
        "Divine you expect to keep after buying: what the reward sells for, minus the "
        "asking price, minus the flat running cost per map. With a failure risk "
        "(“NET · EXPECTED”) only the share of maps that succeed counts.",
    ),
    "yield": (
        "Yield",
        "Profit per unit of difficulty: net ÷ difficulty × 100. This is what decides "
        "an alert and which listing the hotkey takes, so an easy map with modest "
        "profit can beat a brutal one with more. Same number as the YIELD column in "
        "the list below.",
    ),
    "difficulty": (
        "Difficulty",
        "How hard and risky the map's mods make it; 100 is a typical map. It starts "
        "from the base of the toughest encounter mod (or the clean-map default), then "
        "every dangerous mod and deadly pairing multiplies it. Each mod's part is "
        "shown beside it below. Amber over 100, red over 300.",
    ),
    "price_panel": (
        "Reward prices",
        "What each reward you are searching sells for, how long ago that price was "
        "fetched and where it came from - so you know before an alert whether its "
        "NET can be trusted.\n"
        "Green: a fresh trade price.\n"
        "Amber: overdue for a re-check, from your last session, a poe.ninja "
        "estimate, or a price you pinned - check it before travelling.\n"
        "Red: no buyable listings found, so this reward cannot be priced.\n"
        "↻ marks a reward being re-fetched right now. “recheck” re-prices that "
        "reward now instead of waiting for its scheduled refresh.",
    ),
    "reward_value": (
        "Reward value",
        "What this map's reward unique sells for, and how long ago that price was "
        "checked on the trade site. NET is worked out from it.",
    ),
    "feed": (
        "Recent listings",
        "Every listing your searches caught, newest first. Bright rows alerted; dim "
        "rows did not clear the yield bar.\n"
        "PRICE  asking price\n"
        "NET  expected profit, in divine\n"
        "YIELD  net per 100 difficulty\n"
        "DIFF  difficulty (100 = a typical map)\n"
        "AGE  how long ago it arrived\n"
        "Hover a row for its mods; click it to teleport.",
    ),
    "pairing": (
        "Deadly pairing",
        "These mods together are far worse than either one alone, so difficulty is "
        "multiplied again on top of their own multipliers.",
    ),
    "stash": (
        "Where it is in the shop",
        "The item's spot in the seller's stash tab, so you can click it in Faustus' "
        "shop the moment the hideout loads. The grid marks the cell; col and row "
        "count from the top left. The trade site does not say how big the tab is, so "
        "a 12×12 tab is assumed unless the position only fits a quad (24×24) tab. "
        "Unknown for listings caught from the page instead of the network.",
    ),
    "risk": (
        "Failure risk",
        "Chance the map cannot be completed with these mods. A failed map pays nothing "
        "back, so NET already counts only the share of maps that succeed.",
    ),
    "ESTIMATED PRICE": (
        "Estimated price",
        "The reward value is poe.ninja's median, not live trade listings, and can be "
        "well off. Worth a look before travelling.",
    ),
    "CACHED PRICE": (
        "Cached price",
        "The reward value is from your last session and has not been re-checked yet this run.",
    ),
    "YOUR PRICE": (
        "Your price",
        "The reward value is one you pinned in ⚙ Settings, not the market's.",
    ),
}
# reference sources as the reward-value line names them
SOURCE_WORDS = {
    "cached": "last session",
    "live": "poe.ninja estimate",
    "stale": "poe.ninja estimate",
    "override": "your price",
    "manual": "config price",
}
# Reference sources that are poe.ninja's per-map median - the documented
# INACCURATE fallback. Anything priced from these is flagged so a rate-limited
# session can never look like a healthy one.
FALLBACK_SOURCES = frozenset({"live", "stale"})
# A price restored from the previous session: real trade data, but not yet
# reconfirmed this run, so it is flagged more gently than the ninja fallback.
CACHED_SOURCE = "cached"
# A price the user pinned in the settings panel. Flagged for the same reason
# as the two above: the profit on screen came from a number the user typed,
# not from the market, and that is worth knowing before travelling on it.
OVERRIDE_SOURCE = "override"


def _fmt(value) -> str:
    return "" if value is None else f"{value:g}"


def _parse(text: str) -> float | None:
    text = text.strip()
    return None if not text else float(text)


def _ago(seconds: float | None) -> str:
    """How long ago a price was calculated, at the coarsest useful unit.
    Empty when unknown (manual overrides and poe.ninja have no fetch time)."""
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.0f}h ago"


def _stash_spot(x: int, y: int) -> tuple[int, str]:
    """(grid side, "bottom right") for a 0-based cell in the seller's tab.

    The trade data gives the cell but not the tab's size, so a normal 12×12
    tab is assumed unless the cell only fits a quad tab (24×24). The words
    split the tab into thirds each way."""
    side = 12 if max(x, y) < 12 else 24
    row = ("top", "middle", "bottom")[min(2, y * 3 // side)]
    col = ("left", "centre", "right")[min(2, x * 3 // side)]
    if row == "middle" and col == "centre":
        return side, "centre"
    return side, f"{row} {col}"


def _display_name(key: str) -> str:
    """Reward keys keep their 'Foil ' prefix internally; the UI drops it."""
    return key.removeprefix("Foil ")


def _num(value: float) -> str:
    """A number for a worked example: one decimal at most, no trailing .0."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _money(amount: float, currency: str) -> str:
    """160d / 40c / 3 exalted: the compact form the feed already uses."""
    short = {"divine": "d", "chaos": "c"}.get(currency)
    return f"{amount:g}{short}" if short else f"{amount:g} {currency}"


def _price_tag(entry, age_s: float | None) -> str:
    """What sits next to a reward's name on the Watching line."""
    if entry.amount is None:
        return "(pricing…)"
    money = _money(entry.amount, entry.currency)
    if entry.override_div is not None:
        return f"{money} (yours)"
    ago = _ago(age_s)
    return f"{money} ({ago})" if ago else money


def _price_status(entry, state: str | None, age_s: float | None, refresh_s: float):
    """One reward's price as the price panel shows it: (price, note, level).

    entry: RewardPrice or None; state: the warm-up state ("done", "working",
    "queued", "pending", "unpriced") or None ("queued" = a recheck the user
    asked for, not started yet). level says how far to trust the price:
    "good" a fresh trade price, "warn" anything to double-check (overdue,
    last session, a poe.ninja estimate, a pinned price), "bad" unpriceable,
    "dim" not priced yet or a fixed config price.
    """
    if entry is None or entry.amount is None:
        note = {
            "working": "fetching…",
            "queued": "queued",
            "pending": "queued",
            "unpriced": "no listings",
        }.get(state or "", "pricing…")
        return ("—", note, "bad" if state == "unpriced" else "dim")
    price = _money(entry.amount, entry.currency)
    ago = _ago(age_s)
    if entry.override_div is not None:
        note, level = "your price", "warn"
    elif entry.source == "manual":
        note, level = "config price", "dim"
    elif entry.source in ("live", "stale"):
        note, level = "poe.ninja estimate", "warn"
    elif entry.source == "cached":
        note, level = "last session" + (f" · {ago}" if ago else ""), "warn"
    elif age_s is not None and age_s > 1.5 * refresh_s:
        # overdue: the scheduled re-price should have replaced it by now
        # (rate limited, or the trade site is struggling)
        note, level = f"{ago} · overdue", "warn"
    else:
        note, level = ago, "good"
    if state == "working":
        price, note = f"↻ {price}", "fetching…"
    elif state == "queued":
        note = "recheck queued"
    return (price, note, level)


def _net_breakdown(view, running_cost: float) -> str:
    """NET worked through for this listing, e.g. '160 worth − 52 price − 1
    running = +107d'. Empty when it cannot be shown exactly: another currency
    is involved (the conversion is not on screen), or the parts do not add up
    to the net shown - a sum that disagrees with the number above it would
    explain nothing."""
    if view.generic or view.net_div is None:
        return ""
    if view.currency != "divine" or view.reference_currency != "divine":
        return ""
    keep = 1.0 - view.fail_chance
    if abs(view.reference_amount * keep - view.amount - running_cost - view.net_div) > 0.5:
        return ""
    worth = f"{_num(view.reference_amount)} worth"
    if view.fail_chance:
        worth += f" × {keep:.0%} succeed"
    return (
        f"{worth} − {_num(view.amount)} price − {_num(running_cost)} running\n"
        f"= {view.net_div:+.1f}d"
    )


def _yield_breakdown(net_div: float | None, difficulty: float, bar: float) -> str:
    ratio = net_yield(net_div, difficulty)
    if ratio is None or net_div is None:
        return ""
    verdict = "alerts" if ratio >= bar else "below the bar"
    return (
        f"{net_div:+.1f} net ÷ {_num(difficulty)} difficulty × 100 = {ratio:+.1f}\n"
        f"alert bar is ≥ {bar:g}, so this {verdict}"
    )


def _difficulty_breakdown(view, base_default: float) -> str:
    """Difficulty rebuilt from the notes beside each mod: max(default, mod
    bases) × every multiplier × every pairing. Empty unless it reproduces the
    score shown (a mod matching two lines notes its multiplier on both)."""
    bases: list[float] = []
    mults: list[float] = []
    try:
        for _text, note, _level in view.mods:
            for part in note.split(" · ") if note else ():
                if part.startswith("base "):
                    bases.append(float(part[5:]))
                elif part.startswith("×"):
                    mults.append(float(part[1:]))
    except ValueError:
        return ""
    mults += [mult for _label, _note, mult in view.pairings]
    base = max([base_default, *bases])
    score = base
    for mult in mults:
        score *= mult
    if abs(score - view.difficulty) > max(0.5, 0.01 * view.difficulty):
        return ""
    start = f"base {_num(base)}" if base in bases else f"clean map {_num(base)}"
    steps = "".join(f" × {m:g}" for m in mults if m != 1)
    return f"{start}{steps} = {view.difficulty:.0f}"


# The stash mini grid's size in pixels (square): 4px per cell on a 12x12 tab
STASH_GRID_PX = 48

# Palette - design A, "Refined". One slate ground, one surface for the alert
# card, 1px hairlines instead of boxes. Colour is kept for meaning only:
# green = good, amber = warning/estimated, red = danger, magenta = jackpot.
BG = "#0f1216"
STRIP = "#0b0e11"  # status strip and mod panel: one step darker than the ground
SURFACE = "#161b21"  # the alert card and runner-up rows
HAIR = "#262e38"  # dividers and card outlines
STASH_BG = "#0b0e11"  # the stash mini grid's tab, one step darker than the card
FG = "#e6eaef"
DIM = "#9aa5b1"
FAINT = "#7d8894"  # diminished: feed rows that did not reach the threshold
ZEBRA = "#12161b"  # every other feed row
GOOD = "#5fd069"
WARN = "#e5b84b"
BAD = "#f06a6a"  # red TEXT on the dark ground
BAD_FILL = "#b23a3a"  # red BACKGROUND under white text (BAD is too light for it)
PRICE = "#ffffff"
BOX = "#2a323c"  # neutral chip fill
YIELD_BG, YIELD_CAP, YIELD_FG = "#1b2a20", "#a9d8b0", "#6fe07b"
PAIR_BG, PAIR_FG = "#4a1c1c", "#ffd2d2"  # deadly-pairing rows
RISK_BG, RISK_FG = "#3d2f12", "#ffe2a8"  # failure-risk rows: amber, not red
# Runner-up alerts are travel targets too, so each one is a raised band that
# reads as a button rather than a dim line of text, and lifts under the
# pointer to confirm what a click would hit.
RUNNER_BG = SURFACE
RUNNER_BG_HOVER = "#1f2730"
GOOD_BRIGHT = "#7ee089"  # travel button, hovered
BTN_FG = "#0b1a0e"  # dark text on the green travel button
BTN_OFF = "#1d242c"  # travel button with nothing to travel to
MODS_BG = STRIP  # darker panel behind the mod list
DOT_NONE = "#3a434e"  # mod-row marker for a mod that does not score
COUNTDOWN_BG = "#222a33"
# Latency badge outline per state, a darker shade of the text colour.
BADGE_EDGE = {GOOD: "#2d4a33", WARN: "#5c4a1e", BAD: "#5a2626"}
# Jackpot (big net AND big yield). Magenta because every other colour is
# taken, and it must not be readable as any of them at a glance.
JACKPOT = "#d65cff"
JACKPOT_SOFT = "#e7a8ff"  # name and numbers on the dark card
JACKPOT_BANNER = "#a634d1"  # banner fill: dark enough for white text
JACKPOT_YIELD_BG = "#2c1838"
JACKPOT_BTN_FG = "#1c0626"
# super jackpot: gold, the tier above magenta
SUPER = "#ffc233"
SUPER_SOFT = "#ffe08a"
SUPER_BANNER = "#8a5c00"  # dark enough for white text
SUPER_YIELD_BG = "#33280f"
SUPER_BTN_FG = "#2a1d00"


@dataclass(frozen=True)
class _Tier:
    """Everything that styles one jackpot tier, so each painter picks the
    tier once instead of branching on two flags."""

    accent: str  # frame, countdown, feed row, button
    soft: str  # name and numbers on the dark card
    banner: str  # banner fill
    yield_bg: str  # tinted yield cell
    cap: str  # caption on the tinted cell
    btn_fg: str  # text on the accent-coloured button
    title: str  # banner text
    feed_note: str


JACKPOT_TIER = _Tier(
    JACKPOT,
    JACKPOT_SOFT,
    JACKPOT_BANNER,
    JACKPOT_YIELD_BG,
    "#d9b8e8",
    JACKPOT_BTN_FG,
    "★  JACKPOT",
    "  ★ JACKPOT",
)
SUPER_TIER = _Tier(
    SUPER,
    SUPER_SOFT,
    SUPER_BANNER,
    SUPER_YIELD_BG,
    "#ecd9a8",
    SUPER_BTN_FG,
    "★★  SUPER JACKPOT",
    "  ★★ SUPER",
)


def _tier(item) -> _Tier | None:
    """The jackpot tier of an AlertView or FeedEntry; None for neither."""
    if getattr(item, "super_jackpot", False):
        return SUPER_TIER
    return JACKPOT_TIER if item.jackpot else None


TITLE = "Valdo Sniper"
TRADE_URL = "https://www.pathofexile.com/trade/search"
# Font roles -> the first installed family. Every _font() call names a role,
# so a machine without (say) Bahnschrift degrades instead of rendering Tk's
# fallback serif.
FONT_ROLES = {
    "ui": ("Segoe UI", "Arial"),
    "semi": ("Segoe UI Semibold", "Segoe UI", "Arial"),
    "num": ("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "Arial"),
    "mono": ("Cascadia Mono", "Consolas", "Courier New"),
    "icon": ("Segoe Fluent Icons", "Segoe MDL2 Assets"),
}
# Header glyphs: Segoe icon-font codepoints, or plain symbols without one.
ICON_GLYPHS = {"settings": "", "volume": "", "mute": ""}
PLAIN_GLYPHS = {"settings": "⚙", "volume": "🔊", "mute": "🔇"}


# How long "Updated to vX" stays in the strip after a restart into it
UPDATED_NOTICE_MS = 120_000


def _update_pill(status: UpdateStatus) -> tuple[str, str, str]:
    """(pill text, dot colour, text colour) for a self-update state."""
    v = f"v{status.version}"
    return {
        "available": (f"↑ {v} out", GOOD, GOOD),
        "downloading": (f"↑ {v} downloading…", FAINT, DIM),
        "ready": (f"↑ {v} · restart", GOOD, GOOD),
        "failed": (f"↑ {v} failed", WARN, WARN),
        "installed": (f"✓ updated to {v}", GOOD, DIM),
    }.get(status.state, (f"↑ {v}", FAINT, DIM))


def _update_help(status: UpdateStatus, packaged: bool) -> tuple[str, str]:
    v = f"v{status.version}"
    if status.state == "ready":
        return (
            f"Update {v} is ready",
            f"Downloaded and verified. Click to restart into {v} now (takes a few "
            "seconds; live alerts on screen are dropped, open trade tabs reconnect by "
            "themselves). Or keep going: it installs the next time you start Valdo "
            f"Sniper. You are on v{__version__}.",
        )
    if status.state == "downloading":
        return (
            f"Downloading {v}",
            "A newer version is downloading in the background. Nothing changes until you restart.",
        )
    if status.state == "failed":
        return (
            f"Update {v} did not download",
            f"The download failed or did not verify ({status.detail or 'unknown error'}). "
            "It is retried later by itself. Click to open the release page and "
            "download it by hand.",
        )
    if status.state == "installed":
        return (
            f"Updated to {v}",
            "Valdo Sniper restarted into the new version. Click to see what changed.",
        )
    how = (
        "Updates download by themselves when auto_download is on."
        if packaged
        else "You are running from source: git pull (or download the ZIP again) and "
        "restart start.bat."
    )
    return (
        f"Version {v} is out",
        f"You are on v{__version__}. {how} Click to open the release page.",
    )


class _Pill:
    """Status-strip item: a coloured square and a short label.

    `config(text=, fg=)` mirrors the tk.Label calls the drain loop already
    makes: `fg` colours the square, and the text stays quiet unless the
    state is a problem worth reading (amber/red)."""

    def __init__(self, parent: tk.Widget, font):
        self.frame = tk.Frame(parent, bg=STRIP)
        self._dot = tk.Frame(self.frame, bg=FAINT, width=7, height=7)
        self._dot.pack(side="left", padx=(0, 6))
        self._label = tk.Label(self.frame, text="", bg=STRIP, fg=DIM, font=font)
        self._label.pack(side="left")
        self.widgets = (self.frame, self._dot, self._label)  # for hover help

    def config(
        self, text: str | None = None, fg: str | None = None, text_fg: str | None = None
    ) -> None:
        if text is not None:
            self._label.config(text=text)
        if fg is not None:
            self._dot.config(bg=fg)
            self._label.config(fg=text_fg or (fg if fg in (BAD, WARN) else DIM))

    def pack(self, **kw) -> None:
        self.frame.pack(**kw)

    def bind(self, sequence: str, func) -> None:
        for widget in (self.frame, self._dot, self._label):
            widget.bind(sequence, func)

    def set_cursor(self, cursor: str) -> None:
        for widget in (self.frame, self._dot, self._label):
            widget.configure(cursor=cursor)


class Overlay:
    def __init__(
        self,
        root: tk.Tk,
        bus: Bus,
        config: Config,
        on_travel=None,  # Callable[[str], None]: user clicked an alert row
        # Callable[[ModScoringConfig, float, str], str | None]: settings
        # applied (scoring, min_yield, hotkey combo) -> error or None
        on_settings_change=None,
        # Callable[[str, float | None], None]: pin a reward's reference price
        # for this session (None clears it back to automatic pricing)
        on_price_override=None,
        # Callable[[str], None]: price panel recheck button - re-price this
        # reward now rather than at its next scheduled refresh
        on_price_recheck=None,
        # Callable[[float, float], None]: jackpot bars (min net, min yield)
        on_jackpot_change=None,
        overrides_path=None,  # Path for scoring_overrides.yaml persistence
        # Callable[[], None]: update pill clicked with an update staged
        on_update_install=None,
    ):
        self._root = root
        self._bus = bus
        self._config = config
        self._on_travel = on_travel
        self._on_settings_change = on_settings_change
        self._on_price_override = on_price_override
        self._on_price_recheck = on_price_recheck
        self._on_jackpot_change = on_jackpot_change
        self._on_update_install = on_update_install
        self._update: UpdateStatus | None = None
        self._jackpot_net = config.alerts.jackpot_min_net
        self._jackpot_yield = config.alerts.jackpot_min_yield
        self._super_net = config.alerts.super_jackpot_min_net
        self._super_yield = config.alerts.super_jackpot_min_yield
        self._overrides_path = overrides_path
        # reward -> divine, the session price corrections currently applied.
        # Kept here so the panel only pushes what actually changed and can be
        # reopened showing the corrections still in force.
        self._price_overrides: dict[str, float] = {}
        # per-reward widgets of the open settings panel's price editor
        self._price_entries: dict[str, tk.Entry] = {}
        self._price_labels: dict[str, tk.Label] = {}
        self._scoring_config = config.mod_scoring
        self._current_threshold = config.thresholds.min_yield
        self._current_running_cost = config.thresholds.running_cost
        self._current_combo = config.hotkey.combo
        self._tune_window: tk.Toplevel | None = None
        self._tooltip: tk.Toplevel | None = None
        # hover help: the widget group whose help is showing or pending,
        # and the pending after() id (see _bind_help)
        self._help_group: tuple | None = None
        self._help_win: tk.Toplevel | None = None  # built once, see _build_help_win
        self._alerts: tuple[AlertView, ...] = ()
        self._muted = False
        self._volume = config.alerts.volume
        # Jackpot alarm: loops while any live jackpot is unsilenced (see
        # _sync_jackpot_alarm). Clicking the banner adds the live jackpots
        # to _jp_silenced; travel or expiry ends it by removing the alert.
        # None | "jackpot" | "super": which alarm is looping right now
        self._jp_ringing: str | None = None
        self._jp_silenced: set[str] = set()
        self._jp_tier: _Tier = JACKPOT_TIER  # the banner's current tier
        # (kind, sound path, volume) -> playable WAV | None; see _alert_wav.
        # A DICT, not a single slot: the alert kinds alternate, and a
        # one-entry cache would rescale a ~240 KB WAV on the alert path
        # every time the kind changed - exactly what the warm-up prevents.
        self._wav_cache: dict[tuple[str, str, float], str | None] = {}
        self._sound = SoundWorker(
            self._sound_backend,
            on_error=lambda e: event("alert_sound_error", error=repr(e)),
        )
        # after a travel, the traveled listing stays pinned in the top slot
        # for traveled_display_seconds instead of vanishing instantly
        self._pinned_top: AlertView | None = None
        self._pinned_until = 0.0
        self._pin_token = 0
        # shared Font objects, keyed by (family, base size, weight):
        # Ctrl+/Ctrl- rescale every widget through them (see _zoom)
        self._fonts: dict[tuple, tkfont.Font] = {}
        self._font_scale = 1.0
        # (calculating, ready, total, per-reward rows) from the price warm-up
        self._warmup: tuple | None = None
        self._ninja_status = "manual"
        self._pricing_health = None  # PricingHealth | None
        self._skew_logged = False  # clock-skew warning is logged once per run

        root.title(TITLE)
        self._icon_images = app_icon.apply(root)  # kept referenced: see icon.apply
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry("515x800+40+40")
        # the four-column headline needs the width; below this the numbers
        # and their unit captions start clipping
        root.minsize(470, 380)
        # bind_all so the zoom keys work from the settings panel too
        root.bind_all("<Control-plus>", lambda e: self._zoom(+1))
        root.bind_all("<Control-equal>", lambda e: self._zoom(+1))
        root.bind_all("<Control-KP_Add>", lambda e: self._zoom(+1))
        root.bind_all("<Control-minus>", lambda e: self._zoom(-1))
        root.bind_all("<Control-KP_Subtract>", lambda e: self._zoom(-1))
        root.bind_all("<Control-0>", lambda e: self._zoom(0))  # reset

        installed = set(tkfont.families(root))
        self._families = {
            role: next((f for f in names if f in installed), names[-1])
            for role, names in FONT_ROLES.items()
        }
        self._glyph = ICON_GLYPHS if self._families["icon"] in installed else PLAIN_GLYPHS
        # Connected live-search tabs. Zero means the user has not finished
        # setup (or closed every tab), so the idle card becomes a setup guide.
        self._tab_count = 0
        self._tabs_need_install = True
        host = config.server.host if config.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        self._install_url = f"http://{host}:{config.server.port}{INSTALL_PATH}"

        # ---- status strip: tabs | game | prices ........... mute  settings
        strip = tk.Frame(root, bg=STRIP)
        strip.pack(fill="x")
        inner = tk.Frame(strip, bg=STRIP)
        inner.pack(fill="x", padx=12, pady=7)
        self._tabs_label = _Pill(inner, self._font("ui", 9))
        self._tabs_label.pack(side="left")
        self._tabs_label.config(text="0 tabs", fg=BAD)
        # clicking it opens the installer whenever the script is missing/old
        self._tabs_label.bind("<Button-1>", lambda e: self._tabs_clicked())
        self._game_label = _Pill(inner, self._font("ui", 9))
        self._game_label.pack(side="left", padx=(14, 0))
        self._game_label.config(text="PoE ?", fg=FAINT)
        self._price_label = _Pill(inner, self._font("ui", 9))
        self._price_label.pack(side="left", padx=(14, 0))
        self._price_label.config(text="Prices", fg=FAINT)
        # self-update: shown only once a newer version exists
        self._update_pill = _Pill(inner, self._font("ui", 9))
        self._update_pill.bind("<Button-1>", lambda e: self._update_clicked())
        self._update_pill.set_cursor("hand2")
        self._bind_help(self._update_pill.widgets, self._update_help)
        tune_btn = tk.Label(
            inner,
            text=self._glyph["settings"],
            bg=STRIP,
            fg=DIM,
            font=self._font("icon", 11),
            cursor="hand2",
        )
        tune_btn.pack(side="right")
        tune_btn.bind("<Button-1>", lambda e: self._open_tuning())
        self._mute_btn = tk.Label(
            inner,
            text=self._glyph["volume"],
            bg=STRIP,
            fg=DIM,
            font=self._font("icon", 11),
            cursor="hand2",
        )
        self._mute_btn.pack(side="right", padx=(0, 12))
        self._mute_btn.bind("<Button-1>", lambda e: self._toggle_mute())
        self._bind_help(self._tabs_label.widgets, HELP["tabs"])
        self._bind_help(self._game_label.widgets, HELP["game"])
        self._bind_help(self._price_label.widgets, HELP["prices"])
        self._bind_help((self._mute_btn,), HELP["mute"])
        self._bind_help((tune_btn,), HELP["settings"])
        tk.Frame(root, bg=HAIR, height=1).pack(fill="x")

        # reward names being live-searched, on their own line (the strip is
        # too crowded to hold them); hovering shows each reward's price
        self._searches_label = tk.Label(
            root,
            text="",
            bg=BG,
            fg=FAINT,
            font=self._font("ui", 9),
            anchor="w",
            justify="left",
            wraplength=490,
        )
        self._searches_label.pack(fill="x", padx=12, pady=(6, 0))
        self._reward_prices: tuple = ()
        # RewardPrice.age_s is as of the push; ages shown are counted on
        # from when it arrived so "4m ago" keeps ticking between pushes
        self._reward_prices_at = time.monotonic()
        self._watch_names: list[str] = []  # reward names, from the tabs
        self._watch_extra: list[str] = []  # generic / unidentified tab notes
        self._bind_help((self._searches_label,), self._watching_help)

        # ---- the alert card. Every block inside it is optional except the
        # title row; _show_alert_body packs the visible ones in _card_blocks
        # order. The outline thickens to 2px magenta for a jackpot.
        self._card = tk.Frame(
            root, bg=SURFACE, highlightthickness=1, highlightbackground=HAIR, highlightcolor=HAIR
        )
        self._card.pack(fill="x", padx=12, pady=(8, 0))
        card = self._card

        self._jackpot_banner = tk.Frame(card, bg=JACKPOT_BANNER)
        self._jp_left = tk.Label(
            self._jackpot_banner,
            text="★  JACKPOT",
            bg=JACKPOT_BANNER,
            fg="white",
            font=self._font("ui", 12, "bold"),
        )
        self._jp_left.pack(side="left", padx=(12, 0), pady=6)
        self._jp_right = tk.Label(
            self._jackpot_banner, text="", bg=JACKPOT_BANNER, fg="white", font=self._font("num", 12)
        )
        self._jp_right.pack(side="right", padx=(0, 12))
        # clicking the banner silences the alarm - never travels: the
        # hotkey stays the only way to act on a listing
        for widget in (self._jackpot_banner, self._jp_left, self._jp_right):
            widget.bind("<Button-1>", lambda e: self._silence_jackpot())
        self._bind_help((self._jackpot_banner, self._jp_left, self._jp_right), self._jackpot_help)
        # currency-mismatch banner (the scam guard)
        self._banner = tk.Label(
            card, text="", bg=BAD_FILL, fg="white", font=self._font("ui", 11, "bold"), pady=4
        )

        # title row: reward name | latency badge | travel button
        self._title_row = tk.Frame(card, bg=SURFACE)
        self._key_label = tk.Label(
            self._title_row,
            text="Waiting for listings…",
            bg=SURFACE,
            fg=FG,
            font=self._font("semi", 16),
            anchor="w",
        )
        self._key_label.pack(side="left", fill="x", expand=True)
        # Travel button. A Frame of labels rather than a tk.Button so it can
        # carry the hotkey as a keycap. Pressing it is one user input driving
        # one server action - the same contract as the hotkey, routed
        # through the identical _click_top path. Labels never take keyboard
        # focus, so clicking it never pulls focus from the game.
        self._travel_btn = tk.Frame(self._title_row, bg=GOOD)
        self._travel_text = tk.Label(
            self._travel_btn, text="TELEPORT", bg=GOOD, fg=BTN_FG, font=self._font("ui", 10, "bold")
        )
        self._travel_text.pack(side="left", padx=(12, 8), pady=7)
        self._travel_key = tk.Label(
            self._travel_btn,
            text="",
            bg=BTN_FG,
            fg=GOOD,
            font=self._font("mono", 10, "bold"),
            padx=5,
        )
        self._travel_enabled = False
        self._travel_jackpot: _Tier | None = None
        for widget in (self._travel_btn, self._travel_text, self._travel_key):
            widget.bind("<Button-1>", lambda e: self._travel_enabled and self._click_top())
            widget.bind("<Enter>", lambda e: self._paint_travel(hover=True))
            widget.bind("<Leave>", lambda e: self._paint_travel(hover=False))
        self._travel_btn.pack(side="right", padx=(10, 0))
        self._bind_help((self._travel_btn, self._travel_text, self._travel_key), self._travel_help)
        # How stale the listing already was when we first saw it. Sits next
        # to the travel button because it is a go/no-go signal: the longer it
        # was live before reaching us, the more likely it is already sold.
        self._age_label = tk.Label(
            self._title_row,
            text="",
            bg=SURFACE,
            fg=DIM,
            font=self._font("mono", 9),
            padx=5,
            pady=1,
            highlightthickness=0,
        )
        self._age_label.pack(side="right")
        self._bind_help((self._age_label,), HELP["latency"])
        self._set_travel_button(has_target=False, pinned=False)  # nothing yet at startup

        # Identity line for a generic (non-Valdo) alert: a rare's own name
        # and the seller. Valdo alerts do not need it - the reward IS the
        # identity.
        # For a Valdo alert it says what the reward is worth and how old
        # that price is - the number NET is worked out from.
        self._subtitle = tk.Label(
            card, text="", bg=SURFACE, fg=DIM, font=self._font("ui", 10), anchor="w"
        )
        self._bind_help(
            (self._subtitle,),
            lambda: (
                None
                if self._top_view() is None or self._top_view().generic
                else HELP["reward_value"]
            ),
        )

        # Where the item sits in the seller's tab: a mini grid with the cell
        # marked, then "bottom right · tab 7 · col 9, row 9". Read during the
        # hideout load so the click at Faustus is already aimed.
        self._stash_row = tk.Frame(card, bg=SURFACE)
        self._stash_grid = tk.Canvas(
            self._stash_row,
            width=STASH_GRID_PX + 2,
            height=STASH_GRID_PX + 2,
            bg=SURFACE,
            highlightthickness=0,
        )
        self._stash_grid.pack(side="left")
        self._stash_where = tk.Label(
            self._stash_row, text="", bg=SURFACE, fg=FG, font=self._font("semi", 13)
        )
        self._stash_where.pack(side="left", padx=(10, 0))
        self._stash_detail = tk.Label(
            self._stash_row, text="", bg=SURFACE, fg=DIM, font=self._font("ui", 10)
        )
        self._stash_detail.pack(side="left", padx=(10, 0))
        self._stash_widgets = (
            self._stash_row,
            self._stash_grid,
            self._stash_where,
            self._stash_detail,
        )
        self._bind_help(self._stash_widgets, HELP["stash"])

        # First-run guide: shown in place of the numbers while no live-search
        # tab is connected. Both rows act on a click only.
        self._setup_panel = tk.Frame(card, bg=SURFACE)
        for num, text, action in (
            ("1", "Install the browser script", self._open_install),
            ("2", "Open a Valdo live search on the trade site", lambda: self._open_link(TRADE_URL)),
        ):
            line = tk.Frame(self._setup_panel, bg=SURFACE)
            line.pack(fill="x", pady=(0, 5))
            parts = (
                line,
                tk.Label(
                    line, text=num, bg=HAIR, fg=FG, font=self._font("mono", 9, "bold"), width=2
                ),
                tk.Label(line, text=text, bg=SURFACE, fg=FG, font=self._font("ui", 10), anchor="w"),
                tk.Label(line, text="→", bg=SURFACE, fg=GOOD, font=self._font("ui", 10, "bold")),
            )
            parts[1].pack(side="left")
            parts[2].pack(side="left", padx=(8, 0))
            parts[3].pack(side="right")
            for widget in parts:
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda e, act=action: act())
        tk.Label(
            self._setup_panel,
            text="First: chrome://extensions → Tampermonkey → Details →\n"
            "turn on “Allow User Scripts”. Chrome runs nothing without it.\n"
            "The tabs light turns green once a search tab connects.",
            bg=SURFACE,
            fg=FAINT,
            font=self._font("ui", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        # The four numbers that decide a snipe, in the order you read them:
        # what you pay, what you make, what it is worth per unit of pain,
        # and how much pain. The frame's HAIR background showing through the
        # 1px gaps between cells IS the divider grid.
        self._stats = tk.Frame(card, bg=HAIR)
        self._stat_cells: dict[str, tk.Frame] = {}
        self._stat_caps: dict[str, tk.Label] = {}
        self._stat_vals: dict[str, tk.Label] = {}
        # widgets inside a cell beyond its caption and number (the net's
        # "div" unit and the row holding it): clickable and hoverable too
        self._stat_extras: list[tk.Widget] = []
        last = len(STAT_COLUMNS) - 1
        for col, (key, caption) in enumerate(STAT_COLUMNS):
            self._stats.grid_columnconfigure(col, weight=1, uniform="stat")
            cell = tk.Frame(self._stats, bg=SURFACE)
            cell.grid(row=0, column=col, sticky="nsew", padx=(0, 0 if col == last else 1), pady=1)
            cap = tk.Label(
                cell, text=caption, bg=SURFACE, fg=DIM, font=self._font("ui", 7, "bold"), anchor="w"
            )
            cap.pack(fill="x", padx=12, pady=(8, 0))
            extras: tuple[tk.Widget, ...] = ()
            if key == "profit":
                # "+15 div": the unit follows the number, smaller, so a
                # three-digit net still fits the cell
                row = tk.Frame(cell, bg=SURFACE)
                row.pack(fill="x", padx=12, pady=(0, 6))
                val = tk.Label(row, text="", bg=SURFACE, fg=PRICE, font=self._font("num", 21))
                val.pack(side="left")
                self._net_unit = tk.Label(
                    row, text="", bg=SURFACE, fg=DIM, font=self._font("ui", 10, "bold")
                )
                self._net_unit.pack(side="left", anchor="s", padx=(4, 0), pady=(0, 5))
                extras = (row, self._net_unit)
                self._stat_extras.extend(extras)
            else:
                val = tk.Label(
                    cell, text="", bg=SURFACE, fg=PRICE, font=self._font("num", 21), anchor="w"
                )
                val.pack(fill="x", padx=12, pady=(0, 6))
            self._stat_cells[key] = cell
            self._stat_caps[key] = cap
            self._stat_vals[key] = val
            # below the whole row, so it never covers the numbers being read
            self._bind_help(
                (cell, cap, val, *extras), lambda k=key: self._stat_help(k), anchor=self._stats
            )

        self._chips = tk.Frame(card, bg=SURFACE)  # warning chips
        # Deadly mod pairings get their own full-width rows rather than a
        # chip: the note ("Impossible unless DPS-check build") is the part
        # worth reading, and it does not fit in a chip.
        self._pairings = tk.Frame(card, bg=SURFACE)
        # Failure risks (Bismuth, Protect the Altar under less damage...):
        # one amber row each plus a line saying what they cost. Net is
        # already an expected value; these rows say why it is lower.
        self._risks = tk.Frame(card, bg=SURFACE)
        # the top listing's mods, always visible (no hover needed)
        self._top_mods = tk.Frame(card, bg=MODS_BG, pady=6)
        self._countdown = tk.Canvas(card, height=4, bg=COUNTDOWN_BG, highlightthickness=0)
        self._bind_help((self._countdown,), self._countdown_help)

        self._card_blocks = (
            (self._jackpot_banner, {"fill": "x"}),
            (self._banner, {"fill": "x"}),
            (self._title_row, {"fill": "x", "padx": 12, "pady": (10, 8)}),
            (self._subtitle, {"fill": "x", "padx": 12, "pady": (0, 6)}),
            (self._stash_row, {"fill": "x", "padx": 12, "pady": (0, 8)}),
            (self._setup_panel, {"fill": "x", "padx": 12, "pady": (0, 10)}),
            (self._stats, {"fill": "x"}),
            (self._chips, {"fill": "x", "padx": 12, "pady": (8, 0)}),
            (self._pairings, {"fill": "x", "padx": 12, "pady": (6, 0)}),
            (self._risks, {"fill": "x", "padx": 12, "pady": (6, 0)}),
            (self._top_mods, {"fill": "x", "padx": 12, "pady": (8, 0)}),
            (self._countdown, {"fill": "x", "padx": 12, "pady": (10, 12)}),
        )

        # Reward prices: each searched reward's price, its age and whether
        # it can be trusted, plus fetch progress while pricing runs. Shown
        # whenever no alert is (so a price can be judged BEFORE an alert
        # arrives), hidden while one is - the alert names its own price.
        self._price_panel = tk.Frame(root, bg=MODS_BG, pady=4)
        # the frame persists, so it is bound ONCE; the rows are rebuilt and
        # bound per render (see _render_price_panel)
        self._bind_help(
            (self._price_panel,),
            HELP["price_panel"],
            anchor=self._price_panel,
            member_of=self._price_panel,
        )

        self._runners = tk.Frame(root, bg=BG)  # one clickable row per runner-up
        self._runners.pack(fill="x", padx=12, pady=(6, 0))

        # Status line is packed BEFORE the feed so it wins the bottom slot:
        # pack gives space in call order, and the expanding feed would
        # otherwise squeeze travel results off-screen in a small window.
        self._status_line = tk.Label(
            root, text="", bg=BG, fg=DIM, font=self._font("mono", 9), anchor="w"
        )
        self._status_line.pack(side="bottom", fill="x", padx=12, pady=(2, 6))

        # live feed: every incoming listing, alerting or not; non-alerting
        # rows render diminished. Newest first. Hover a row for its mods.
        self._feed_entries: deque = deque(maxlen=max(config.alerts.feed_rows, 1))
        feed_box = tk.Frame(root, bg=BG)
        # expand: the feed absorbs the window's spare vertical space instead
        # of leaving a gap between the alert area and the status line
        feed_box.pack(side="bottom", fill="both", expand=True, padx=12, pady=(12, 0))
        self._threshold_note = tk.Label(
            feed_box, text="", bg=BG, fg=DIM, font=self._font("ui", 8, "bold"), anchor="w"
        )
        self._threshold_note.pack(fill="x", pady=(0, 4))
        self._update_threshold_note()
        self._bind_help(
            (self._threshold_note,),
            lambda: (
                "Alert bar",
                f"A listing alerts when its YIELD is at least {self._current_threshold:g}. "
                "Change it in ⚙ Settings (Alert at YIELD ≥).",
            ),
        )
        feed_header = tk.Label(
            feed_box,
            # yield = net per 100 difficulty: the same unit as the threshold
            # noted directly above, and the alert ranking key
            text=f"{'PRICE':<8}{'NET':<9}{'YIELD':<9}{'DIFF':<12}{'AGE':<10}REWARD",
            bg=BG,
            fg=DIM,
            font=self._font("mono", 8, "bold"),
            anchor="w",
            padx=6,
        )
        feed_header.pack(fill="x")
        self._bind_help((feed_header,), HELP["feed"])
        tk.Frame(feed_box, bg=HAIR, height=1).pack(fill="x", pady=(3, 0))

        # The history is longer than the window, so the rows live on a
        # scrollable canvas while the note and column header above stay
        # pinned in place.
        scroll_area = tk.Frame(feed_box, bg=BG)
        scroll_area.pack(fill="both", expand=True)
        self._feed_canvas = tk.Canvas(
            scroll_area, bg=BG, highlightthickness=0, height=FEED_MIN_HEIGHT
        )
        self._feed_canvas.pack(fill="both", expand=True)
        # Overlay scrollbar: `place`d ON TOP of the canvas rather than packed
        # beside it, so appearing and vanishing never reflows the rows. Flat
        # dark styling instead of the native chrome, and it only shows while
        # scrolling (see _flash_scrollbar).
        self._feed_bar = tk.Scrollbar(
            scroll_area,
            orient="vertical",
            command=self._feed_canvas.yview,
            width=8,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            troughcolor=BG,
            bg="#46525f",
            activebackground="#5d6b7a",
            elementborderwidth=0,
        )
        self._bar_hide_id: str | None = None
        self._feed_canvas.configure(yscrollcommand=self._feed_bar.set)
        # keep it alive while the pointer is on it, so it can be dragged
        self._feed_bar.bind("<Enter>", lambda e: self._cancel_bar_hide())
        self._feed_bar.bind("<Leave>", lambda e: self._flash_scrollbar())
        self._feed = tk.Frame(self._feed_canvas, bg=BG)
        feed_window = self._feed_canvas.create_window((0, 0), window=self._feed, anchor="nw")
        # keep the scrollable region in step with the rows, and the rows as
        # wide as the canvas so a full-width row stays clickable
        self._feed.bind(
            "<Configure>",
            lambda e: self._feed_canvas.configure(scrollregion=self._feed_canvas.bbox("all")),
        )
        self._feed_canvas.bind(
            "<Configure>", lambda e: self._feed_canvas.itemconfigure(feed_window, width=e.width)
        )
        self._feed_canvas.bind("<MouseWheel>", self._feed_wheel)
        self._feed.bind("<MouseWheel>", self._feed_wheel)

        # fixed pool of feed row labels updated in place: destroying and
        # recreating a dozen Labels per listing (widget + font layout) is
        # where render-time outliers came from. Rows are packed on first
        # use and never unpacked (the deque only grows to maxlen).
        self._feed_rows: list[tk.Label] = []
        self._feed_mapped = 0
        for i in range(self._feed_entries.maxlen or 1):
            row = tk.Label(
                self._feed,
                text="",
                bg=BG,
                fg=FAINT,
                font=self._font("mono", 9),
                anchor="w",
                padx=6,
                pady=2,
                cursor="hand2",
            )
            row.bind("<Button-1>", lambda e, idx=i: self._feed_click(idx))
            # rows sit above the canvas, so they need the wheel binding too
            row.bind("<MouseWheel>", self._feed_wheel)
            self._bind_tooltip(row, lambda idx=i: self._feed_mods(idx))
            self._feed_rows.append(row)

        # the top alert is clickable too - click any listing to travel to it.
        # Every headline number is part of the same target.
        clickable = (
            self._key_label,
            *self._stat_vals.values(),
            *self._stat_caps.values(),
            *self._stat_cells.values(),
            *self._stat_extras,
            *self._stash_widgets,
        )
        self._top_widgets = clickable
        for widget in clickable:
            widget.bind("<Button-1>", lambda e: self._click_top())
            widget.configure(cursor="hand2")
        # No hover tooltip on the headline: the top alert already renders its
        # full mod list inline in the dark panel below, so the popup only
        # repeated it - directly on top of the numbers being read.

        self._build_help_win()

        # Warm the (silent) audio cache off-thread so the first alert never
        # pays for reading and rescaling the WAV.
        threading.Thread(
            target=self._warm_alert_wavs, name="alert-sound-warmup", daemon=True
        ).start()

        # Warm-up: realize the banner once so its first real appearance does
        # not pay font-load/relayout cost. (No startup sound: alert audio
        # plays on a background thread, so no priming is needed.)
        self._banner.config(text="⚠ CURRENCY MISMATCH")
        self._banner.pack(fill="x")
        root.update_idletasks()
        self._banner.pack_forget()
        self._render_alerts()  # lay the card out in its idle / setup state

        # event-driven drain: the asyncio thread fires <<BusWake>> after each
        # put; the DRAIN_MS poll below only covers a lost/failed wake
        root.bind("<<BusWake>>", lambda e: self._drain_once())
        self._wake_flag = threading.Event()
        threading.Thread(target=self._wake_loop, name="tk-waker", daemon=True).start()
        bus.set_waker(self._wake_flag.set)
        root.after(DRAIN_MS, self._drain)
        root.after(TICK_MS, self._tick)
        root.after(30_000, self._refresh_feed_ages)

    # ------------------------------------------------------------------ bus

    def _wake_loop(self) -> None:
        """Turns bus puts into <<BusWake>> events, on its own thread.

        The asyncio thread only sets `_wake_flag` and never touches Tcl. A
        Tcl call from a non-main thread is marshalled to the Tk thread and
        BLOCKS until the Tk thread runs it - i.e. until the render in
        progress finishes. Called straight from bus.put, that stalled the
        detection path behind every overlay render, and each put then
        rendered on its own instead of batching: a burst of 10 listings/s
        backed decisions up by seconds. Here the blocking lands on this
        thread, and every put made while it waits folds into one wake, so a
        burst costs one drain per render rather than one per listing.
        When the window is tearing down, the fallback poll covers the rest.
        """
        while True:
            self._wake_flag.wait()
            self._wake_flag.clear()  # before the wake: a put during it re-arms
            try:
                self._root.event_generate("<<BusWake>>", when="tail")
            except (tk.TclError, RuntimeError):
                time.sleep(DRAIN_MS / 1000)  # tearing down / not in mainloop yet

    def _drain(self) -> None:
        self._drain_once()
        self._root.after(DRAIN_MS, self._drain)

    def _drain_once(self) -> None:
        """Apply every queued event, then render each dirty region ONCE - a
        burst of events costs one alerts render + one feed render, not one
        per event."""
        alerts_dirty = False
        feed_dirty = False
        new_alert = False
        new_views: list[AlertView] = []
        for ev in self._bus.drain():
            if isinstance(ev, AlertsChanged):
                self._alerts = ev.alerts
                alerts_dirty = True
                if ev.new_alert:
                    new_alert = True
                    # ev.new_view carries the arriving alert even when it
                    # ranks below the display cut
                    if ev.new_view is not None:
                        new_views.append(ev.new_view)
            elif isinstance(ev, TabsChanged):
                n = len(ev.tabs)
                # a tab whose 30s heartbeat is >75s old is silently dead
                stale = sum(1 for t in ev.tabs if t.get("hello_age_s", 0) > 75)
                rewards = sorted(
                    {_display_name(t["search_reward"]) for t in ev.tabs if t.get("search_reward")}
                )
                # A tab running an older userscript silently drops whatever
                # the new version added - the fields just arrive missing,
                # which looks like a broken feature rather than a stale
                # script. Name it here; it is otherwise only visible by
                # grepping userscript_version out of the logs.
                # A missing version means a script older than the one that
                # started reporting it, so it is stale too - not unknown.
                old = sorted(
                    {
                        t.get("version") or "pre-0.7.2"
                        for t in ev.tabs
                        if t.get("version") != EXPECTED_USERSCRIPT_VERSION
                    }
                )
                text = f"{n} tab{'' if n == 1 else 's'}" + (f" · {stale} stale" if stale else "")
                # a tab whose listings have crept from ~1.5s late to 4-6s
                # late over hours (see sniper.tablag): a reload fixes it
                slow = sorted(
                    _display_name(t.get("search_reward") or "a tab")
                    for t in ev.tabs
                    if t.get("slow")
                )
                if slow:
                    text += f" · reload {', '.join(slow)}"
                if old:
                    # clicking the pill reinstalls; Tampermonkey also picks
                    # the new version up by itself via @updateURL
                    text += " · update script"
                    event("userscript_outdated", running=old, expected=EXPECTED_USERSCRIPT_VERSION)
                self._tabs_label.config(
                    text=text, fg=BAD if old else WARN if stale or slow else GOOD if n else BAD
                )
                self._tabs_need_install = bool(old) or n == 0
                self._tabs_label.set_cursor("hand2" if self._tabs_need_install else "")
                if (n == 0) != (self._tab_count == 0):
                    alerts_dirty = True  # idle card switches to/from the setup guide
                self._tab_count = n
                # a generic tab has no reward BY DESIGN, so it must not be
                # counted as one whose reward could not be identified
                generic = sum(1 for t in ev.tabs if t.get("search_kind") == "any")
                unknown = sum(
                    1
                    for t in ev.tabs
                    if not t.get("search_reward") and t.get("search_kind") != "any"
                )
                extra = []
                if generic:
                    extra.append(f"⚡{generic} live search" + ("es" if generic > 1 else ""))
                if unknown and n:  # tabs whose reward is not yet known
                    extra.append(f"+{unknown} unidentified")
                self._watch_names, self._watch_extra = rewards, extra
                self._render_watching()
            elif isinstance(ev, PriceStatus):
                self._ninja_status = ev.status
                self._render_price_pill()
            elif isinstance(ev, UpdateStatus):
                self._set_update(ev)
            elif isinstance(ev, PricingHealth):
                self._pricing_health = ev
                self._render_price_pill()
            elif isinstance(ev, ClickOutcome):
                if ev.ok:
                    text = "Travel sent" + (f" ({ev.reason})" if ev.reason else "")
                else:
                    text = f"TRAVEL FAILED: {ev.reason.replace('_', ' ')}"
                    # a failed travel (item sold, row gone...) must not keep
                    # the ➜ pin up as if the teleport were in progress
                    if (
                        self._pinned_top is not None
                        and self._pinned_top.listing_id == ev.listing_id
                    ):
                        self._pin_token += 1  # cancel the pending unpin timer
                        self._pinned_top = None
                        alerts_dirty = True
                self._status_line.config(text=text, fg=GOOD if ev.ok else BAD)
            elif isinstance(ev, GameStatus):
                self._game_label.config(
                    text="PoE running" if ev.running else "PoE not running",
                    fg=GOOD if ev.running else BAD,
                )
            elif isinstance(ev, Traveled):
                # keep the traveled listing in the top slot (mods visible
                # inline) so it's readable during the loading screen
                self._pin_top(ev.view)
                alerts_dirty = True
            elif isinstance(ev, ListingSeen):
                self._feed_entries.appendleft(ev.entry)
                feed_dirty = True
            elif isinstance(ev, RewardPrices):
                self._reward_prices = ev.entries
                self._reward_prices_at = time.monotonic()
                self._refresh_price_editor()
                self._render_watching()
                self._refresh_reward_value()
                self._refresh_price_panel()
            elif isinstance(ev, WarmupStatus):
                warmup = (ev.calculating, ev.priced, ev.total, ev.rewards)
                if warmup != self._warmup:
                    self._warmup = warmup
                    alerts_dirty = True  # headline + progress panel both move
        if alerts_dirty:
            self._render_alerts()
        if feed_dirty:
            self._render_feed()
        # log AFTER the render it measures, BEFORE the sound call so audio
        # quirks never pollute the render latency figure
        for view in new_views:
            event(
                "alert_shown",
                listing_id=view.listing_id,
                frame_to_ui_ms=round((time.monotonic() - view.created_monotonic) * 1000, 1),
            )
        for view in new_views:
            if view.jackpot:
                event("jackpot_alert", listing_id=view.listing_id, key=view.key)
        if alerts_dirty:
            self._sync_jackpot_alarm()
        # one sound per batch: SND_ASYNC bursts cancel anyway. A ringing
        # jackpot alarm owns the speaker - a routine woop would cut it off.
        # That includes a generic hit: a live jackpot outranks it, so the
        # alarm keeps ringing for the hotkey's target. Silence the banner
        # and later generic hits sound again.
        if new_alert and not self._jp_ringing:
            # A batch carrying a generic hit plays the generic sound: those
            # are the rare, hand-picked ones, so if a batch mixes kinds the
            # one worth interrupting for wins.
            self._play_sound("generic" if any(v.generic for v in new_views) else "valdo")

    # ----------------------------------------------------------------- fonts

    def _font(
        self, family: str, size: int, weight: str = "normal", underline: bool = False
    ) -> tkfont.Font:
        """Shared mutable Font per (family, base size, weight, underline);
        every widget - static, per-render, or tooltip - goes through here so
        _zoom can rescale the whole overlay at once."""
        family = self._families.get(family, family)  # a role -> installed family
        key = (family, size, weight, underline)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(
                family=family, size=self._scaled(size), weight=weight, underline=underline
            )
        return self._fonts[key]

    def _scaled(self, size: int) -> int:
        return max(6, round(size * self._font_scale))

    def _zoom(self, step: int) -> None:
        """Ctrl+ / Ctrl- / Ctrl+0: grow, shrink, or reset every font."""
        scale = 1.0 if step == 0 else round(self._font_scale + 0.1 * step, 2)
        scale = min(1.8, max(0.7, scale))
        if scale == self._font_scale:
            return
        self._font_scale = scale
        for key, f in self._fonts.items():
            f.configure(size=self._scaled(key[1]))  # key[1] is the base size

    # ------------------------------------------------------------- rendering

    def _reward_price_rows(self):
        """Searching-line tooltip. The price source is deliberately not
        shown - the header's price pill already reports it - so these rows
        carry no right-hand note. A hand-pinned price is the exception: it
        did not come from any market source, so it says so.

        A reward with no price yet still gets a row saying so: an empty
        answer here is indistinguishable from a broken hover.
        """
        rows = []
        for entry in self._reward_prices:
            name = _display_name(entry.reward)
            if entry.amount is None:
                health = self._pricing_health
                waiting = "rate limited" if health and health.backoff_s > 0 else "pricing…"
                rows.append((f"{name}: {waiting}", "", "yellow"))
                continue
            note = _ago(self._live_age(entry))
            if entry.override_div is not None:
                note = "your price"
            rows.append(
                (
                    f"{name}: {entry.amount:g} {entry.currency}"
                    + (f"   ·   {note}" if note else ""),
                    "",
                    "yellow" if entry.override_div is not None else "none",
                )
            )
        return tuple(rows)

    def _live_age(self, entry) -> float | None:
        if entry.age_s is None:
            return None
        return entry.age_s + (time.monotonic() - self._reward_prices_at)

    def _render_watching(self) -> None:
        """Watching line: each searched reward with its price and how long ago
        that price was fetched, then any non-reward tabs."""
        prices = {_display_name(e.reward): e for e in self._reward_prices}
        names = []
        for name in self._watch_names:
            entry = prices.get(name)
            item = f"{name} {_price_tag(entry, self._live_age(entry))}" if entry else name
            # non-breaking spaces inside an item: the line wraps BETWEEN
            # rewards, never between a name and its price or age
            names.append(item.replace(" ", " "))
        parts = ([",  ".join(names)] if names else []) + self._watch_extra
        self._searches_label.config(text=f"Watching  {' · '.join(parts)}" if parts else "")

    def _reward_value_text(self, view: AlertView) -> str:
        """The line under a Valdo alert's name: what its reward is worth, when
        that price was last checked, and its source when it is not trade."""
        if view.generic or not view.reference_amount:
            return ""
        bits = [f"Reward worth {_money(view.reference_amount, view.reference_currency)}"]
        # a trade price is the normal case and goes unnamed; any other source
        # (last session, poe.ninja, a pinned price) is worth calling out
        source = SOURCE_WORDS.get(view.reference_source)
        if source:
            bits.append(source)
        entry = next((e for e in self._reward_prices if e.reward == view.key), None)
        ago = _ago(self._live_age(entry)) if entry is not None else ""
        if ago:
            bits.append(f"price checked {ago}")
        return "  ·  ".join(bits)

    def _refresh_reward_value(self) -> None:
        """Keep the reward-value line's age current without a full re-render."""
        top = self._top_view()
        if top is not None and not top.generic:
            self._subtitle.config(text=self._reward_value_text(top))

    def _top_view(self) -> AlertView | None:
        if self._pinned_top is not None:
            return self._pinned_top
        return self._alerts[0] if self._alerts else None

    def _price_editor_text(self, entry) -> str:
        """Label for one row of the settings panel's price editor: the reward
        and what the app would price it at with no override, so a correction
        is always typed next to the number it replaces (and so clearing one
        shows what it returns to)."""
        name = _display_name(entry.reward)
        if entry.auto_amount is None:
            return f"{name} · auto none yet"
        return f"{name} · auto {entry.auto_amount:g} {entry.auto_currency}"

    def _refresh_price_editor(self) -> None:
        """Keep the open panel's 'auto:' notes current as prices refresh.

        Only the notes - never the entry widgets, which the user may be
        typing in. A background re-price must not move the cursor or
        overwrite a half-typed correction.
        """
        if not self._price_labels:
            return
        for entry in self._reward_prices:
            label = self._price_labels.get(entry.reward)
            if label is not None and label.winfo_exists():
                label.config(text=self._price_editor_text(entry))

    def _update_threshold_note(self) -> None:
        """Stated in the Yield column's units so the cutoff can be read
        straight off the feed."""
        self._threshold_note.config(
            text=f"RECENT LISTINGS  ·  ALERTING AT YIELD ≥ {self._current_threshold:g}"
        )

    def _render_price_pill(self) -> None:
        """The header's pricing pill reports the WORST current problem, so a
        rate-limited or half-priced session is never mistaken for a healthy
        one.

        It names the source of REWARD prices only. poe.ninja is still
        fetched for currency rates and the league, but it stopped being the
        reward price source, so saying "poe.ninja" here would misreport
        where the profit numbers come from.
        """
        health = self._pricing_health
        if health is not None and health.backoff_s > 0:
            self._price_label.config(text=f"RATE LIMITED {health.backoff_s:.0f}s", fg=BAD)
            return
        if health is not None and health.unpriced:
            self._price_label.config(text=f"Est. prices {health.unpriced}/{health.total}", fg=WARN)
            return
        if self._config.trade_pricing.enabled:
            # healthy: prices always come from trade, nothing worth saying
            self._price_label.config(text="Prices", fg=GOOD)
            return
        status = self._ninja_status  # trade pricing off: ninja IS the source
        color = {"live": GOOD, "stale": WARN, "manual": DIM}.get(status, DIM)
        self._price_label.config(text=f"Prices: {status}", fg=color)

    def _pricing_is_busy(self) -> bool:
        """Warming up, or re-pricing a reward right now (a scheduled refresh,
        or a search the user just opened)."""
        if not self._warmup:
            return False
        return bool(self._warmup[0]) or any(row[1] == "working" for row in self._warmup[3])

    def _price_rows(self) -> list[tuple[str, str, str, str, str, str | None]]:
        """(reward key, reward name, price, note, level, fetch state) for
        every searched reward, from the latest RewardPrices merged with the
        warm-up's fetch states."""
        states = {row[0]: row[1] for row in (self._warmup[3] if self._warmup else ())}
        prices = {e.reward: e for e in self._reward_prices}
        refresh_s = self._config.trade_pricing.refresh_minutes * 60
        rows = []
        for reward in sorted(set(prices) | set(states), key=_display_name):
            entry = prices.get(reward)
            age = self._live_age(entry) if entry is not None else None
            state = states.get(reward)
            price, note, level = _price_status(entry, state, age, refresh_s)
            rows.append((reward, _display_name(reward), price, note, level, state))
        return rows

    def _render_price_panel(self) -> None:
        """Each searched reward's price, its age and how far to trust it.
        The ages tick on (see _refresh_feed_ages); a reward being re-fetched
        keeps its old figure, marked ↻, because that is exactly when you are
        looking at a stale number."""
        rows = self._price_rows()
        if not rows:
            self._price_panel.pack_forget()
            return
        self._clear_frame(self._price_panel)
        minutes = self._config.trade_pricing.refresh_minutes
        header = tk.Label(
            self._price_panel,
            text=f"REWARD PRICES  ·  RE-CHECKED EVERY {minutes:g} MIN",
            bg=MODS_BG,
            fg=DIM,
            font=self._font("ui", 8, "bold"),
            anchor="w",
        )
        header.pack(fill="x", padx=6, pady=(0, 3))
        colour = {"good": GOOD, "warn": WARN, "bad": BAD, "dim": DIM}
        can_recheck = self._on_price_recheck is not None and self._config.trade_pricing.enabled
        for reward, name, price, note, level, state in rows:
            line = tk.Frame(self._price_panel, bg=MODS_BG)
            line.pack(fill="x", padx=6, pady=1)
            if can_recheck:
                self._recheck_button(line, reward, busy=state in ("working", "queued"))
            tk.Label(
                line,
                text=f"{note:<20}",
                bg=MODS_BG,
                fg=colour[level] if level != "good" else DIM,
                font=self._font("mono", 9),
                anchor="w",
            ).pack(side="right", padx=(10, 0))
            tk.Label(
                line,
                text=price,
                bg=MODS_BG,
                fg=colour[level],
                font=self._font("mono", 10, "bold"),
                anchor="e",
            ).pack(side="right")
            tk.Label(
                line,
                text=name,
                bg=MODS_BG,
                fg=FG if level != "dim" else DIM,
                font=self._font("mono", 10),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
        self._bind_help(
            self._price_panel.winfo_children(),
            HELP["price_panel"],
            anchor=self._price_panel,  # below the table, not over its rows
            descendants=True,
            member_of=self._price_panel,
        )
        self._price_panel.pack(fill="x", padx=12, pady=(6, 0), before=self._runners)

    def _recheck_button(self, parent: tk.Widget, reward: str, busy: bool) -> None:
        """A row's "recheck" button: re-price this reward now. One click asks
        for one fetch; while that fetch is queued or running the button is
        greyed out and ignores clicks (the app ignores repeats too)."""
        btn = tk.Label(
            parent,
            text="recheck",
            bg=MODS_BG if busy else BOX,
            fg=FAINT if busy else FG,
            font=self._font("ui", 8),
            padx=5,
            cursor="arrow" if busy else "hand2",
        )
        btn.pack(side="right", padx=(8, 0))
        if busy:
            return

        def click(_event=None) -> None:
            btn.config(bg=MODS_BG, fg=FAINT, cursor="arrow")
            btn.unbind("<Button-1>")
            event("price_recheck_clicked", reward=reward)
            self._on_price_recheck(reward)

        btn.bind("<Button-1>", click)

    def _refresh_price_panel(self) -> None:
        """Re-render the panel if it is the one on screen (idle card)."""
        if not self._alerts and self._pinned_top is None and self._tab_count:
            self._render_price_panel()

    def _idle_text(self) -> str:
        """Headline when nothing is alerting. During the startup warm-up it
        says so, because listings really are being withheld until their
        reward has a trustworthy price."""
        if self._warmup is not None:
            calculating, ready, total = self._warmup[:3]
            if calculating and total:
                return f"Calculating prices…  ({ready}/{total})"
            # A re-price in flight deliberately does NOT rewrite this line:
            # the pricing panel below already names the reward and marks it
            # with ↻, and the headline should keep saying what it means for
            # sniping - that there is nothing to travel to.
        return "No active alert"

    def _render_mods(self, parent: tk.Frame, mods, bg: str, wrap: int) -> None:
        """One row per mod: a severity square, the text, and its scoring
        modifier pinned right so the modifiers line up as a scannable column
        instead of trailing off after ragged mod text. Caller clears
        `parent` first."""
        for text, note, level in mods:
            fg = BAD if level == "red" else WARN if level == "yellow" else DIM
            weight = "bold" if level != "none" else "normal"
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", padx=10, pady=1)
            tk.Frame(row, bg=fg if level != "none" else DOT_NONE, width=6, height=6).pack(
                side="left", anchor="n", pady=(7, 0), padx=(0, 8)
            )
            if note:
                # packed before the text so it reserves the right-hand column;
                # top-aligned so a multi-line combo mod's modifier reads as
                # belonging to the block, not floating in its middle
                tk.Label(
                    row,
                    text=note,
                    bg=bg,
                    fg=fg,
                    font=self._font("mono", 9, weight),
                    anchor="ne",
                ).pack(side="right", padx=(10, 0), anchor="n")
            tk.Label(
                row,
                text=text,
                bg=bg,
                fg=fg,
                font=self._font("ui", 9, weight),
                anchor="w",
                justify="left",
                wraplength=wrap,
            ).pack(side="left", fill="x", expand=True)

    def _clear_frame(self, frame: tk.Frame) -> None:
        self._hide_tooltip()  # a hovered row may be getting destroyed
        for child in frame.winfo_children():
            child.destroy()

    def _click_top(self) -> None:
        if self._on_travel is None:
            return
        if self._pinned_top is None and self._alerts:
            self._on_travel(self._alerts[0].listing_id)
        elif self._pinned_top is not None:
            self._on_travel(self._pinned_top.listing_id)  # no-ops: already traveled

    def _pin_top(self, view: AlertView) -> None:
        """Sets pin state only; the caller (drain) renders once per batch."""
        self._pinned_top = view
        self._pinned_until = time.monotonic() + self._config.alerts.traveled_display_seconds
        self._pin_token += 1
        token = self._pin_token

        def unpin() -> None:
            if token == self._pin_token:
                self._pinned_top = None
                self._render_alerts()

        self._root.after(int(self._config.alerts.traveled_display_seconds * 1000), unpin)

    def _paint_cell(self, key: str, bg: str, cap_fg: str = DIM) -> None:
        self._stat_cells[key].config(bg=bg)
        self._stat_caps[key].config(bg=bg, fg=cap_fg)
        self._stat_vals[key].config(bg=bg)

    def _set_stats(self, top: AlertView) -> None:
        """Fill the four headline numbers, each coloured by what it means:
        net by sign, yield against the alert threshold, difficulty against
        the reference difficulty of 100. A jackpot paints net and yield in
        its own colour - they are the two numbers that made it one."""
        ratio = net_yield(top.net_div, top.difficulty)
        if top.generic:
            # Price is the only one of the four that means anything here:
            # there is no reference to profit against and no map mods to
            # score. Blanking beats showing three dashes that look like a
            # failed lookup rather than a column that does not apply.
            self._paint_cell(TINTED_STAT, SURFACE)
            self._stat_caps["price"].config(text=f"PRICE · {top.currency.upper()}", fg=DIM)
            self._stat_vals["price"].config(text=f"{top.amount:g}", fg=PRICE)
            for col in ("profit", "ratio", "difficulty"):
                self._stat_caps[col].config(text="")
                self._stat_vals[col].config(text="")
            self._net_unit.config(text="")
            return
        for key, caption in STAT_COLUMNS[1:]:
            self._stat_caps[key].config(text=caption)
        if top.fail_chance > 0:
            # net is an expected value once a failure risk is in play
            self._stat_caps["profit"].config(text="NET · EXPECTED")
        jp = _tier(top)
        self._paint_cell(
            TINTED_STAT,
            jp.yield_bg if jp else YIELD_BG,
            cap_fg=jp.cap if jp else YIELD_CAP,
        )
        self._stat_caps["price"].config(
            text=f"PRICE · {top.currency.upper()}",
            # a mismatched currency is already bannered; flag it here too so
            # the number is never read in the wrong unit
            fg=BAD if top.mismatch else DIM,
        )
        self._stat_vals["price"].config(text=f"{top.amount:g}", fg=BAD if top.mismatch else PRICE)
        self._stat_vals["profit"].config(
            text=f"{top.net_div:+.0f}",
            fg=jp.soft if jp else GOOD if top.net_div > 0 else BAD,
        )
        self._net_unit.config(text="div")
        if ratio is None:
            ratio_text = "?"
        else:  # a decimal matters at 3.8, not at 128
            ratio_text = f"{ratio:+.0f}" if abs(ratio) >= 100 else f"{ratio:+.1f}"
        clears = ratio is not None and ratio >= self._current_threshold
        self._stat_vals["ratio"].config(
            text=ratio_text, fg=jp.soft if jp else YIELD_FG if clears else WARN
        )
        self._stat_vals["difficulty"].config(
            # whole numbers only: the fractional part is noise at this size
            # (the feed's Difficulty column keeps the exact score)
            text=f"{top.difficulty:.0f}",
            fg=BAD if top.difficulty > DIFF_BAD else WARN if top.difficulty > DIFF_WARN else FG,
        )

    def _show_alert_body(
        self,
        chips: bool,
        mods: bool,
        countdown: bool,
        subtitle: bool = False,
        *,
        stash: bool = False,
        jackpot: bool = False,
        mismatch: bool = False,
        setup: bool = False,
        stats: bool = True,
    ) -> None:
        """Pack the card's visible blocks, in _card_blocks order.

        Everything is unpacked and the visible set repacked, rather than
        toggled one by one: emptying a frame is not enough (Tk keeps a
        container's requested height after its children are destroyed), and
        a plain re-pack would append a block to the END of the card.
        """
        show = {
            self._jackpot_banner: jackpot,
            self._banner: mismatch,
            self._title_row: True,
            self._subtitle: subtitle,
            self._stash_row: stash,
            self._setup_panel: setup,
            self._stats: stats,
            self._chips: chips,
            self._pairings: bool(self._pairings.winfo_children()),
            self._risks: bool(self._risks.winfo_children()),
            self._top_mods: mods,
            self._countdown: countdown,
        }
        for widget, _ in self._card_blocks:
            widget.pack_forget()
        for widget, opts in self._card_blocks:
            if show[widget]:
                widget.pack(**opts)

    def _set_stash(self, stash: tuple[str, int, int] | None) -> None:
        """Draw the item's cell in the seller's tab (see _stash_spot)."""
        canvas = self._stash_grid
        canvas.delete("all")
        if stash is None:
            return
        name, x, y = stash
        side, where = _stash_spot(x, y)
        px = STASH_GRID_PX
        cell = px / side
        canvas.create_rectangle(1, 1, px + 1, px + 1, fill=STASH_BG, outline=HAIR)
        for i in (1, 2):  # thirds, matching the words beside it
            at = 1 + round(px * i / 3)
            canvas.create_line(at, 1, at, px + 1, fill=HAIR)
            canvas.create_line(1, at, px + 1, at, fill=HAIR)
        # the marked cell, never smaller than a readable dot on a quad tab
        size = max(cell, 6)
        cx, cy = 1 + (x + 0.5) * cell, 1 + (y + 0.5) * cell
        canvas.create_rectangle(
            cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, fill=GOOD, outline=""
        )
        self._stash_where.config(text=where)
        tab = f"tab “{name}”  ·  " if name else ""
        self._stash_detail.config(text=f"{tab}col {x + 1}, row {y + 1}")

    def _set_age(self, lag_ms: float | None) -> None:
        """Show the head start other snipers had. Green means we saw it
        essentially as it was indexed; red means it had been sitting live
        long enough that it is probably already gone."""
        if lag_ms is None:  # DOM capture: no index time
            self._age_label.config(text="", highlightthickness=0, padx=0)
            return
        if lag_ms <= CLOCK_SKEW_MS:
            self._report_clock_skew(lag_ms)
            color, text = BAD, "PC clock off"
        else:
            # clamp the rounding-noise case too: a -400ms artifact is 0
            lag_ms = max(0.0, lag_ms)
            seconds = lag_ms / 1000
            text = f"{lag_ms:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"
            color = BAD if lag_ms > LAG_BAD_MS else WARN if lag_ms > LAG_OK_MS else GOOD
        self._age_label.config(
            text=text,
            fg=color,
            padx=5,
            highlightthickness=1,
            highlightbackground=BADGE_EDGE[color],
            highlightcolor=BADGE_EDGE[color],
        )

    def _report_clock_skew(self, lag_ms: float) -> None:
        """Log the skew once per run: the readings stay wrong until the clock
        is resynced, and without this the only trace is negative lag numbers
        buried in the listing log."""
        if self._skew_logged:
            return
        self._skew_logged = True
        event("clock_skew", behind_s=round(-lag_ms / 1000, 1), note="latency readings unusable")

    def _set_travel_button(
        self, has_target: bool, pinned: bool, jackpot: _Tier | None = None
    ) -> None:
        """Three states: armed (shows the hotkey as a keycap), already
        traveling, and nothing to travel to. Armed on a jackpot it takes the
        tier's colour like the rest of the card."""
        self._travel_enabled = has_target and not pinned
        self._travel_jackpot = jackpot if self._travel_enabled else None
        self._travel_text.config(text="TRAVELING…" if pinned else "TELEPORT")
        if self._travel_enabled:
            self._travel_key.config(text=self._current_combo)
            self._travel_key.pack(side="left", padx=(0, 10))
        else:
            self._travel_key.pack_forget()
        self._paint_travel(hover=False)

    def _paint_travel(self, hover: bool) -> None:
        if not self._travel_enabled:
            bg, fg, key_bg, key_fg, cursor = BTN_OFF, FAINT, BTN_OFF, FAINT, ""
        elif self._travel_jackpot is not None:
            t = self._travel_jackpot
            bg = t.soft if hover else t.accent
            fg, key_bg, key_fg, cursor = t.btn_fg, t.btn_fg, t.soft, "hand2"
        else:
            bg = GOOD_BRIGHT if hover else GOOD
            fg, key_bg, key_fg, cursor = BTN_FG, BTN_FG, GOOD, "hand2"
        self._travel_btn.config(bg=bg, cursor=cursor)
        self._travel_text.config(bg=bg, fg=fg, cursor=cursor)
        self._travel_key.config(bg=key_bg, fg=key_fg, cursor=cursor)

    def _tabs_clicked(self) -> None:
        if self._tabs_need_install:
            self._open_install()

    def _open_install(self) -> None:
        """Opens the app's own userscript URL; Tampermonkey takes it from
        there. Only ever on a click."""
        self._open_link(self._install_url)

    # ---------------------------------------------------------- self-update

    def _set_update(self, status: UpdateStatus) -> None:
        self._update = status
        text, dot, text_fg = _update_pill(status)
        self._update_pill.config(text=text, fg=dot, text_fg=text_fg)
        if not self._update_pill.frame.winfo_ismapped():
            self._update_pill.pack(side="left", padx=(14, 0))
        if status.state == "installed":
            self._root.after(UPDATED_NOTICE_MS, self._clear_installed_notice)

    def _clear_installed_notice(self) -> None:
        if self._update is not None and self._update.state == "installed":
            self._update = None
            self._update_pill.frame.pack_forget()

    def _update_help(self) -> tuple[str, str] | None:
        if self._update is None:
            return None
        return _update_help(self._update, packaged=paths.FROZEN)

    def _update_clicked(self) -> None:
        status = self._update
        if status is None:
            return
        if status.state == "ready" and self._on_update_install is not None:
            event("update_pill_clicked", version=status.version)
            self._update_pill.config(text=f"↑ v{status.version} · restarting…")
            self._root.update_idletasks()
            self._on_update_install()
            return
        url = status.url or (
            f"https://github.com/{self._config.updates.repo}/releases/tag/v{status.version}"
        )
        self._open_link(url)

    def _open_link(self, url: str) -> None:
        """In Chrome, where the live searches and Tampermonkey are - never
        just the Windows default browser (see sniper/browser.py)."""
        event("open_link", url=url, browser=browser.open_url(url))

    def _render_alerts(self) -> None:
        pinned = self._pinned_top
        top_widgets = self._top_widgets
        self._clear_frame(self._pairings)
        self._clear_frame(self._risks)
        if not self._alerts and pinned is None:
            setup = self._tab_count == 0
            self._key_label.config(
                text="No live searches connected" if setup else self._idle_text(),
                fg=FG if setup else DIM,
            )
            self._paint_cell(TINTED_STAT, SURFACE)
            for key, caption in STAT_COLUMNS:
                self._stat_caps[key].config(text=caption, fg=DIM)
                self._stat_vals[key].config(text="—", fg=FAINT)
            self._net_unit.config(text="")
            self._set_age(None)
            self._set_travel_button(has_target=False, pinned=False)
            self._clear_frame(self._chips)
            self._clear_frame(self._top_mods)
            self._clear_frame(self._runners)
            self._countdown.delete("all")
            self._paint_card(None)
            # collapse the card: four dashes say nothing, so that space goes
            # to the reward prices (or the setup guide) and the history
            self._show_alert_body(
                chips=False, mods=False, countdown=False, setup=setup, stats=False
            )
            if setup:
                self._price_panel.pack_forget()
            else:
                self._render_price_panel()
            for widget in top_widgets:  # nothing to click or hover
                widget.configure(cursor="")
            return
        for widget in top_widgets:
            widget.configure(cursor="hand2")

        # while pinned, the traveled listing holds the top slot and live
        # alerts queue below it
        top = pinned if pinned is not None else self._alerts[0]
        runner_views = self._alerts[:2] if pinned is not None else self._alerts[1:]
        if top.mismatch:
            self._banner.config(
                text=f"⚠  CURRENCY MISMATCH: {top.currency.upper()} ≠ "
                f"ref {top.reference_currency.upper()}"
            )
        tier = _tier(top)
        if tier is not None:
            ratio = net_yield(top.net_div, top.difficulty) or 0.0
            self._jp_right.config(text=f"{top.net_div:+.0f}d net  ·  {ratio:+.0f} yield")
            self._jp_tier = tier
            self._paint_jp_banner()
        self._paint_card(tier)

        prefix = "➜ " if pinned is not None else ""
        self._key_label.config(
            text=f"{prefix}{_display_name(top.key)}", fg=tier.soft if tier else FG
        )
        if top.generic:
            bits = [b for b in (top.item_name, top.seller) if b and b != top.key]
            self._subtitle.config(text="  ·  ".join(bits))
        else:
            self._subtitle.config(text=self._reward_value_text(top))
        self._price_panel.pack_forget()  # the alert names its own reward price
        self._set_stats(top)
        self._set_age(top.index_lag_ms)
        self._set_stash(top.stash)
        self._set_travel_button(has_target=True, pinned=pinned is not None, jackpot=tier)
        for label, note, mult in top.pairings:
            row = tk.Frame(self._pairings, bg=PAIR_BG)
            row.pack(fill="x", pady=(0, 2))
            tk.Label(
                row,
                text=f"☠  {label.upper()}  ×{mult:g}",
                bg=PAIR_BG,
                fg=PAIR_FG,
                font=self._font("ui", 9, "bold"),
                anchor="w",
                padx=8,
                pady=3,
            ).pack(side="left")
            if note:
                tk.Label(
                    row,
                    text=note,
                    bg=PAIR_BG,
                    fg=PAIR_FG,
                    font=self._font("ui", 9),
                    anchor="e",
                    padx=8,
                ).pack(side="right")

        self._render_risks(top)
        # hover help on the rows just built (bound after their labels exist)
        title, body = HELP["pairing"]
        for row, (_label, _note, mult) in zip(
            self._pairings.winfo_children(), top.pairings, strict=True
        ):
            self._bind_help((row,), (title, f"{body} This one: ×{mult:g}."), descendants=True)
        for row in self._risks.winfo_children():
            self._bind_help((row,), HELP["risk"], descendants=True)

        self._clear_frame(self._top_mods)
        self._render_mods(self._top_mods, top.mods, MODS_BG, wrap=360)
        self._clear_frame(self._chips)
        chip_font = self._font("ui", 8, "bold")

        def chip(text: str, fg: str, bg: str = SURFACE, edge: str | None = None) -> None:
            label = tk.Label(
                self._chips,
                text=text,
                bg=bg,
                fg=fg,
                font=chip_font,
                padx=7,
                pady=2,
                highlightthickness=1 if edge else 0,
                highlightbackground=edge or bg,
                highlightcolor=edge or bg,
            )
            label.pack(side="left", padx=(0, 6))
            if text in HELP:
                self._bind_help((label,), HELP[text])

        if top.generic:
            chip("⚡ LIVE SEARCH", FG, bg=BOX)
        if top.reference_source in (*FALLBACK_SOURCES, CACHED_SOURCE, OVERRIDE_SOURCE):
            # this net was NOT computed against a trade average confirmed
            # this session - say so rather than let it read as solid
            text = {
                OVERRIDE_SOURCE: "YOUR PRICE",
                CACHED_SOURCE: "CACHED PRICE",
            }.get(top.reference_source, "ESTIMATED PRICE")
            chip(text, WARN, edge=BADGE_EDGE[WARN])
        for label, color in top.special_warnings:
            if color == "red":
                chip(f"❗ {label.upper()}", "white", bg=BAD_FILL)
            else:
                chip(f"⚠ {label.upper()}", WARN, edge=BADGE_EDGE[WARN])
        for label in top.warn_labels:
            chip(f"⚠ {label}", WARN)
        self._show_alert_body(
            chips=bool(self._chips.winfo_children()),
            mods=bool(top.mods),
            countdown=True,
            subtitle=bool(self._subtitle.cget("text")),
            stash=top.stash is not None,
            jackpot=top.jackpot,
            mismatch=top.mismatch,
        )

        self._clear_frame(self._runners)
        for i, a in enumerate(runner_views):
            self._render_runner(i, a)
        self._draw_countdown()

    def _render_risks(self, top: AlertView) -> None:
        """One amber row per failure risk, then what they cost. The cost is
        already off net (an expected value), so this is the explanation for
        a net that looks lower than price-vs-market would suggest."""
        if not top.risks:
            return
        for label, note, chance in top.risks:
            # label left, chance pinned right, the note on its own line: a
            # long label and a note side by side overflow the card
            row = tk.Frame(self._risks, bg=RISK_BG, pady=3)
            row.pack(fill="x", pady=(0, 2))
            head = tk.Frame(row, bg=RISK_BG)
            head.pack(fill="x")
            tk.Label(
                head,
                text=f"{chance:.0%} FAIL",
                bg=RISK_BG,
                fg=BAD if chance >= 1.0 else RISK_FG,
                font=self._font("ui", 9, "bold"),
                padx=8,
            ).pack(side="right")
            tk.Label(
                head,
                text=f"⚠  {label.upper()}",
                bg=RISK_BG,
                fg=RISK_FG,
                font=self._font("ui", 9, "bold"),
                anchor="w",
                padx=8,
            ).pack(side="left", fill="x", expand=True)
            if note:
                tk.Label(
                    row,
                    text=note,
                    bg=RISK_BG,
                    fg=RISK_FG,
                    font=self._font("ui", 8),
                    anchor="w",
                    padx=26,
                ).pack(fill="x")
        if top.fail_chance >= 1.0:
            summary, color = "Fails every time: you would lose what you pay.", BAD
        else:
            summary = (
                f"Net counts a {top.fail_chance:.0%} chance of failing "
                f"(−{top.risk_cost_div:.0f}d of the reward's value)."
            )
            color = DIM
        tk.Label(
            self._risks,
            text=summary,
            bg=SURFACE,
            fg=color,
            font=self._font("ui", 9),
            anchor="w",
        ).pack(fill="x", pady=(1, 0))

    def _paint_card(self, tier: _Tier | None) -> None:
        """2px outline in the tier's colour for a jackpot, 1px hairline
        otherwise."""
        color = tier.accent if tier else HAIR
        self._card.config(
            highlightthickness=2 if tier else 1,
            highlightbackground=color,
            highlightcolor=color,
        )

    def _render_runner(self, i: int, a) -> None:
        """One runner-up alert as a clickable band.

        These are travel targets, not footnotes: the hotkey only takes #1, so
        anything below it can ONLY be reached by clicking it. A padded band
        with the same numbers the headline shows (yield included, since that
        is what ranked it here), a travel arrow, and a hover lift so the
        click target is unambiguous.
        """
        tier = _tier(a)
        band = tk.Frame(
            self._runners,
            bg=RUNNER_BG,
            highlightthickness=1,
            highlightbackground=tier.accent if tier else HAIR,
            highlightcolor=tier.accent if tier else HAIR,
        )
        band.pack(fill="x", pady=(0, 4))
        ratio = net_yield(a.net_div, a.difficulty)
        price = f"{a.amount:g}d" if a.currency == "divine" else f"{a.amount:g} {a.currency}"
        profit = "?d" if a.net_div is None else f"{a.net_div:+.0f}d"
        per_100 = "?" if ratio is None else f"{ratio:+.1f}"
        # fixed-width columns in the feed's order, so the runners, the feed
        # and the headline can all be read as the same four numbers
        text = tk.Label(
            band,
            text=f"{'★' if a.jackpot else '#'}{i + 2}  {price:<9}{profit:<8}{per_100:<8}"
            f"{a.difficulty:<8g}{_display_name(a.key)}"
            + (f"  ⚠{a.fail_chance:.0%}" if a.fail_chance > 0 else ""),
            bg=RUNNER_BG,
            fg=tier.soft if tier else FG,
            font=self._font("mono", 9),
            anchor="w",
        )
        text.pack(side="left", fill="x", expand=True, padx=(10, 0), pady=5)
        arrow = tk.Label(
            band,
            text="→",
            bg=RUNNER_BG,
            fg=tier.accent if tier else GOOD,
            font=self._font("ui", 11, "bold"),
        )
        arrow.pack(side="right", padx=(6, 10))

        parts = (band, text, arrow)
        # tooltip FIRST: _bind_tooltip takes <Enter>/<Leave> with a plain
        # bind(), which replaces whatever is already on those sequences, so
        # the tint below has to be added after it and with add="+".
        self._bind_tooltip(text, a.mods)
        for widget in parts:
            widget.configure(cursor="hand2")
            widget.bind(
                "<Button-1>",
                lambda e, lid=a.listing_id: self._on_travel and self._on_travel(lid),
            )
            # the pointer sits on a CHILD, so every part repaints the WHOLE
            # band; otherwise the highlight would drop out as the pointer
            # crossed from the text to the arrow
            widget.bind(
                "<Enter>", lambda e, w=parts: self._tint_runner(w, RUNNER_BG_HOVER), add="+"
            )
            widget.bind("<Leave>", lambda e, w=parts: self._tint_runner(w, RUNNER_BG), add="+")

    @staticmethod
    def _tint_runner(widgets, color: str) -> None:
        for widget in widgets:
            widget.configure(bg=color)

    def _draw_countdown(self) -> None:
        self._countdown.delete("all")
        if self._pinned_top is not None:
            total = self._config.alerts.traveled_display_seconds
            remaining = max(0.0, self._pinned_until - time.monotonic())
            tier = _tier(self._pinned_top)
        elif self._alerts:
            total = self._config.alerts.expiry_seconds
            remaining = max(0.0, self._alerts[0].expires_at_monotonic - time.monotonic())
            tier = _tier(self._alerts[0])
        else:
            return
        width = self._countdown.winfo_width() or 465
        frac = remaining / total if total else 0
        color = GOOD if frac > 0.5 else WARN if frac > 0.25 else BAD
        if tier is not None:
            color = tier.accent
        self._countdown.create_rectangle(0, 0, width * frac, 4, fill=color, width=0)

    def _tick(self) -> None:
        self._draw_countdown()
        self._root.after(TICK_MS, self._tick)

    def _refresh_feed_ages(self) -> None:
        """Keep the feed's minutes-ago column current between listings."""
        if self._feed_entries:
            self._render_feed()
        # the price ages next to each reward tick on too
        self._render_watching()
        self._refresh_reward_value()
        self._refresh_price_panel()
        self._root.after(30_000, self._refresh_feed_ages)

    # ---------------------------------------------------------- tuning panel

    def _entry(self, parent: tk.Widget, width: int) -> tk.Entry:
        """Settings-panel input: flat, hairline border that turns green
        while focused."""
        return tk.Entry(
            parent,
            width=width,
            bg=SURFACE,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=self._font("mono", 9),
            highlightthickness=1,
            highlightbackground=HAIR,
            highlightcolor=GOOD,
        )

    def _open_tuning(self) -> None:
        """⚙ panel: edit the profit threshold, hotkey combo, base_default,
        div_per_point, and every rule's min_base/multiplier. Apply swaps the
        live engine (and rebinds the hotkey); Save also persists to
        scoring_overrides.yaml (merged over config.yaml on next start)."""
        if self._tune_window is not None and self._tune_window.winfo_exists():
            self._tune_window.lift()
            return
        win = tk.Toplevel(self._root)
        self._tune_window = win
        win.title("Settings")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.geometry("+460+40")

        # ---- pinned header: search box + the save/error note. Both stay in
        # view however far the list below is scrolled - the note used to sit
        # at the very bottom, out of sight of the field being edited.
        top = tk.Frame(win, bg=STRIP)
        top.pack(side="top", fill="x")
        search_row = tk.Frame(top, bg=STRIP)
        search_row.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(
            search_row,
            text="" if self._glyph is ICON_GLYPHS else "⌕",
            bg=STRIP,
            fg=DIM,
            font=self._font("icon", 10),
        ).pack(side="left", padx=(0, 6))
        search_var = tk.StringVar()
        search_e = tk.Entry(
            search_row,
            textvariable=search_var,
            bg=SURFACE,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=self._font("ui", 10),
            highlightthickness=1,
            highlightbackground=HAIR,
            highlightcolor=GOOD,
        )
        search_e.pack(side="left", fill="x", expand=True, ipady=3)
        clear_btn = tk.Label(
            search_row, text="✕", bg=STRIP, fg=FAINT, font=self._font("ui", 10), cursor="hand2"
        )
        clear_btn.pack(side="left", padx=(6, 0))
        clear_btn.bind("<Button-1>", lambda e: (search_var.set(""), search_e.focus_set()))
        note = tk.Label(top, text="", bg=STRIP, fg=DIM, font=self._font("ui", 9), anchor="w")
        note.pack(fill="x", padx=10, pady=(0, 6))
        tk.Frame(win, bg=HAIR, height=1).pack(side="top", fill="x")

        # the rule list is long: everything else lives in a scrollable body
        canvas = tk.Canvas(win, bg=BG, highlightthickness=0, width=400, height=640)
        scrollbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=BG)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        sc = self._scoring_config
        entries: dict[str, tk.Entry] = {}

        row_cursor = [0]
        # Every section registers its widgets here so the search box can
        # show/hide them: {"title", "head": [...], "rows": [(text, [...])],
        # "captions": [...]}. grid_remove() remembers each widget's grid
        # options, so hidden rows come back exactly where they were.
        sections: list[dict] = []

        def heading(title: str, column_title: str = "") -> None:
            r = row_cursor[0]
            head = [
                tk.Label(body, text=title, bg=BG, fg=FG, font=self._font("semi", 10), anchor="w")
            ]
            head[0].grid(row=r, column=0, sticky="w", padx=(10, 6), pady=(12, 2))
            if column_title:
                col = tk.Label(body, text=column_title, bg=BG, fg=DIM, font=self._font("ui", 8))
                col.grid(row=r, column=1, padx=(2, 10), pady=(12, 2), sticky="w")
                head.append(col)
            sections.append({"title": title.casefold(), "head": head, "rows": [], "captions": []})
            row_cursor[0] += 1

        def field(
            label: str, value: str, width: int = 7, hint: str = "", search: str = ""
        ) -> tk.Entry:
            """One label + entry row in the body grid. `hint` becomes a hover
            tooltip on the label (marked with a trailing ⓘ); it is searched
            too, along with any extra `search` words."""
            r = row_cursor[0]
            name = tk.Label(
                body,
                text=f"{label} ⓘ" if hint else label,
                bg=BG,
                fg=DIM,
                font=self._font("ui", 9),
                anchor="w",
                cursor="question_arrow" if hint else "",
            )
            name.grid(row=r, column=0, sticky="w", padx=(10, 6), pady=1)
            if hint:
                self._bind_tooltip(name, ((hint, "", "none"),))
            entry = self._entry(body, width)
            entry.insert(0, value)
            entry.grid(row=r, column=1, padx=(2, 10), sticky="w")
            sections[-1]["rows"].append((f"{label} {hint} {search}".casefold(), [name, entry]))
            row_cursor[0] += 1
            return entry

        def caption(text: str) -> None:
            label = tk.Label(
                body,
                text=text,
                bg=BG,
                fg=FAINT,
                font=self._font("ui", 8),
                anchor="w",
                justify="left",
            )
            label.grid(row=row_cursor[0], column=0, columnspan=2, sticky="w", padx=10, pady=(2, 0))
            sections[-1]["captions"].append(label)
            row_cursor[0] += 1

        # ---- 1. Alerts: everything that decides WHETHER something alerts
        heading("Alerts", "Value")
        thr_e = field(
            "Alert at YIELD ≥",
            _fmt(self._current_threshold),
            hint="YIELD = net divine per 100 difficulty. A 200-difficulty map\n"
            "must net twice as much as a 100 one to reach the same yield.\n"
            "Highest yield is also what the hotkey travels to.",
            search="threshold min_yield",
        )
        flat_e = field(
            "Running cost (div)",
            _fmt(self._current_running_cost),
            hint="Taken off every listing BEFORE Net and Yield are computed: it\n"
            "accounts for loss on selling and the time spent running the map.\n"
            "Recommended value is 1.",
        )
        jp_net_e = field(
            "Jackpot at NET ≥",
            _fmt(self._jackpot_net),
            hint="A listing is a JACKPOT when its net AND its yield both\n"
            "clear these bars: it gets a magenta banner and frame and\n"
            "becomes the hotkey's top pick.",
        )
        jp_yield_e = field("Jackpot at YIELD ≥", _fmt(self._jackpot_yield), search="jackpot")
        sjp_net_e = field(
            "Super jackpot at NET ≥",
            _fmt(self._super_net),
            hint="The tier above: a jackpot that ALSO clears these bars gets a\n"
            "gold banner and frame, its own alarm, and outranks a jackpot.",
            search="super jackpot",
        )
        sjp_yield_e = field(
            "Super jackpot at YIELD ≥", _fmt(self._super_yield), search="super jackpot"
        )
        caption("Each tier needs both (e.g. +80d net at yield 144).")

        # ---- 2. Hotkey & sound
        heading("Hotkey & sound", "Value")
        combo_e = field("Hotkey combo", self._current_combo, width=16, search="key travel")
        vol_e = field("Alert volume (0 - 1)", _fmt(self._volume), search="sound")

        # ---- 3. Reward prices. A reference price is occasionally just wrong -
        # a thin unid market, a reward whose listings the average lands badly
        # on - and every figure downstream inherits the error, so the user can
        # pin the price they know is right. Session-only on purpose: an old
        # correction outliving the market that justified it would be worse
        # than the original error.
        self._price_entries = {}
        self._price_labels = {}
        heading("Reward prices", "Divine")
        caption("Fix a wrong reference price. Blank = automatic.\nNot saved: cleared on restart.")

        def price_row(entry) -> None:
            r = row_cursor[0]
            name = tk.Label(
                body,
                text=self._price_editor_text(entry),
                bg=BG,
                fg=DIM,
                font=self._font("ui", 9),
                anchor="w",
            )
            name.grid(row=r, column=0, sticky="w", padx=(10, 6), pady=1)
            box = self._entry(body, 7)
            box.insert(0, _fmt(self._price_overrides.get(entry.reward)))
            box.grid(row=r, column=1, padx=(2, 10), sticky="w")
            sections[-1]["rows"].append((_display_name(entry.reward).casefold(), [name, box]))
            row_cursor[0] += 1
            self._price_entries[entry.reward] = box
            self._price_labels[entry.reward] = name

        if self._reward_prices:
            for entry in self._reward_prices:
                price_row(entry)
        else:
            caption("No live searches connected yet.")

        # ---- 4. Failure risks, in percent. A failed map loses what you paid,
        # so these turn net into an expected value.
        risk_entries: dict[str, tk.Entry] = {}
        if sc.risks:
            heading("Failure risks", "Fail %")
            for risk in sc.risks:
                risk_entries[risk.label] = field(
                    risk.label, _fmt(round(risk.fail_chance * 100, 2)), hint=risk.note
                )
            caption(
                "Chance the map fails, losing what you paid for it:\n"
                "net = (1 − fail) × market − price − running cost.\n"
                "100 = never worth buying; 0 = ignore the risk."
            )

        # ---- 5. Deadly pairings: separate dict from the rules, so a pairing
        # named like a rule cannot clobber it
        pair_entries: dict[str, tk.Entry] = {}
        if sc.pairings:
            heading("Deadly pairings", "×")
            for pairing in sc.pairings:
                pair_entries[pairing.label] = field(
                    pairing.label, _fmt(pairing.multiplier), hint=pairing.note
                )
            caption("Mods that are far worse together than apart.")

        # ---- 6/7. Difficulty. Rules split into two exclusive groups: a rule
        # either raises the base difficulty (min_base) or multiplies the final
        # score, never both. Warning-only rules have no number to tune and
        # are not listed. Alphabetical: there are dozens of them.
        def by_name(rule):
            return rule.label.casefold()

        base_rules = sorted((r for r in sc.rules if r.min_base is not None), key=by_name)
        mult_rules = sorted((r for r in sc.rules if r.multiplier is not None), key=by_name)

        def rule_rows(rules, values) -> None:
            for rule, value in zip(rules, values, strict=True):
                entries[rule.label] = field(rule.label, value)

        # base_default belongs here: it is the same kind of number as every
        # min_base, and they combine as max(base_default, matched min_bases).
        heading("Base difficulty", "Base")
        base_e = field("Clean map (no mods)", _fmt(sc.base_default), search="base_default")
        rule_rows(base_rules, [_fmt(r.min_base) for r in base_rules])
        caption("The highest base wins; multipliers then scale it.")

        heading("Difficulty multipliers", "×")
        rule_rows(mult_rules, [_fmt(r.multiplier) for r in mult_rules])

        no_match = tk.Label(body, text="", bg=BG, fg=FAINT, font=self._font("ui", 9), anchor="w")
        no_match.grid(row=row_cursor[0], column=0, columnspan=2, sticky="w", padx=10, pady=12)
        no_match.grid_remove()
        row_cursor[0] += 1

        def apply_search(*_args) -> None:
            """Show only rows whose label (or hover hint) contains the query.
            A query naming a section ("risk", "pairing") shows all of it.
            Captions are help text, so they hide while searching."""
            query = search_var.get().strip().casefold()
            shown = 0
            for section in sections:
                whole = bool(query) and query in section["title"]
                visible = 0
                for text, widgets in section["rows"]:
                    show = not query or whole or query in text
                    for widget in widgets:
                        widget.grid() if show else widget.grid_remove()
                    visible += show
                for widget in section["head"]:
                    widget.grid() if (not query or visible) else widget.grid_remove()
                for widget in section["captions"]:
                    widget.grid() if not query else widget.grid_remove()
                shown += visible
            if query and not shown:
                no_match.config(text=f"No setting matches “{search_var.get().strip()}”.")
                no_match.grid()
            else:
                no_match.grid_remove()
            canvas.yview_moveto(0)

        search_var.trace_add("write", apply_search)
        search_e.bind("<Escape>", lambda e: search_var.set(""))
        search_e.focus_set()

        def collect(save: bool) -> None:
            from dataclasses import replace

            from sniper.config import ModScoringConfig, save_scoring_overrides

            base_labels = {r.label for r in base_rules}
            mult_labels = {r.label for r in mult_rules}
            try:
                new_rules = []
                for rule in sc.rules:
                    if rule.label in base_labels:
                        new_rules.append(replace(rule, min_base=_parse(entries[rule.label].get())))
                    elif rule.label in mult_labels:
                        new_rules.append(
                            replace(rule, multiplier=_parse(entries[rule.label].get()))
                        )
                    else:  # warning-only rules pass through untouched
                        new_rules.append(rule)
                new_config = ModScoringConfig(
                    base_default=float(base_e.get()),
                    rules=tuple(new_rules),
                    pairings=tuple(
                        replace(p, multiplier=float(pair_entries[p.label].get()))
                        if p.label in pair_entries
                        else p
                        for p in sc.pairings
                    ),
                    risks=tuple(
                        replace(r, fail_chance=float(risk_entries[r.label].get()) / 100)
                        if r.label in risk_entries
                        else r
                        for r in sc.risks
                    ),
                )
                new_threshold = float(thr_e.get())
                new_flat = float(flat_e.get())
                new_volume = float(vol_e.get())
                new_jp_net = float(jp_net_e.get())
                new_jp_yield = float(jp_yield_e.get())
                new_sjp_net = float(sjp_net_e.get())
                new_sjp_yield = float(sjp_yield_e.get())
                # start from the pins already in force: a reward whose live
                # search opened after this panel did has no row here, and
                # must not be silently un-pinned by an unrelated edit
                new_overrides = dict(self._price_overrides)
                for reward, box in self._price_entries.items():
                    value = _parse(box.get())
                    if value is None:
                        new_overrides.pop(reward, None)
                    else:
                        new_overrides[reward] = value
            except ValueError as exc:
                note.config(text=f"Bad number: {exc}", fg=BAD)
                return
            bad_price = next((r for r, v in new_overrides.items() if v <= 0), None)
            if bad_price is not None:
                note.config(text=f"{_display_name(bad_price)} price must be > 0", fg=BAD)
                return
            new_combo = combo_e.get().strip()
            if not new_combo:
                note.config(text="Hotkey combo must not be empty", fg=BAD)
                return
            if not 0.0 <= new_volume <= 1.0:
                note.config(text="Volume must be between 0 and 1", fg=BAD)
                return
            bad_risk = next(
                (r.label for r in new_config.risks if not 0.0 <= r.fail_chance <= 1.0), None
            )
            if bad_risk is not None:
                note.config(text=f"{bad_risk}: fail % must be 0-100", fg=BAD)
                return
            if min(new_jp_net, new_jp_yield, new_sjp_net, new_sjp_yield) < 0:
                note.config(text="Jackpot bars must be >= 0", fg=BAD)
                return

            error = None
            if self._on_settings_change:
                error = self._on_settings_change(new_config, new_threshold, new_combo, new_flat)
            if error:  # e.g. invalid hotkey combo - old combo stays active
                note.config(text=error, fg=BAD)
                return
            self._scoring_config = new_config
            self._current_threshold = new_threshold
            self._current_running_cost = new_flat
            self._current_combo = new_combo
            bars = (new_jp_net, new_jp_yield, new_sjp_net, new_sjp_yield)
            current = (self._jackpot_net, self._jackpot_yield, self._super_net, self._super_yield)
            if bars != current:
                self._jackpot_net, self._jackpot_yield, self._super_net, self._super_yield = bars
                if self._on_jackpot_change is not None:
                    self._on_jackpot_change(*bars)
            if new_volume != self._volume:
                self._volume = new_volume
                # rescale off-thread; the cache keys on volume so this both
                # invalidates and re-warms in one step
                threading.Thread(
                    target=self._warm_alert_wavs, name="alert-sound-warmup", daemon=True
                ).start()
                if self._jp_ringing:  # restart at the new volume (or stop at 0)
                    self._jp_ringing = None
                    self._sound.submit(SoundCommand("stop"))
                self._sync_jackpot_alarm()
            # price pins go out per changed reward and are NOT persisted with
            # the rest of the panel - they are a correction for right now
            if new_overrides != self._price_overrides:
                for reward in set(new_overrides) | set(self._price_overrides):
                    before, after = (
                        self._price_overrides.get(reward),
                        new_overrides.get(reward),
                    )
                    if before != after and self._on_price_override is not None:
                        self._on_price_override(reward, after)
                self._price_overrides = new_overrides
            self._update_threshold_note()
            self._render_alerts()  # button label carries the hotkey combo
            if save and self._overrides_path is not None:
                save_scoring_overrides(
                    new_config,
                    self._overrides_path,
                    min_yield=new_threshold,
                    hotkey_combo=new_combo,
                    alert_volume=new_volume,
                    running_cost=new_flat,
                    jackpot_min_net=new_jp_net,
                    jackpot_min_yield=new_jp_yield,
                    super_jackpot_min_net=new_sjp_net,
                    super_jackpot_min_yield=new_sjp_yield,
                )
            # never claim a pinned price was saved: it lives for this session
            # only, and "Saved" would read as a promise the app does not keep
            pinned = len(self._price_overrides)
            pins = f"  ·  {pinned} price{'s' if pinned > 1 else ''} pinned this session"
            note.config(text="Saved ✓" + (pins if pinned else ""), fg=GOOD)

        # settings apply + save automatically: every edit is debounced, then
        # applied live and persisted (invalid intermediate values just show
        # in the note and change nothing until fixed)
        pending: list[str] = []

        def schedule_apply(_event=None) -> None:
            if pending:
                win.after_cancel(pending.pop())
            pending.append(win.after(600, lambda: win.winfo_exists() and collect(save=True)))

        for widget in [
            thr_e,
            flat_e,
            jp_net_e,
            jp_yield_e,
            combo_e,
            base_e,
            vol_e,
            *entries.values(),
            *pair_entries.values(),
            *risk_entries.values(),
            *self._price_entries.values(),
        ]:
            widget.bind("<KeyRelease>", schedule_apply)

        def on_close() -> None:
            # drop the widget references so a price refresh arriving after the
            # panel closes has nothing dead to write into
            self._price_entries = {}
            self._price_labels = {}
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    # ------------------------------------------------------------- live feed

    def _feed_wheel(self, event) -> None:
        """Wheel scrolling over any part of the history area."""
        self._feed_canvas.yview_scroll(int(-event.delta / 120), "units")
        self._flash_scrollbar()

    def _cancel_bar_hide(self) -> None:
        if self._bar_hide_id is not None:
            self._root.after_cancel(self._bar_hide_id)
            self._bar_hide_id = None

    def _flash_scrollbar(self) -> None:
        """Show the overlay scrollbar, then fade it out again after a beat.
        No-op when everything already fits."""
        first, last = self._feed_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        if not self._feed_bar.winfo_ismapped():
            self._feed_bar.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self._cancel_bar_hide()
        self._bar_hide_id = self._root.after(SCROLLBAR_HIDE_MS, self._hide_scrollbar)

    def _hide_scrollbar(self) -> None:
        self._bar_hide_id = None
        self._feed_bar.place_forget()

    def _feed_click(self, idx: int) -> None:
        if self._on_travel is not None and idx < len(self._feed_entries):
            self._on_travel(self._feed_entries[idx].listing_id)

    def _feed_mods(self, idx: int):
        """Hover content for a feed row: the deadly pairings first (they
        decide whether the map is runnable at all), then how far behind we
        saw it, then the mods."""
        if idx >= len(self._feed_entries):
            return ()
        entry = self._feed_entries[idx]
        rows = [
            (f"☠ {label.upper()}  ×{mult:g}" + (f" — {note}" if note else ""), "", "red")
            for label, note, mult in entry.pairings
        ]
        rows += [
            (
                f"⚠ {label.upper()}  {chance:.0%} FAIL" + (f" — {note}" if note else ""),
                "",
                "red" if chance >= 1.0 else "yellow",
            )
            for label, note, chance in entry.risks
        ]
        if entry.index_lag_ms is not None:
            lag = entry.index_lag_ms
            if lag <= CLOCK_SKEW_MS:
                rows.append(
                    (
                        f"latency unusable: this PC's clock is ~{-lag / 1000:.0f}s behind "
                        "the trade site, so every latency reading is off by that much. "
                        "Fix with `w32tm /resync` in an admin prompt.",
                        "",
                        "red",
                    )
                )
            else:
                lag = max(0.0, lag)
                amount = f"{lag:.0f}ms" if lag < 1000 else f"{lag / 1000:.1f}s"
                level = "red" if lag > LAG_BAD_MS else "yellow" if lag > LAG_OK_MS else "none"
                rows.append((f"latency {amount} behind the trade site", "", level))
        return tuple(rows) + self._feed_price_rows(entry) + entry.mods

    def _feed_price_rows(self, entry) -> tuple:
        """What the listing's reward is worth NOW and how old that price is,
        so a listing can still be judged after its alert has gone."""
        if entry.generic:
            return ()
        rp = next((e for e in self._reward_prices if e.reward == entry.key), None)
        if rp is None:
            return ()
        refresh_s = self._config.trade_pricing.refresh_minutes * 60
        price, note, level = _price_status(rp, None, self._live_age(rp), refresh_s)
        mod_level = {"warn": "yellow", "bad": "red"}.get(level, "none")
        return ((f"{_display_name(entry.key)} is worth {price} now  ·  {note}", "", mod_level),)

    def _render_feed(self) -> None:
        """Update the pooled row labels in place (see __init__)."""
        for idx, e in enumerate(self._feed_entries):
            profit = "?d" if e.net_div is None else f"{e.net_div:+.0f}d"
            note = {
                "blocked": "  [blocked]",
                "no_reference": "  [no ref]",
                "no_rate": "  [no rate]",
            }.get(e.verdict, "")
            price = f"{e.amount:g}d" if e.currency == "divine" else f"{e.amount:g} {e.currency}"
            ratio = net_yield(e.net_div, e.difficulty)
            per_100 = "?" if ratio is None else f"{ratio:+.1f}"
            difficulty = f"{e.difficulty:g}"
            if e.generic:
                # these three columns do not apply to a non-Valdo listing;
                # "?" would read as a lookup that failed rather than one
                # that was never attempted
                profit = per_100 = difficulty = "—"
                note = "  [search]"
            if e.fail_chance > 0 and not e.generic:
                note += f"  ⚠{e.fail_chance:.0%} fail"
            tier = _tier(e)
            if tier is not None:
                note = tier.feed_note
            if e.received_monotonic > 0:
                age = f"{max(0, int((time.monotonic() - e.received_monotonic) / 60))}m ago"
            else:
                age = "-"
            row = self._feed_rows[idx]
            row.config(
                text=f"{price:<8}{profit:<9}{per_100:<9}{difficulty:<12}{age:<10}"
                f"{_display_name(e.key)}{note}",
                bg=ZEBRA if idx % 2 else BG,
                # a jackpot keeps its colour for its whole life in the feed,
                # so one that went by unseen is unmistakable minutes later
                fg=tier.accent if tier else FG if e.verdict == "alert" else FAINT,
            )
            if idx >= self._feed_mapped:
                row.pack(fill="x")
                self._feed_mapped = idx + 1

    # ---------------------------------------------------------- mod tooltip

    def _bind_tooltip(self, widget: tk.Widget, mods) -> None:
        """mods: a tuple, or a callable returning one (for persistent widgets
        whose content changes between renders)."""
        provider = mods if callable(mods) else (lambda: mods)
        widget.bind("<Enter>", lambda e: self._show_tooltip(widget, provider()))
        widget.bind("<Leave>", lambda e: self._hide_tooltip())

    def _show_tooltip(self, widget: tk.Widget, mods) -> None:
        """mods: tuple of (mod text, scoring note, level). Mods that carry a
        difficulty modifier are highlighted with the modifier shown beside
        them; neutral mods render dim. No tooltip when there is nothing to
        show (no active alert / no captured mods)."""
        self._hide_tooltip()
        if not mods:
            return
        win = tk.Toplevel(self._root)
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, bg=MODS_BG, highlightthickness=1, highlightbackground="#3a4550")
        frame.pack()
        # same rows as the main alert's mod list: modifier right-aligned and
        # underlined, note sharing the mod's tier colour
        self._render_mods(frame, mods, MODS_BG, wrap=320)
        x = widget.winfo_rootx() + 16
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        win.geometry(f"+{x}+{y}")
        self._tooltip = win

    def _hide_tooltip(self) -> None:
        if self._help_group is not None and self._help_win is not None:
            self._help_win.withdraw()
        self._help_group = None
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    # ------------------------------------------------------------ hover help

    def _bind_help(
        self,
        widgets,
        content,
        anchor=None,
        descendants: bool = False,
        member_of: tk.Widget | None = None,
    ) -> None:
        """Explain a part of the UI on hover, instantly.

        content: (title, body[, live]) or a callable returning one (or None
        for nothing to say right now) - callables let the live line work the
        numbers through for the listing on screen. The widgets are one
        target: moving between them neither hides nor re-places the popup.
        member_of widens that target to everything inside a container, for
        rows that are rebuilt (and re-bound) while the container persists.
        It opens below `anchor` (default: the hovered widget). Bound with
        add="+" so it never displaces a widget's own hover handlers.
        """
        if descendants:  # a row of labels: the pointer is on a child
            found, stack = [], list(widgets)
            while stack:
                widget = stack.pop()
                found.append(widget)
                stack.extend(widget.winfo_children())
            widgets = found
        group = tuple(widgets)
        if member_of is not None:
            key: object = str(member_of)

            def inside(widget) -> bool:  # Tk paths nest: ".!frame3.!label2"
                path = str(widget) if widget is not None else ""
                return path == key or path.startswith(f"{key}.")
        else:
            key = group

            def inside(widget) -> bool:
                return widget in group

        provider = content if callable(content) else (lambda: content)

        def enter(_e, widget) -> None:
            if self._help_group == key:
                return  # already showing for this target
            self._hide_tooltip()
            self._help_group = key
            self._show_help(widget, anchor, provider())

        def leave(e) -> None:
            try:
                under = self._root.winfo_containing(e.x_root, e.y_root)
            except (KeyError, tk.TclError):
                under = None
            if not inside(under):
                self._hide_tooltip()

        for widget in group:
            widget.bind("<Enter>", lambda e, w=widget: enter(e, w), add="+")
            widget.bind("<Leave>", leave, add="+")
            widget.bind("<ButtonPress>", lambda e: self._hide_tooltip(), add="+")

    def _build_help_win(self) -> None:
        """The one help popup, built at startup and only ever re-filled,
        moved, shown and withdrawn: creating a window per hover is what made
        help feel slow."""
        win = tk.Toplevel(self._root)
        win.withdraw()
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(
            win, bg=MODS_BG, highlightthickness=1, highlightbackground="#3a4550", padx=10, pady=7
        )
        frame.pack()
        self._help_title = tk.Label(
            frame, text="", bg=MODS_BG, fg=FG, font=self._font("ui", 9, "bold"), anchor="w"
        )
        self._help_title.pack(fill="x")
        self._help_body = tk.Label(
            frame,
            text="",
            bg=MODS_BG,
            fg=DIM,
            font=self._font("ui", 9),
            justify="left",
            anchor="w",
        )
        self._help_body.pack(fill="x", pady=(2, 0))
        self._help_live = tk.Label(
            frame,
            text="",
            bg=MODS_BG,
            fg=PRICE,
            font=self._font("mono", 9),
            justify="left",
            anchor="w",
        )
        self._help_win = win

    def _show_help(self, widget: tk.Widget, anchor, content) -> None:
        win = self._help_win
        if win is None or not content or not widget.winfo_exists():
            return
        title, body, *rest = content
        live = rest[0] if rest else ""
        wrap = round(HELP_WRAP * self._font_scale)
        self._help_title.config(text=title)
        self._help_body.config(text=body, wraplength=wrap)
        if live:
            self._help_live.config(text=live, wraplength=wrap)
            self._help_live.pack(fill="x", pady=(6, 0))
        else:
            self._help_live.pack_forget()
        below = anchor if anchor is not None and anchor.winfo_exists() else widget
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        # kept inside the overlay's width (whatever monitor it is on), and
        # flipped above the target when it would run off the screen bottom
        left = self._root.winfo_rootx()
        right = left + self._root.winfo_width()
        x = max(left, min(widget.winfo_rootx(), right - w))
        y = below.winfo_rooty() + below.winfo_height() + 2
        if y + h > win.winfo_screenheight():
            y = max(0, widget.winfo_rooty() - h - 2)
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.lift()

    def _stat_help(self, column: str):
        """column: a STAT_COLUMNS key (price / profit / ratio / difficulty)."""
        key = {"profit": "net", "ratio": "yield"}.get(column, column)
        title, body = HELP[key]
        top = self._top_view()
        if top is not None and top.generic and key != "price":
            return None  # the column is blank for a non-Valdo listing
        live = ""
        if top is not None and key == "price" and top.mismatch:
            live = (
                f"Listed in {top.currency}, but the reward is priced in "
                f"{top.reference_currency}: check the number before travelling."
            )
        elif top is not None and key == "net":
            live = _net_breakdown(top, self._current_running_cost)
        elif top is not None and key == "yield":
            live = _yield_breakdown(top.net_div, top.difficulty, self._current_threshold)
        elif top is not None and key == "difficulty":
            live = _difficulty_breakdown(top, self._scoring_config.base_default)
        return (title, body, live)

    def _watching_help(self):
        if not self._watch_names:
            return None
        title, body = HELP["watching"]
        rows = "\n".join(text for text, _note, _level in self._reward_price_rows())
        return (title, body, rows)

    def _travel_help(self):
        return (
            "Teleport",
            "Travel to the seller's hideout, the same as pressing "
            f"{self._current_combo}. One click or press sends exactly one travel; buy "
            "the map from Faustus when you arrive. Clicking the reward name or the "
            "numbers does the same. Only works while Path of Exile is running.",
        )

    def _jackpot_help(self):
        silence = (
            " Click this banner to silence the alarm. Clicking it never teleports; "
            "only the button or the hotkey does."
        )
        if self._jp_tier is SUPER_TIER:
            return (
                "Super jackpot",
                f"A jackpot that also clears the super bars: net ≥ {self._super_net:g}d "
                f"and yield ≥ {self._super_yield:g}. It outranks every other alert for "
                "the hotkey and loops its own alarm." + silence,
            )
        return (
            "Jackpot",
            f"Net ≥ {self._jackpot_net:g}d and yield ≥ {self._jackpot_yield:g}, rare "
            "enough to drop what you are doing. It loops an alarm until you teleport, "
            "it expires, or you silence it." + silence,
        )

    def _countdown_help(self):
        seconds = self._config.alerts.expiry_seconds
        return (
            "Time left",
            f"The alert is dropped {seconds:g}s after it arrived. A listing that old "
            "has usually been bought, and dropping it means a reflex press can never "
            "send you to a dead one.",
        )

    # --------------------------------------------------------- traveled panel

    # ----------------------------------------------------------------- sound

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._mute_btn.config(
            text=self._glyph["mute" if self._muted else "volume"], fg=BAD if self._muted else DIM
        )
        event("mute_toggled", muted=self._muted)
        self._sync_jackpot_alarm()  # mute stops a ringing alarm; unmute resumes it

    def _silence_jackpot(self) -> None:
        """Banner click: stop the alarm for every jackpot on screen now. A
        later, different jackpot still rings. Only touches sound."""
        live = jackpots_to_ring(self._alerts, self._jp_silenced)
        if not live:
            return
        self._jp_silenced |= live
        event("jackpot_silenced", listing_ids=sorted(live))
        self._sync_jackpot_alarm()

    def _sync_jackpot_alarm(self) -> None:
        """Start or stop the looping alarm to match what is on screen, and
        show the 'click to silence' hint only while it rings."""
        live_ids = {a.listing_id for a in self._alerts}
        self._jp_silenced &= live_ids  # forget listings that are gone
        enabled = (
            self._config.alerts.jackpot_alarm
            and not self._muted
            and self._volume > 0
            and winsound is not None
        )
        want = alarm_kind(self._alerts, self._jp_silenced) if enabled else None
        if want != self._jp_ringing:
            self._jp_ringing = want
            # a super jackpot arriving over a ringing jackpot swaps the alarm
            self._sound.submit(SoundCommand(want, loop=True) if want else SoundCommand("stop"))
        self._paint_jp_banner()

    def _paint_jp_banner(self) -> None:
        """Banner in the top alert's tier colour, with the 'click to
        silence' hint only while an alarm rings."""
        t = self._jp_tier
        ringing = self._jp_ringing is not None
        self._jackpot_banner.config(bg=t.banner)
        self._jp_left.config(
            text=f"{t.title}  ·  click to silence" if ringing else t.title, bg=t.banner
        )
        self._jp_right.config(bg=t.banner)
        for widget in (self._jackpot_banner, self._jp_left, self._jp_right):
            widget.configure(cursor="hand2" if ringing else "")

    # ----------------------------------------------------------------- sound

    def _alert_wav(self, kind: str = "valdo") -> str | None:
        """Path to the volume-scaled WAV for this alert kind + volume.

        Cached: rescaling a ~240 KB WAV on every alert would be wasted work,
        and every kind is warmed at startup so no alert pays for it. None
        means no WAV was found - the caller uses the (unscalable) system
        alias instead.
        """
        alerts = self._config.alerts
        configured, default = {
            "generic": (alerts.generic_sound, GENERIC_WAV),
            "jackpot": (alerts.jackpot_sound, JACKPOT_WAV),
            "super": (alerts.super_jackpot_sound, SUPER_JACKPOT_WAV),
        }.get(kind, (alerts.sound, None))
        volume = self._volume
        # `kind` belongs in the key even though `configured` usually
        # differs: with the settings left empty the paths are the SAME
        # empty string, and keying on it alone made the generic alert replay
        # the Valdo sound - the one thing a separate sound exists to prevent.
        key = (kind, configured, round(volume, 3))
        if key not in self._wav_cache:
            self._wav_cache[key] = alert_wav_path(configured, volume, default)
        return self._wav_cache[key]

    def _warm_alert_wavs(self) -> None:
        """Rescale every alert sound off the UI thread. Called at startup and
        after a volume change, which invalidates every cached key."""
        for kind in ("valdo", "generic", "jackpot", "super"):
            self._alert_wav(kind)

    def _play_sound(self, kind: str = "valdo") -> None:
        """kind: "valdo" | "generic"."""
        if self._muted or self._volume <= 0:
            return
        if winsound is None:
            self._root.bell()
            return
        self._sound.submit(SoundCommand(kind))

    def _sound_backend(self, command: SoundCommand) -> None:
        """Runs on the SoundWorker thread. PlaySound loads the audio
        synchronously even with SND_ASYNC (~100-500ms when interrupting a
        playing sound), so it must never run on the UI thread - burst alerts
        would delay rendering."""
        if winsound is None:
            return
        if command.kind == "stop":
            winsound.PlaySound(None, 0)  # stops any async/looping sound
            return
        loop = winsound.SND_LOOP if command.loop else 0  # SND_LOOP needs SND_ASYNC
        path = self._alert_wav(command.kind)
        if path is not None:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | loop)
        else:  # no WAV available: alias playback ignores volume
            winsound.PlaySound(
                "SystemExclamation",
                winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT | loop,
            )
