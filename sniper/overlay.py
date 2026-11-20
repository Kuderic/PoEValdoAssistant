"""Always-on-top tkinter overlay. Runs on the MAIN thread only; all state
arrives as immutable events from the bus. The asyncio thread wakes the
drain via a <<BusWake>> Tk event the instant it publishes (a DRAIN_MS
root.after poll remains as safety net), so the decision -> render delay is
event-driven, not poll-quantized.

Latency contract: sound + render happen inside the same drain pass that
delivers AlertsChanged(new_alert=True); the frame->UI delta is logged as
`alert_shown` for the <100 ms acceptance check.
"""

from __future__ import annotations

import contextlib
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from tkinter import font as tkfont

from sniper import attention
from sniper.alerts import AlertView
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
    WarmupStatus,
)
from sniper.config import Config
from sniper.logging_setup import event
from sniper.margin import net_yield
from sniper.server import EXPECTED_USERSCRIPT_VERSION, INSTALL_PATH
from sniper.sound import GENERIC_WAV, JACKPOT_WAV, SoundCommand, SoundWorker, alert_wav_path

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
    ("profit", "NET DIV"),
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
# Hover help for the latency readout, as a _render_mods row triple.
LATENCY_HELP = (
    "Latency: how long this listing had already been live before it "
    "reached you — the head start every other sniper had on it. Measured "
    "from the trade site's own index time. Low is worth racing; several "
    "seconds means it is probably already gone.",
    "",
    "none",
)
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


def _display_name(key: str) -> str:
    """Reward keys keep their 'Foil ' prefix internally; the UI drops it."""
    return key.removeprefix("Foil ")


# Palette - design A, "Refined". One slate ground, one surface for the alert
# card, 1px hairlines instead of boxes. Colour is kept for meaning only:
# green = good, amber = warning/estimated, red = danger, magenta = jackpot.
BG = "#0f1216"
STRIP = "#0b0e11"  # status strip and mod panel: one step darker than the ground
SURFACE = "#161b21"  # the alert card and runner-up rows
HAIR = "#262e38"  # dividers and card outlines
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
JACKPOT_DEEP = "#6e2a8c"  # the banner pulses between the two
JACKPOT_YIELD_BG = "#2c1838"
JACKPOT_BTN_FG = "#1c0626"
JACKPOT_PULSE_MS = 350
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

    def config(self, text: str | None = None, fg: str | None = None) -> None:
        if text is not None:
            self._label.config(text=text)
        if fg is not None:
            self._dot.config(bg=fg)
            self._label.config(fg=fg if fg in (BAD, WARN) else DIM)

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
        # Callable[[float, float], None]: jackpot bars (min net, min yield)
        on_jackpot_change=None,
        overrides_path=None,  # Path for scoring_overrides.yaml persistence
    ):
        self._root = root
        self._bus = bus
        self._config = config
        self._on_travel = on_travel
        self._on_settings_change = on_settings_change
        self._on_price_override = on_price_override
        self._on_jackpot_change = on_jackpot_change
        self._jackpot_net = config.alerts.jackpot_min_net
        self._jackpot_yield = config.alerts.jackpot_min_yield
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
        self._alerts: tuple[AlertView, ...] = ()
        self._muted = False
        self._volume = config.alerts.volume
        # (kind, sound path, volume) -> playable WAV | None; see _alert_wav.
        # A DICT, not a single slot: the alert kinds alternate, and a
        # one-entry cache would rescale a ~240 KB WAV on the alert path
        # every time the kind changed - exactly what the warm-up prevents.
        self._wav_cache: dict[tuple[str, str, float], str | None] = {}
        self._sound = SoundWorker(
            self._sound_backend,
            on_error=lambda e: event("alert_sound_error", error=repr(e)),
        )
        # A jackpot is live: banner pulsing, alarm looping, window pulled
        # forward. Ends the moment no live alert is a jackpot any more
        # (consumed, expired, travel pinned) - see _sync_jackpot.
        self._jackpot_active = False
        self._pulse_token = 0
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
        self._bind_tooltip(self._searches_label, self._reward_price_rows)

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
        self._travel_jackpot = False
        for widget in (self._travel_btn, self._travel_text, self._travel_key):
            widget.bind("<Button-1>", lambda e: self._travel_enabled and self._click_top())
            widget.bind("<Enter>", lambda e: self._paint_travel(hover=True))
            widget.bind("<Leave>", lambda e: self._paint_travel(hover=False))
        self._travel_btn.pack(side="right", padx=(10, 0))
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
        self._bind_tooltip(self._age_label, (LATENCY_HELP,))
        self._set_travel_button(has_target=False, pinned=False)  # nothing yet at startup

        # Identity line for a generic (non-Valdo) alert: a rare's own name
        # and the seller. Valdo alerts do not need it - the reward IS the
        # identity.
        self._subtitle = tk.Label(
            card, text="", bg=SURFACE, fg=DIM, font=self._font("ui", 10), anchor="w"
        )

        # First-run guide: shown in place of the numbers while no live-search
        # tab is connected. Both rows act on a click only.
        self._setup_panel = tk.Frame(card, bg=SURFACE)
        for num, text, action in (
            ("1", "Install the browser script", self._open_install),
            ("2", "Open a Valdo live search on the trade site", lambda: webbrowser.open(TRADE_URL)),
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
            text="Needs Chrome + Tampermonkey with “Allow User Scripts” on.\n"
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
        last = len(STAT_COLUMNS) - 1
        for col, (key, caption) in enumerate(STAT_COLUMNS):
            self._stats.grid_columnconfigure(col, weight=1, uniform="stat")
            cell = tk.Frame(self._stats, bg=SURFACE)
            cell.grid(row=0, column=col, sticky="nsew", padx=(0, 0 if col == last else 1), pady=1)
            cap = tk.Label(
                cell, text=caption, bg=SURFACE, fg=DIM, font=self._font("ui", 7, "bold"), anchor="w"
            )
            cap.pack(fill="x", padx=12, pady=(8, 0))
            val = tk.Label(
                cell, text="", bg=SURFACE, fg=PRICE, font=self._font("num", 21), anchor="w"
            )
            val.pack(fill="x", padx=12, pady=(0, 6))
            self._stat_cells[key] = cell
            self._stat_caps[key] = cap
            self._stat_vals[key] = val

        self._chips = tk.Frame(card, bg=SURFACE)  # warning chips
        # Deadly mod pairings get their own full-width rows rather than a
        # chip: the note ("Impossible unless DPS-check build") is the part
        # worth reading, and it does not fit in a chip.
        self._pairings = tk.Frame(card, bg=SURFACE)
        # the top listing's mods, always visible (no hover needed)
        self._top_mods = tk.Frame(card, bg=MODS_BG, pady=6)
        self._countdown = tk.Canvas(card, height=4, bg=COUNTDOWN_BG, highlightthickness=0)

        self._card_blocks = (
            (self._jackpot_banner, {"fill": "x"}),
            (self._banner, {"fill": "x"}),
            (self._title_row, {"fill": "x", "padx": 12, "pady": (10, 8)}),
            (self._subtitle, {"fill": "x", "padx": 12, "pady": (0, 6)}),
            (self._setup_panel, {"fill": "x", "padx": 12, "pady": (0, 10)}),
            (self._stats, {"fill": "x"}),
            (self._chips, {"fill": "x", "padx": 12, "pady": (8, 0)}),
            (self._pairings, {"fill": "x", "padx": 12, "pady": (6, 0)}),
            (self._top_mods, {"fill": "x", "padx": 12, "pady": (8, 0)}),
            (self._countdown, {"fill": "x", "padx": 12, "pady": (10, 12)}),
        )

        # Startup pricing progress: which reward is being fetched, which are
        # done and at what price. Only visible while pricing is busy.
        self._warmup_panel = tk.Frame(root, bg=MODS_BG, pady=4)

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
        tk.Label(
            feed_box,
            # yield = net per 100 difficulty: the same unit as the threshold
            # noted directly above, and the alert ranking key
            text=f"{'PRICE':<8}{'NET':<9}{'YIELD':<9}{'DIFF':<12}{'AGE':<10}REWARD",
            bg=BG,
            fg=DIM,
            font=self._font("mono", 8, "bold"),
            anchor="w",
            padx=6,
        ).pack(fill="x")
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
        )
        self._top_widgets = clickable
        for widget in clickable:
            widget.bind("<Button-1>", lambda e: self._click_top())
            widget.configure(cursor="hand2")
        # No hover tooltip on the headline: the top alert already renders its
        # full mod list inline in the dark panel below, so the popup only
        # repeated it - directly on top of the numbers being read.

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
        bus.set_waker(self._wake)
        root.after(DRAIN_MS, self._drain)
        root.after(TICK_MS, self._tick)
        root.after(30_000, self._refresh_feed_ages)

    # ------------------------------------------------------------------ bus

    def _wake(self) -> None:
        """Bus waker, called from the asyncio thread after every put.
        event_generate is safe cross-thread (thread-enabled Tcl queues it);
        when the window is tearing down the fallback poll covers the rest."""
        with contextlib.suppress(tk.TclError, RuntimeError):
            self._root.event_generate("<<BusWake>>", when="tail")

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
                if old:
                    # clicking the pill reinstalls; Tampermonkey also picks
                    # the new version up by itself via @updateURL
                    text += " · update script"
                    event("userscript_outdated", running=old, expected=EXPECTED_USERSCRIPT_VERSION)
                self._tabs_label.config(
                    text=text, fg=BAD if old else WARN if stale else GOOD if n else BAD
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
                parts = []
                if rewards:
                    parts.append(", ".join(rewards))
                if generic:
                    parts.append(f"⚡{generic} live search" + ("es" if generic > 1 else ""))
                if unknown and n:  # tabs whose reward is not yet known
                    parts.append(f"+{unknown} unidentified")
                self._searches_label.config(text=f"Watching  {' · '.join(parts)}" if parts else "")
            elif isinstance(ev, PriceStatus):
                self._ninja_status = ev.status
                self._render_price_pill()
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
                self._refresh_price_editor()
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
        if new_alert:  # one sound per batch: SND_ASYNC bursts cancel anyway
            if any(v.jackpot for v in new_views):
                self._start_jackpot(next(v for v in new_views if v.jackpot))
            elif self._jackpot_active:
                # any other sound would cancel the looping alarm, silencing
                # the best alert on screen to announce a lesser one
                pass
            else:
                # A batch carrying a generic hit plays the generic sound:
                # those are the rare, hand-picked ones, so if a batch mixes
                # kinds the one worth interrupting for wins.
                self._play_sound("generic" if any(v.generic for v in new_views) else "valdo")
        if alerts_dirty:
            self._sync_jackpot()

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
            note = _ago(entry.age_s)
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
            self._price_label.config(text="Trade prices", fg=GOOD)
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

    def _render_warmup_panel(self) -> None:
        """Show what is actually being priced, not just a counter: each
        active reward with its state and whatever price we have so far.
        Visible whenever pricing is working - startup, a scheduled re-price,
        or a newly opened live search - and hidden when it is idle."""
        rows = self._warmup[3] if self._warmup else ()
        if not rows or not self._pricing_is_busy():
            self._warmup_panel.pack_forget()
            return
        self._clear_frame(self._warmup_panel)
        for reward, state, price, age in rows:
            label, fg = {
                "done": ("", GOOD),
                "working": ("fetching…", WARN),
                "pending": ("queued", FAINT),
                "unpriced": ("no listings", DIM),
            }.get(state, ("", DIM))
            if price and _ago(age):
                # How old the shown figure is. Needed most while a reward is
                # being re-fetched: that is exactly when you are looking at a
                # stale number, and an app reopened after five hours shows
                # its restored prices as "5h ago" rather than looking current.
                price = f"{price}  ·  {_ago(age)}"
            if state == "working" and price:
                # a re-price keeps the old figure visible, marked as in flux
                price = f"↻ {price}"
            line = tk.Frame(self._warmup_panel, bg=MODS_BG)
            line.pack(fill="x", padx=6, pady=1)
            tk.Label(
                line,
                text=price or label,
                bg=MODS_BG,
                fg=fg,
                font=self._font("mono", 10, "bold" if state == "done" else "normal"),
                anchor="e",
            ).pack(side="right", padx=(10, 0))
            tk.Label(
                line,
                text=_display_name(reward),
                bg=MODS_BG,
                fg=FG if state in ("done", "working") else DIM,
                font=self._font("mono", 10),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
        self._warmup_panel.pack(fill="x", padx=12, pady=(6, 0), before=self._runners)

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
            return
        for key, caption in STAT_COLUMNS[1:]:
            self._stat_caps[key].config(text=caption)
        jp = top.jackpot
        self._paint_cell(
            TINTED_STAT,
            JACKPOT_YIELD_BG if jp else YIELD_BG,
            cap_fg="#d9b8e8" if jp else YIELD_CAP,
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
            fg=JACKPOT_SOFT if jp else GOOD if top.net_div > 0 else BAD,
        )
        if ratio is None:
            ratio_text = "?"
        else:  # a decimal matters at 3.8, not at 128
            ratio_text = f"{ratio:+.0f}" if abs(ratio) >= 100 else f"{ratio:+.1f}"
        clears = ratio is not None and ratio >= self._current_threshold
        self._stat_vals["ratio"].config(
            text=ratio_text, fg=JACKPOT_SOFT if jp else YIELD_FG if clears else WARN
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
            self._setup_panel: setup,
            self._stats: stats,
            self._chips: chips,
            self._pairings: bool(self._pairings.winfo_children()),
            self._top_mods: mods,
            self._countdown: countdown,
        }
        for widget, _ in self._card_blocks:
            widget.pack_forget()
        for widget, opts in self._card_blocks:
            if show[widget]:
                widget.pack(**opts)

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

    def _set_travel_button(self, has_target: bool, pinned: bool, jackpot: bool = False) -> None:
        """Three states: armed (shows the hotkey as a keycap), already
        traveling, and nothing to travel to. Armed on a jackpot it turns
        magenta like the rest of the card."""
        self._travel_enabled = has_target and not pinned
        self._travel_jackpot = jackpot and self._travel_enabled
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
        elif self._travel_jackpot:
            bg = JACKPOT_SOFT if hover else JACKPOT
            fg, key_bg, key_fg, cursor = JACKPOT_BTN_FG, JACKPOT_BTN_FG, JACKPOT_SOFT, "hand2"
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
        event("open_install", url=self._install_url)
        webbrowser.open(self._install_url)

    def _render_alerts(self) -> None:
        pinned = self._pinned_top
        top_widgets = self._top_widgets
        self._clear_frame(self._pairings)
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
            self._set_age(None)
            self._set_travel_button(has_target=False, pinned=False)
            self._clear_frame(self._chips)
            self._clear_frame(self._top_mods)
            self._clear_frame(self._runners)
            self._countdown.delete("all")
            self._paint_card(jackpot=False)
            # collapse the card: with nothing to show, that space belongs to
            # the history list below (or the warm-up panel / setup guide)
            self._show_alert_body(
                chips=False, mods=False, countdown=False, setup=setup, stats=not setup
            )
            self._render_warmup_panel()
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
        if top.jackpot:
            ratio = net_yield(top.net_div, top.difficulty) or 0.0
            self._jp_right.config(text=f"{top.net_div:+.0f}d net  ·  {ratio:+.0f} yield")
        self._paint_card(jackpot=top.jackpot)

        prefix = "➜ " if pinned is not None else ""
        self._key_label.config(
            text=f"{prefix}{_display_name(top.key)}", fg=JACKPOT_SOFT if top.jackpot else FG
        )
        if top.generic:
            bits = [b for b in (top.item_name, top.seller) if b and b != top.key]
            self._subtitle.config(text="  ·  ".join(bits))
        self._warmup_panel.pack_forget()  # a live alert outranks the progress list
        self._set_stats(top)
        self._set_age(top.index_lag_ms)
        self._set_travel_button(has_target=True, pinned=pinned is not None, jackpot=top.jackpot)
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

        self._clear_frame(self._top_mods)
        self._render_mods(self._top_mods, top.mods, MODS_BG, wrap=360)
        self._clear_frame(self._chips)
        chip_font = self._font("ui", 8, "bold")

        def chip(text: str, fg: str, bg: str = SURFACE, edge: str | None = None) -> None:
            tk.Label(
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
            ).pack(side="left", padx=(0, 6))

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
            subtitle=top.generic,
            jackpot=top.jackpot,
            mismatch=top.mismatch,
        )

        self._clear_frame(self._runners)
        for i, a in enumerate(runner_views):
            self._render_runner(i, a)
        self._draw_countdown()

    def _paint_card(self, jackpot: bool) -> None:
        """2px magenta outline for a jackpot, 1px hairline otherwise."""
        color = JACKPOT if jackpot else HAIR
        self._card.config(
            highlightthickness=2 if jackpot else 1,
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
        band = tk.Frame(
            self._runners,
            bg=RUNNER_BG,
            highlightthickness=1,
            highlightbackground=JACKPOT if a.jackpot else HAIR,
            highlightcolor=JACKPOT if a.jackpot else HAIR,
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
            f"{a.difficulty:<8g}{_display_name(a.key)}",
            bg=RUNNER_BG,
            fg=JACKPOT_SOFT if a.jackpot else FG,
            font=self._font("mono", 9),
            anchor="w",
        )
        text.pack(side="left", fill="x", expand=True, padx=(10, 0), pady=5)
        arrow = tk.Label(
            band,
            text="→",
            bg=RUNNER_BG,
            fg=JACKPOT if a.jackpot else GOOD,
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
            jackpot = self._pinned_top.jackpot
        elif self._alerts:
            total = self._config.alerts.expiry_seconds
            remaining = max(0.0, self._alerts[0].expires_at_monotonic - time.monotonic())
            jackpot = self._alerts[0].jackpot
        else:
            return
        width = self._countdown.winfo_width() or 465
        frac = remaining / total if total else 0
        color = GOOD if frac > 0.5 else WARN if frac > 0.25 else BAD
        if jackpot:
            color = JACKPOT
        self._countdown.create_rectangle(0, 0, width * frac, 4, fill=color, width=0)

    def _tick(self) -> None:
        self._draw_countdown()
        self._root.after(TICK_MS, self._tick)

    def _refresh_feed_ages(self) -> None:
        """Keep the feed's minutes-ago column current between listings."""
        if self._feed_entries:
            self._render_feed()
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

        # the rule list is long: everything lives in a scrollable body
        canvas = tk.Canvas(win, bg=BG, highlightthickness=0, width=400, height=680)
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

        def heading(title: str, column_title: str = "") -> None:
            r = row_cursor[0]
            tk.Label(body, text=title, bg=BG, fg=FG, font=self._font("semi", 10), anchor="w").grid(
                row=r, column=0, sticky="w", padx=(10, 6), pady=(10, 1)
            )
            if column_title:
                tk.Label(body, text=column_title, bg=BG, fg=FG, font=self._font("semi", 10)).grid(
                    row=r, column=1, padx=(2, 10), pady=(10, 1)
                )
            row_cursor[0] += 1

        def field(label: str, value: str, width: int = 7, hint: str = "") -> tk.Entry:
            """One label + entry row in the body grid. `hint` becomes a hover
            tooltip on the label (marked with a trailing ⓘ)."""
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
            row_cursor[0] += 1
            return entry

        def caption(text: str) -> None:
            tk.Label(
                body,
                text=text,
                bg=BG,
                fg=DIM,
                font=self._font("ui", 9),
                anchor="w",
                justify="left",
            ).grid(row=row_cursor[0], column=0, columnspan=2, sticky="w", padx=10, pady=(2, 0))
            row_cursor[0] += 1

        # --- alerting ------------------------------------------------------
        # The threshold IS a yield value: alert when profit/difficulty*100
        # reaches it, which is exactly the feed's Yield column.
        heading("Alerting", "Value")
        thr_e = field("Alert at YIELD ≥", _fmt(self._current_threshold))
        flat_e = field(
            "Running cost",
            _fmt(self._current_running_cost),
            hint="Taken off every listing BEFORE Net and Yield are computed: it\n"
            "accounts for loss on selling and the time spent running the map.\n"
            "Recommended value is 1.",
        )
        caption(
            "YIELD = NET divine per 100 difficulty (feed column 3).\n"
            "Net is what is left after the running cost, so yield is\n"
            "already time-adjusted: what the map pays per unit of pain,\n"
            "not its headline profit.\n"
            "A clean 25-difficulty map at yield 14 nets ~3.5d; a\n"
            "200-difficulty map must net twice as much to match.\n"
            "Highest yield is also what the hotkey travels to."
        )

        heading("Jackpot", "Value")
        jp_net_e = field(
            "Jackpot NET ≥",
            _fmt(self._jackpot_net),
            hint="A listing is a JACKPOT when its net AND its yield both\n"
            "clear these bars: magenta banner, looping alarm, overlay\n"
            "pulled to the front, hotkey's top pick.",
        )
        jp_yield_e = field("Jackpot YIELD ≥", _fmt(self._jackpot_yield))
        caption("Both must be met (e.g. +80d net at yield 144).\nApplies to new listings.")

        heading("Controls")
        combo_e = field("Hotkey combo", self._current_combo, width=16)
        vol_e = field("Alert volume (0 - 1)", _fmt(self._volume))

        # --- reward prices ---------------------------------------------------
        # A reference price is occasionally just wrong - a thin unid market, a
        # reward whose listings the average lands badly on - and every profit
        # figure downstream inherits the error. Rather than leave a known-bad
        # number driving alerts, the user can pin the price they know is
        # right. Session-only on purpose: an old correction outliving the
        # market that justified it would be worse than the original error.
        self._price_entries = {}
        self._price_labels = {}
        heading("Reward prices", "Divine")
        caption(
            "Fix a reference price that is wrong. Blank = automatic.\n"
            "Applies to new listings; not saved, cleared on restart."
        )

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
            row_cursor[0] += 1
            self._price_entries[entry.reward] = box
            self._price_labels[entry.reward] = name

        if self._reward_prices:
            for entry in self._reward_prices:
                price_row(entry)
        else:
            caption("No live searches connected yet.")

        # Rules split into two exclusive groups: a rule either raises the base
        # difficulty (min_base) or multiplies the final score (multiplier),
        # never both. Warning-only rules (bismuth/blight) have no number to
        # tune and are not listed.
        base_rules = [r for r in sc.rules if r.min_base is not None]
        mult_rules = [r for r in sc.rules if r.multiplier is not None]

        def rule_rows(rules, values) -> None:
            for rule, value in zip(rules, values, strict=True):
                entries[rule.label] = field(rule.label, value)

        # base_default belongs here, not in a separate block: it is the same
        # kind of number as every min_base below it, and they combine as
        # max(base_default, matched min_bases).
        heading("Base difficulty", "Base")
        base_e = field("Clean map (no mods)", _fmt(sc.base_default))
        rule_rows(base_rules, [_fmt(r.min_base) for r in base_rules])
        caption("The highest base wins; multipliers below then scale it.")

        heading("Difficulty multipliers", "Mult")
        rule_rows(mult_rules, [_fmt(r.multiplier) for r in mult_rules])

        # pairings live in their own dict: they share the entry-by-label
        # mechanism but are a separate config section, so a pairing named
        # like a rule cannot clobber it
        pair_entries: dict[str, tk.Entry] = {}
        if sc.pairings:
            heading("Deadly pairings", "Mult")
            caption("Mods that are far worse together than apart.")
            for pairing in sc.pairings:
                pair_entries[pairing.label] = field(
                    pairing.label, _fmt(pairing.multiplier), hint=pairing.note
                )

        note = tk.Label(body, text="", bg=BG, fg=BAD, font=self._font("ui", 9))
        note.grid(row=row_cursor[0], column=0, columnspan=2, pady=(6, 4))
        row_cursor[0] += 1

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
                )
                new_threshold = float(thr_e.get())
                new_flat = float(flat_e.get())
                new_volume = float(vol_e.get())
                new_jp_net = float(jp_net_e.get())
                new_jp_yield = float(jp_yield_e.get())
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
            if new_jp_net < 0 or new_jp_yield < 0:
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
            if (new_jp_net, new_jp_yield) != (self._jackpot_net, self._jackpot_yield):
                self._jackpot_net, self._jackpot_yield = new_jp_net, new_jp_yield
                if self._on_jackpot_change is not None:
                    self._on_jackpot_change(new_jp_net, new_jp_yield)
            if new_volume != self._volume:
                self._volume = new_volume
                # rescale off-thread; the cache keys on volume so this both
                # invalidates and re-warms in one step
                threading.Thread(
                    target=self._warm_alert_wavs, name="alert-sound-warmup", daemon=True
                ).start()
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
        return tuple(rows) + entry.mods

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
            if e.jackpot:
                note = "  ★ JACKPOT"
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
                fg=JACKPOT if e.jackpot else FG if e.verdict == "alert" else FAINT,
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
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    # --------------------------------------------------------- traveled panel

    # ----------------------------------------------------------------- sound

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._mute_btn.config(
            text=self._glyph["mute" if self._muted else "volume"], fg=BAD if self._muted else DIM
        )
        event("mute_toggled", muted=self._muted)
        # mute is also the way to silence a ringing jackpot without acting on
        # it; unmuting while one is still live brings the alarm back
        if self._jackpot_active:
            if self._muted:
                self._sound.submit(SoundCommand("stop"))
            else:
                self._play_sound("jackpot")

    # --------------------------------------------------------------- jackpot

    def _start_jackpot(self, view: AlertView) -> None:
        """A jackpot just arrived: alarm on loop, overlay forward, banner
        pulsing. Re-entrant - a second jackpot simply restarts the alarm."""
        event("jackpot_alert", listing_id=view.listing_id, key=view.key, net_div=view.net_div)
        self._play_sound("jackpot")
        self._root.title(f"★ JACKPOT ★  {TITLE}")
        try:
            attention.bring_to_front(attention.frame_hwnd(self._root))
        except Exception as e:  # best-effort: never let this break alerting
            event("bring_to_front_error", error=repr(e))
        if not self._jackpot_active:
            self._jackpot_active = True
            self._pulse_token += 1
            self._pulse(self._pulse_token, on=False)

    def _sync_jackpot(self) -> None:
        """End the jackpot state once no live alert is one any more: it was
        consumed (hotkey/click), expired, or cleared by a press. The pinned
        traveled view does not count - the alarm's job is done once you
        have acted on it."""
        if not self._jackpot_active or any(a.jackpot for a in self._alerts):
            return
        self._jackpot_active = False
        self._pulse_token += 1  # cancels the pending pulse callback
        self._paint_jackpot_banner(JACKPOT_BANNER)
        self._sound.submit(SoundCommand("stop"))
        self._root.title(TITLE)
        try:
            attention.stop_flash(attention.frame_hwnd(self._root))
        except Exception as e:
            event("bring_to_front_error", error=repr(e))

    def _paint_jackpot_banner(self, color: str) -> None:
        for widget in (self._jackpot_banner, self._jp_left, self._jp_right):
            widget.config(bg=color)

    def _pulse(self, token: int, on: bool) -> None:
        if token != self._pulse_token:
            return
        self._paint_jackpot_banner(JACKPOT_BANNER if on else JACKPOT_DEEP)
        self._root.after(JACKPOT_PULSE_MS, self._pulse, token, not on)

    # ----------------------------------------------------------------- sound

    def _kind_volume(self, kind: str) -> float:
        return self._config.alerts.jackpot_volume if kind == "jackpot" else self._volume

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
        }.get(kind, (alerts.sound, None))
        volume = self._kind_volume(kind)
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
        for kind in ("valdo", "generic", "jackpot"):
            self._alert_wav(kind)

    def _play_sound(self, kind: str = "valdo") -> None:
        """kind: "valdo" | "generic" | "jackpot" (the last one loops until
        _sync_jackpot or mute stops it)."""
        if self._muted or self._kind_volume(kind) <= 0:
            return
        if winsound is None:
            self._root.bell()
            return
        self._sound.submit(SoundCommand(kind, loop=kind == "jackpot"))

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
