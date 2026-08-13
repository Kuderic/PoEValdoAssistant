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
from collections import deque
from tkinter import font as tkfont

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
from sniper.margin import profit_per_100_difficulty
from sniper.sound import alert_wav_path

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
    ("price", "PRICE"),
    ("profit", "PROFIT DIV"),
    ("ratio", "P/100D"),
    ("difficulty", "DIFFICULTY"),
)
# P/100D is the number the alert decision and the hotkey's pick both turn
# on, so it gets an outline the other three do not.
BOXED_STAT = "ratio"
STAT_PAD = 6  # inner padding, so the outline never touches the digits
# Difficulty colouring, tiered off the reference difficulty of 100 that the
# threshold is calibrated for: at or under it is an easy map, 3x it is brutal.
DIFF_WARN, DIFF_BAD = 100.0, 300.0
# Head start (ms) other snipers had on a listing before it reached us. The
# cutoffs come from this app's own logs: travels clicked within ~2s of
# detection lost ~26% of the time, 4-10s lost ~52%.
LAG_OK_MS, LAG_BAD_MS = 2000.0, 5000.0
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


BG = "#101418"
FG = "#e4e9ef"
DIM = "#a8b3bf"
FAINT = "#7b8794"  # diminished: feed rows that did not reach the threshold
GOOD = "#5fd069"
WARN = "#e0b341"
BAD = "#e05555"
PRICE = "#ffffff"
BOX = "#46525f"  # outline around the headline P/100D stat
GOOD_BRIGHT = "#7ee089"  # travel button, pressed
BTN_FG = "#0b1a0e"  # dark text on the green travel button
BTN_OFF = "#1d242c"  # travel button with nothing to travel to
MODS_BG = "#080b0e"  # darker panel behind the mod list


class Overlay:
    def __init__(
        self,
        root: tk.Tk,
        bus: Bus,
        config: Config,
        on_travel=None,  # Callable[[str], None]: user clicked an alert row
        # Callable[[ModScoringConfig, float, str], str | None]: settings
        # applied (scoring, global_profit_div, hotkey combo) -> error or None
        on_settings_change=None,
        overrides_path=None,  # Path for scoring_overrides.yaml persistence
    ):
        self._root = root
        self._bus = bus
        self._config = config
        self._on_travel = on_travel
        self._on_settings_change = on_settings_change
        self._overrides_path = overrides_path
        self._scoring_config = config.mod_scoring
        self._current_threshold = config.thresholds.global_profit_div
        self._current_flat_reduction = config.thresholds.flat_profit_reduction
        self._current_combo = config.hotkey.combo
        self._tune_window: tk.Toplevel | None = None
        self._tooltip: tk.Toplevel | None = None
        self._alerts: tuple[AlertView, ...] = ()
        self._muted = False
        self._volume = config.alerts.volume
        # ((sound path, volume), playable WAV path | None); see _alert_wav
        self._wav_cache: tuple[tuple[str, float], str | None] | None = None
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

        root.title("Valdo Sniper")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry("515x760+40+40")
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

        # header: tab dots | tune button | price pill | game pill
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=10, pady=(8, 2))
        self._tabs_label = tk.Label(
            header, text="Tabs: 0", bg=BG, fg=DIM, font=self._font("Consolas", 10)
        )
        self._tabs_label.pack(side="left")
        tune_btn = tk.Label(
            header, text="⚙", bg=BG, fg=DIM, font=self._font("Segoe UI", 11), cursor="hand2"
        )
        tune_btn.pack(side="right")
        tune_btn.bind("<Button-1>", lambda e: self._open_tuning())
        self._mute_btn = tk.Label(
            header, text="🔊", bg=BG, fg=DIM, font=self._font("Segoe UI", 11), cursor="hand2"
        )
        self._mute_btn.pack(side="right", padx=(0, 6))
        self._mute_btn.bind("<Button-1>", lambda e: self._toggle_mute())
        self._game_label = tk.Label(
            header, text="PoE: ?", bg=BG, fg=DIM, font=self._font("Consolas", 10)
        )
        self._game_label.pack(side="right", padx=(0, 8))
        self._price_label = tk.Label(
            header, text="Prices: manual", bg=BG, fg=DIM, font=self._font("Consolas", 10)
        )
        self._price_label.pack(side="right", padx=(0, 10))
        # reward names being live-searched, on their own line (the header
        # row is too crowded to hold them)
        self._searches_label = tk.Label(
            root,
            text="",
            bg=BG,
            fg=DIM,
            font=self._font("Consolas", 10),
            anchor="w",
            wraplength=495,
        )
        self._searches_label.pack(fill="x", padx=10)
        # hovering shows what the app currently thinks each reward is worth
        self._reward_prices: tuple = ()
        self._bind_tooltip(self._searches_label, self._reward_price_rows)

        # mismatch banner (hidden by default)
        self._banner = tk.Label(
            root, text="", bg=BAD, fg="white", font=self._font("Segoe UI", 12, "bold")
        )

        # main alert area: reward name on the left, travel button on the right
        title_row = tk.Frame(root, bg=BG)
        title_row.pack(fill="x", padx=10, pady=(6, 0))
        self._key_label = tk.Label(
            title_row,
            text="Waiting for listings…",
            bg=BG,
            fg=FG,
            font=self._font("Segoe UI", 16, "bold"),
            anchor="w",
        )
        self._key_label.pack(side="left", fill="x", expand=True)
        # How stale the listing already was when we first saw it. Sits next
        # to the travel button because it is a go/no-go signal: the longer it
        # was live before reaching us, the more likely it is already sold.
        self._age_label = tk.Label(
            title_row, text="", bg=BG, fg=DIM, font=self._font("Consolas", 11, "bold")
        )
        self._age_label.pack(side="right", padx=(8, 0))
        self._bind_tooltip(self._age_label, (LATENCY_HELP,))

        # Travel button, right of the title. Pressing it is one user input
        # driving one server action - the same contract as the hotkey and as
        # clicking the alert text, routed through the identical _click_top
        # path. takefocus=0 so it never steals keyboard focus from the game.
        self._travel_btn = tk.Button(
            title_row,
            text="TELEPORT",
            command=self._click_top,
            font=self._font("Segoe UI", 14, "bold"),
            bg=GOOD,
            fg=BTN_FG,
            activebackground=GOOD_BRIGHT,
            activeforeground=BTN_FG,
            disabledforeground=FAINT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            takefocus=0,
            cursor="hand2",
            padx=14,
            pady=7,
        )
        self._travel_btn.pack(side="right", padx=(10, 0))
        self._set_travel_button(has_target=False, pinned=False)  # nothing yet at startup

        # The four numbers that decide a snipe, in the order you read them:
        # what you pay, what you make, what it is worth per unit of pain,
        # and how much pain. Each column carries its own unit caption - the
        # price one names the currency, which must stay prominent.
        # each column is its own cell so BOXED_STAT can be outlined. A cell
        # insets its contents by its 1px border plus STAT_PAD, so the frame's
        # own padding drops by that much to keep the first number aligned
        # with the reward name above it.
        self._stats = tk.Frame(root, bg=BG)
        self._stats.pack(fill="x", padx=10 - STAT_PAD - 1, pady=(2, 2))
        self._stat_cells: dict[str, tk.Frame] = {}
        self._stat_caps: dict[str, tk.Label] = {}
        self._stat_vals: dict[str, tk.Label] = {}
        for col, (key, caption) in enumerate(STAT_COLUMNS):
            self._stats.grid_columnconfigure(col, weight=1, uniform="stat")
            boxed = key == BOXED_STAT
            # every cell carries the 1px border; the unboxed ones paint it in
            # the background colour so all four stay pixel-aligned
            cell = tk.Frame(
                self._stats,
                bg=BG,
                highlightthickness=1,
                highlightbackground=BOX if boxed else BG,
                highlightcolor=BOX if boxed else BG,
            )
            cell.grid(row=0, column=col, sticky="nsew", padx=(0, 6))
            cap = tk.Label(
                cell, text=caption, bg=BG, fg=DIM, font=self._font("Consolas", 9), anchor="w"
            )
            cap.pack(fill="x", padx=STAT_PAD, pady=(1, 0))
            val = tk.Label(
                cell, text="", bg=BG, fg=PRICE, font=self._font("Segoe UI", 23, "bold"), anchor="w"
            )
            val.pack(fill="x", padx=STAT_PAD, pady=(0, 1))
            self._stat_cells[key] = cell
            self._stat_caps[key] = cap
            self._stat_vals[key] = val
        self._chips = tk.Frame(root, bg=BG)  # colored warning chips
        self._chips.pack(fill="x", padx=10)

        # Deadly mod pairings get their own full-width rows rather than a
        # chip: the note ("Impossible unless DPS-check build") is the part
        # worth reading, and it does not fit in a chip.
        self._pairings = tk.Frame(root, bg=BG)

        # Startup pricing progress: which reward is being fetched, which are
        # done and at what price. Only visible while warming up, so it costs
        # nothing once sniping starts.
        self._warmup_panel = tk.Frame(root, bg=MODS_BG)

        # the top listing's mods, always visible (no hover needed); scoring
        # mods highlighted with their modifier. Its own darker panel so the
        # mod list reads as a distinct block under the headline numbers.
        self._top_mods = tk.Frame(root, bg=MODS_BG)
        self._top_mods.pack(fill="x", padx=10, pady=(4, 0))
        self._countdown = tk.Canvas(root, height=8, bg="#1d242c", highlightthickness=0)
        self._countdown.pack(fill="x", padx=10, pady=(4, 6))

        self._runners = tk.Frame(root, bg=BG)  # one clickable row per runner-up
        self._runners.pack(fill="x", padx=10)

        # Status line is packed BEFORE the feed so it wins the bottom slot:
        # pack gives space in call order, and the expanding feed would
        # otherwise squeeze travel results off-screen in a small window.
        self._status_line = tk.Label(
            root, text="", bg=BG, fg=DIM, font=self._font("Consolas", 10), anchor="w"
        )
        self._status_line.pack(side="bottom", fill="x", padx=10, pady=(0, 6))

        # live feed: every incoming listing, alerting or not; non-alerting
        # rows render diminished. Newest first. Hover a row for its mods.
        self._feed_entries: deque = deque(maxlen=max(config.alerts.feed_rows, 1))
        feed_box = tk.Frame(root, bg=BG)
        # expand: the feed absorbs the window's spare vertical space instead
        # of leaving a gap between the alert area and the status line
        feed_box.pack(side="bottom", fill="both", expand=True, padx=10, pady=(2, 0))
        tk.Frame(feed_box, bg="#1d242c", height=1).pack(fill="x", pady=(0, 3))
        self._threshold_note = tk.Label(
            feed_box, text="", bg=BG, fg=DIM, font=self._font("Consolas", 10), anchor="w"
        )
        self._threshold_note.pack(fill="x")
        self._update_threshold_note()
        tk.Label(
            feed_box,
            # P/100D = profit per 100 difficulty: the same unit as the
            # threshold noted directly above, and the alert ranking key
            text=f"{'Price':<8}{'Profit':<9}{'P/100D':<9}{'Difficulty':<12}{'Time':<10}Reward",
            bg=BG,
            fg=DIM,
            font=self._font("Consolas", 10, "bold"),
            anchor="w",
        ).pack(fill="x")

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
                font=self._font("Consolas", 10),
                anchor="w",
                cursor="hand2",
            )
            row.bind("<Button-1>", lambda e, idx=i: self._feed_click(idx))
            # rows sit above the canvas, so they need the wheel binding too
            row.bind("<MouseWheel>", self._feed_wheel)
            self._bind_tooltip(row, lambda idx=i: self._feed_mods(idx))
            self._feed_rows.append(row)

        # the top alert is clickable too - click any listing to travel to it;
        # hovering shows the map's full mod list. Every headline number is
        # part of the same target, so a click anywhere on it travels.
        clickable = (
            self._key_label,
            *self._stat_vals.values(),
            *self._stat_caps.values(),
            *self._stat_cells.values(),  # the boxed cell's padding too
        )
        self._top_widgets = clickable
        for widget in clickable:
            widget.bind("<Button-1>", lambda e: self._click_top())
            widget.configure(cursor="hand2")
            self._bind_tooltip(widget, lambda: self._alerts[0].mods if self._alerts else ())

        # Warm the (silent) audio cache off-thread so the first alert never
        # pays for reading and rescaling the WAV.
        threading.Thread(target=self._alert_wav, name="alert-sound-warmup", daemon=True).start()

        # Warm-up: realize the banner once so its first real appearance does
        # not pay font-load/relayout cost. (No startup sound: alert audio
        # plays on a background thread, so no priming is needed.)
        self._banner.config(text="⚠ CURRENCY MISMATCH")
        self._banner.pack(fill="x", padx=10, pady=(6, 0), before=self._key_label)
        root.update_idletasks()
        self._banner.pack_forget()

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
                text = f"Tabs: {n}" + (f" ({stale} stale)" if stale else "")
                self._tabs_label.config(text=text, fg=WARN if stale else GOOD if n else BAD)
                unknown = sum(1 for t in ev.tabs if not t.get("search_reward"))
                parts = []
                if rewards:
                    parts.append(", ".join(rewards))
                if unknown and n:  # tabs whose reward is not yet known
                    parts.append(f"+{unknown} unidentified")
                self._searches_label.config(text=f"Searching: {' · '.join(parts)}" if parts else "")
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
                    text="PoE: Running" if ev.running else "PoE: NOT RUNNING",
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
            self._play_sound()

    # ----------------------------------------------------------------- fonts

    def _font(
        self, family: str, size: int, weight: str = "normal", underline: bool = False
    ) -> tkfont.Font:
        """Shared mutable Font per (family, base size, weight, underline);
        every widget - static, per-render, or tooltip - goes through here so
        _zoom can rescale the whole overlay at once."""
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
        carry no right-hand note."""
        return tuple(
            (
                f"{_display_name(reward)}: {amount:g} {currency}"
                + (f"   ·   {_ago(age)}" if _ago(age) else ""),
                "",
                "none",
            )
            for reward, amount, currency, _source, age in self._reward_prices
        )

    def _update_threshold_note(self) -> None:
        """Stated in the P/100D column's units so the cutoff can be read
        straight off the feed."""
        self._threshold_note.config(text=f"Alerting at P/100D ≥ {self._current_threshold:g}")

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
            self._price_label.config(text=f"est. prices: {health.unpriced}/{health.total}", fg=WARN)
            return
        if self._config.trade_pricing.enabled:
            self._price_label.config(text="Prices: trade", fg=GOOD)
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
                font=self._font("Consolas", 10, "bold" if state == "done" else "normal"),
                anchor="e",
            ).pack(side="right", padx=(10, 0))
            tk.Label(
                line,
                text=_display_name(reward),
                bg=MODS_BG,
                fg=FG if state in ("done", "working") else DIM,
                font=self._font("Consolas", 10),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
        self._warmup_panel.pack(fill="x", padx=10, pady=(4, 0), before=self._runners)

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
        """One row per mod: text left, its scoring modifier pinned right and
        underlined, so the modifiers line up as a scannable column instead of
        trailing off after ragged mod text. Caller clears `parent` first."""
        for text, note, level in mods:
            fg = BAD if level == "red" else WARN if level == "yellow" else DIM
            weight = "bold" if level != "none" else "normal"
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", padx=6, pady=1)
            if note:
                # packed before the text so it reserves the right-hand column
                tk.Label(
                    row,
                    text=note,
                    bg=bg,
                    fg=fg,
                    font=self._font("Consolas", 10, weight, underline=True),
                    anchor="ne",
                    # top-aligned: a combo mod's rows are multi-line, and the
                    # modifier reads as belonging to the block, not floating
                    # in its vertical middle
                ).pack(side="right", padx=(10, 0), anchor="n")
            tk.Label(
                row,
                text=text,
                bg=bg,
                fg=fg,
                font=self._font("Segoe UI", 10, weight),
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

    def _set_stats(self, top: AlertView) -> None:
        """Fill the four headline numbers, each coloured by what it means:
        profit by sign, P/100D against the alert threshold, difficulty
        against the reference difficulty of 100."""
        ratio = profit_per_100_difficulty(top.profit_div, top.difficulty)
        self._stat_caps["price"].config(
            text=f"PRICE {top.currency.upper()}",
            # a mismatched currency is already bannered; flag it here too so
            # the number is never read in the wrong unit
            fg=BAD if top.mismatch else DIM,
        )
        self._stat_vals["price"].config(text=f"{top.amount:g}", fg=BAD if top.mismatch else PRICE)
        self._stat_vals["profit"].config(
            text=f"{top.profit_div:+.0f}", fg=GOOD if top.profit_div > 0 else BAD
        )
        if ratio is None:
            ratio_text = "?"
        else:  # a decimal matters at 3.8, not at 128
            ratio_text = f"{ratio:+.0f}" if abs(ratio) >= 100 else f"{ratio:+.1f}"
        self._stat_vals["ratio"].config(
            text=ratio_text,
            fg=GOOD if ratio is not None and ratio >= self._current_threshold else WARN,
        )
        self._stat_vals["difficulty"].config(
            # whole numbers only: the fractional part is noise at this size
            # (the feed's Difficulty column keeps the exact score)
            text=f"{top.difficulty:.0f}",
            fg=BAD if top.difficulty > DIFF_BAD else WARN if top.difficulty > DIFF_WARN else FG,
        )

    def _show_alert_body(self, chips: bool, mods: bool, countdown: bool) -> None:
        """Pack/unpack the alert's lower blocks.

        Emptying a frame is not enough: Tk keeps a container's requested
        height after its children are destroyed, so the dark mod panel kept
        reserving its full height with nothing in it. Unpacking releases the
        space to the feed below, which expands into it.

        Always repacked `before` the runner-up rows so the order survives a
        hide/show cycle (a plain pack() would append to the end).
        """
        for frame in (self._chips, self._pairings, self._top_mods, self._countdown):
            frame.pack_forget()
        if chips:
            self._chips.pack(fill="x", padx=10, before=self._runners)
        if self._pairings.winfo_children():
            self._pairings.pack(fill="x", padx=10, pady=(2, 0), before=self._runners)
        if mods:
            self._top_mods.pack(fill="x", padx=10, pady=(4, 0), before=self._runners)
        if countdown:
            self._countdown.pack(fill="x", padx=10, pady=(4, 6), before=self._runners)

    def _set_age(self, lag_ms: float | None) -> None:
        """Show the head start other snipers had. Green means we saw it
        essentially as it was indexed; red means it had been sitting live
        long enough that it is probably already gone."""
        if lag_ms is None:
            self._age_label.config(text="", fg=DIM)  # DOM capture: no index time
            return
        seconds = max(0.0, lag_ms) / 1000
        amount = f"{lag_ms:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"
        self._age_label.config(
            text=f"latency {amount}",
            fg=BAD if lag_ms > LAG_BAD_MS else WARN if lag_ms > LAG_OK_MS else GOOD,
        )

    def _set_travel_button(self, has_target: bool, pinned: bool) -> None:
        """Three states: armed (green, shows the hotkey that does the same
        thing), already-traveled, and nothing to travel to."""
        if pinned:
            text, enabled = "➜  TRAVELING…", False
        elif has_target:
            text, enabled = f"⚡  TELEPORT     [ {self._current_combo} ]", True
        else:
            text, enabled = "TELEPORT", False
        self._travel_btn.config(
            text=text,
            state="normal" if enabled else "disabled",
            bg=GOOD if enabled else BTN_OFF,
            activebackground=GOOD_BRIGHT if enabled else BTN_OFF,
            cursor="hand2" if enabled else "",
        )

    def _render_alerts(self) -> None:
        pinned = self._pinned_top
        top_widgets = self._top_widgets
        if not self._alerts and pinned is None:
            self._banner.pack_forget()
            self._key_label.config(text=self._idle_text(), fg=DIM)
            for key, caption in STAT_COLUMNS:
                self._stat_caps[key].config(text=caption, fg=DIM)
                self._stat_vals[key].config(text="—", fg=FAINT)
            self._set_age(None)
            self._set_travel_button(has_target=False, pinned=False)
            self._clear_frame(self._chips)
            self._clear_frame(self._pairings)
            self._clear_frame(self._top_mods)
            self._clear_frame(self._runners)
            self._countdown.delete("all")
            # collapse the whole alert body: with nothing to show, that space
            # belongs to the history list below (or to the warm-up panel,
            # which is the one thing worth showing while idle)
            self._show_alert_body(chips=False, mods=False, countdown=False)
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
                text=f"⚠ MISMATCH: {top.currency.upper()} ≠ ref {top.reference_currency.upper()}"
            )
            self._banner.pack(fill="x", padx=10, pady=(6, 0), before=self._key_label)
        else:
            self._banner.pack_forget()

        prefix = "➜ " if pinned is not None else ""
        self._key_label.config(text=f"{prefix}{_display_name(top.key)}", fg=GOOD)
        self._warmup_panel.pack_forget()  # a live alert outranks the progress list
        self._set_stats(top)
        self._set_age(top.index_lag_ms)
        self._set_travel_button(has_target=True, pinned=pinned is not None)
        self._clear_frame(self._pairings)
        for label, note, mult in top.pairings:
            row = tk.Frame(self._pairings, bg=BAD)
            row.pack(fill="x", pady=(0, 2))
            tk.Label(
                row,
                text=f"☠ {label.upper()}  ×{mult:g}",
                bg=BAD,
                fg="white",
                font=self._font("Segoe UI", 10, "bold"),
                anchor="w",
                padx=6,
            ).pack(side="left")
            if note:
                tk.Label(
                    row,
                    text=f"{note}  ",
                    bg=BAD,
                    fg="white",
                    font=self._font("Segoe UI", 10),
                    anchor="e",
                ).pack(side="right")

        self._clear_frame(self._top_mods)
        self._render_mods(self._top_mods, top.mods, MODS_BG, wrap=380)
        self._clear_frame(self._chips)
        if top.reference_source in (*FALLBACK_SOURCES, CACHED_SOURCE):
            # this profit was NOT computed against a trade average confirmed
            # this session - say so rather than let it read as solid
            cached = top.reference_source == CACHED_SOURCE
            tk.Label(
                self._chips,
                text="⏳ CACHED PRICE" if cached else "⚠️ ESTIMATED PRICE",
                bg=WARN,
                fg="#1a1a1a",
                font=self._font("Segoe UI", 10, "bold"),
                padx=6,
            ).pack(side="left", padx=(0, 6))
        for label, color in top.special_warnings:
            tk.Label(
                self._chips,
                text=f"{'❗' if color == 'red' else '⚠️'} {label.upper()}",
                bg=BAD if color == "red" else WARN,
                fg="white" if color == "red" else "#1a1a1a",
                font=self._font("Segoe UI", 10, "bold"),
                padx=6,
            ).pack(side="left", padx=(0, 6))
        for label in top.warn_labels:
            tk.Label(
                self._chips,
                text=f"⚠ {label}",
                bg=BG,
                fg=WARN,
                font=self._font("Segoe UI", 10, "bold"),
            ).pack(side="left", padx=(0, 6))
        # only reserve space for blocks that actually have something in them
        self._show_alert_body(
            chips=bool(self._chips.winfo_children()),
            mods=bool(top.mods),
            countdown=True,
        )

        self._clear_frame(self._runners)
        for i, a in enumerate(runner_views):
            row = tk.Label(
                self._runners,
                text=f"#{i + 2}  +{a.profit_div:.0f}d  {a.amount:g} {a.currency}  "
                f"(avg {a.reference_amount:g})  {_display_name(a.key)}",
                bg=BG,
                fg=DIM,
                font=self._font("Consolas", 10),
                anchor="w",
                cursor="hand2",
            )
            row.pack(fill="x")
            row.bind(
                "<Button-1>",
                lambda e, lid=a.listing_id: self._on_travel and self._on_travel(lid),
            )
            self._bind_tooltip(row, a.mods)
        self._draw_countdown()

    def _draw_countdown(self) -> None:
        self._countdown.delete("all")
        if self._pinned_top is not None:
            total = self._config.alerts.traveled_display_seconds
            remaining = max(0.0, self._pinned_until - time.monotonic())
        elif self._alerts:
            total = self._config.alerts.expiry_seconds
            remaining = max(0.0, self._alerts[0].expires_at_monotonic - time.monotonic())
        else:
            return
        width = self._countdown.winfo_width() or 485
        frac = remaining / total if total else 0
        color = GOOD if frac > 0.5 else WARN if frac > 0.25 else BAD
        self._countdown.create_rectangle(0, 0, width * frac, 10, fill=color, width=0)

    def _tick(self) -> None:
        self._draw_countdown()
        self._root.after(TICK_MS, self._tick)

    def _refresh_feed_ages(self) -> None:
        """Keep the feed's minutes-ago column current between listings."""
        if self._feed_entries:
            self._render_feed()
        self._root.after(30_000, self._refresh_feed_ages)

    # ---------------------------------------------------------- tuning panel

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
            tk.Label(
                body, text=title, bg=BG, fg=FG, font=self._font("Consolas", 10, "bold"), anchor="w"
            ).grid(row=r, column=0, sticky="w", padx=(10, 6), pady=(10, 1))
            if column_title:
                tk.Label(
                    body, text=column_title, bg=BG, fg=FG, font=self._font("Consolas", 10, "bold")
                ).grid(row=r, column=1, padx=(2, 10), pady=(10, 1))
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
                font=self._font("Consolas", 10),
                anchor="w",
                cursor="question_arrow" if hint else "",
            )
            name.grid(row=r, column=0, sticky="w", padx=(10, 6), pady=1)
            if hint:
                self._bind_tooltip(name, ((hint, "", "none"),))
            entry = tk.Entry(body, width=width, bg="#1d242c", fg=FG, insertbackground=FG)
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
                font=self._font("Consolas", 9),
                anchor="w",
                justify="left",
            ).grid(row=row_cursor[0], column=0, columnspan=2, sticky="w", padx=10, pady=(2, 0))
            row_cursor[0] += 1

        # --- alerting ------------------------------------------------------
        # The threshold IS a P/100D value: alert when profit/difficulty*100
        # reaches it, which is exactly the feed's P/100D column.
        heading("Alerting", "Value")
        thr_e = field("Alert at P/100D ≥", _fmt(self._current_threshold))
        flat_e = field(
            "Flat profit reduction",
            _fmt(self._current_flat_reduction),
            hint="This accounts for profit loss from selling items and time spent running the map."
            "Recommended value is 1.",
        )
        caption(
            "P/100D = divine profit per 100 difficulty (feed column 3).\n"
            "A clean 25-difficulty map at P/100D 14 profits ~3.5d;\n"
            "a 200-difficulty map must profit 2× as much to match.\n"
            "Highest P/100D is also what the hotkey travels to."
        )

        heading("Controls")
        combo_e = field("Hotkey combo", self._current_combo, width=16)
        vol_e = field("Alert volume (0 - 1)", _fmt(self._volume))

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

        note = tk.Label(body, text="", bg=BG, fg=BAD, font=self._font("Consolas", 10))
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
            except ValueError as exc:
                note.config(text=f"Bad number: {exc}", fg=BAD)
                return
            new_combo = combo_e.get().strip()
            if not new_combo:
                note.config(text="Hotkey combo must not be empty", fg=BAD)
                return
            if not 0.0 <= new_volume <= 1.0:
                note.config(text="Volume must be between 0 and 1", fg=BAD)
                return

            error = None
            if self._on_settings_change:
                error = self._on_settings_change(new_config, new_threshold, new_combo, new_flat)
            if error:  # e.g. invalid hotkey combo - old combo stays active
                note.config(text=error, fg=BAD)
                return
            self._scoring_config = new_config
            self._current_threshold = new_threshold
            self._current_flat_reduction = new_flat
            self._current_combo = new_combo
            if new_volume != self._volume:
                self._volume = new_volume
                # rescale off-thread; the cache keys on volume so this both
                # invalidates and re-warms in one step
                threading.Thread(
                    target=self._alert_wav, name="alert-sound-warmup", daemon=True
                ).start()
            self._update_threshold_note()
            self._render_alerts()  # button label carries the hotkey combo
            if save and self._overrides_path is not None:
                save_scoring_overrides(
                    new_config,
                    self._overrides_path,
                    global_profit_div=new_threshold,
                    hotkey_combo=new_combo,
                    alert_volume=new_volume,
                    flat_profit_reduction=new_flat,
                )
            note.config(text="Saved ✓", fg=GOOD)

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
            combo_e,
            base_e,
            vol_e,
            *entries.values(),
            *pair_entries.values(),
        ]:
            widget.bind("<KeyRelease>", schedule_apply)

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
            amount = f"{lag:.0f}ms" if lag < 1000 else f"{lag / 1000:.1f}s"
            level = "red" if lag > LAG_BAD_MS else "yellow" if lag > LAG_OK_MS else "none"
            rows.append((f"latency {amount} behind the trade site", "", level))
        return tuple(rows) + entry.mods

    def _render_feed(self) -> None:
        """Update the pooled row labels in place (see __init__)."""
        for idx, e in enumerate(self._feed_entries):
            profit = "?d" if e.profit_div is None else f"{e.profit_div:+.0f}d"
            note = {
                "blocked": "  [blocked]",
                "no_reference": "  [no ref]",
                "no_rate": "  [no rate]",
            }.get(e.verdict, "")
            price = f"{e.amount:g}d" if e.currency == "divine" else f"{e.amount:g} {e.currency}"
            ratio = profit_per_100_difficulty(e.profit_div, e.difficulty)
            per_100 = "?" if ratio is None else f"{ratio:+.1f}"
            if e.received_monotonic > 0:
                age = f"{max(0, int((time.monotonic() - e.received_monotonic) / 60))}m ago"
            else:
                age = "-"
            row = self._feed_rows[idx]
            row.config(
                text=f"{price:<8}{profit:<9}{per_100:<9}{e.difficulty:<12g}{age:<10}"
                f"{_display_name(e.key)}{note}",
                fg=FG if e.verdict == "alert" else FAINT,
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
        self._mute_btn.config(text="🔇" if self._muted else "🔊", fg=BAD if self._muted else DIM)
        event("mute_toggled", muted=self._muted)

    def _alert_wav(self) -> str | None:
        """Path to the volume-scaled WAV for the current sound + volume.

        Cached: rescaling a ~240 KB WAV on every alert would be wasted work,
        and the first call is warmed at startup so no alert pays for it.
        None means no WAV was found - the caller uses the (unscalable)
        system alias instead.
        """
        key = (self._config.alerts.sound, round(self._volume, 3))
        cached = self._wav_cache
        if cached is None or cached[0] != key:
            cached = (key, alert_wav_path(key[0], self._volume))
            self._wav_cache = cached
        return cached[1]

    def _play_sound(self) -> None:
        if self._muted or self._volume <= 0:
            return
        if winsound is None:
            self._root.bell()
            return

        # PlaySound loads the audio synchronously even with SND_ASYNC
        # (~100-500ms when interrupting a playing sound), so it must never
        # run on the UI thread - burst alerts would delay rendering.
        def play() -> None:
            try:
                path = self._alert_wav()
                if path is not None:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:  # no WAV available: alias playback ignores volume
                    winsound.PlaySound(
                        "SystemExclamation",
                        winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
            except Exception as e:
                # a silent alert is a missed snipe; never swallow the reason
                event("alert_sound_error", error=repr(e))

        threading.Thread(target=play, name="alert-sound", daemon=True).start()
