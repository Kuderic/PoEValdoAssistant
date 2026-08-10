"""Always-on-top tkinter overlay. Runs on the MAIN thread only; all state
arrives as immutable events drained from the bus every 15 ms.

Latency contract: sound + render happen inside the same drain tick that
delivers AlertsChanged(new_alert=True); the frame->UI delta is logged as
`alert_shown` for the <100 ms acceptance check.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path

from sniper.alerts import AlertView
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    GameStatus,
    ListingSeen,
    PriceStatus,
    TabsChanged,
    Traveled,
)
from sniper.config import Config
from sniper.logging_setup import event

try:
    import winsound
except ImportError:  # non-Windows dev machine
    winsound = None

DRAIN_MS = 15
TICK_MS = 100


def _fmt(value) -> str:
    return "" if value is None else f"{value:g}"


def _parse(text: str) -> float | None:
    text = text.strip()
    return None if not text else float(text)


def _display_name(key: str) -> str:
    """Reward keys keep their 'Foil ' prefix internally; the UI drops it."""
    return key.removeprefix("Foil ")


BG = "#101418"
FG = "#d8dee5"
DIM = "#7a8590"
FAINT = "#48525c"  # diminished: feed rows that did not reach the threshold
GOOD = "#5fd069"
WARN = "#e0b341"
BAD = "#e05555"
PRICE = "#ffffff"


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
        self._current_combo = config.hotkey.combo
        self._tune_window: tk.Toplevel | None = None
        self._tooltip: tk.Toplevel | None = None
        self._alerts: tuple[AlertView, ...] = ()
        self._muted = False
        # after a travel, the traveled listing stays pinned in the top slot
        # for traveled_display_seconds instead of vanishing instantly
        self._pinned_top: AlertView | None = None
        self._pinned_until = 0.0
        self._pin_token = 0

        root.title("Valdo Sniper")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry("410x560+40+40")
        root.minsize(360, 320)

        # header: tab dots | tune button | price pill | game pill
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=10, pady=(8, 2))
        self._tabs_label = tk.Label(header, text="Tabs: 0", bg=BG, fg=DIM, font=("Consolas", 10))
        self._tabs_label.pack(side="left")
        tune_btn = tk.Label(header, text="⚙", bg=BG, fg=DIM, font=("Segoe UI", 11), cursor="hand2")
        tune_btn.pack(side="right")
        tune_btn.bind("<Button-1>", lambda e: self._open_tuning())
        self._mute_btn = tk.Label(
            header, text="🔊", bg=BG, fg=DIM, font=("Segoe UI", 11), cursor="hand2"
        )
        self._mute_btn.pack(side="right", padx=(0, 6))
        self._mute_btn.bind("<Button-1>", lambda e: self._toggle_mute())
        self._game_label = tk.Label(header, text="PoE: ?", bg=BG, fg=DIM, font=("Consolas", 10))
        self._game_label.pack(side="right", padx=(0, 8))
        self._price_label = tk.Label(
            header, text="Prices: manual", bg=BG, fg=DIM, font=("Consolas", 10)
        )
        self._price_label.pack(side="right", padx=(0, 10))
        # reward names being live-searched, on their own line (the header
        # row is too crowded to hold them)
        self._searches_label = tk.Label(
            root, text="", bg=BG, fg=DIM, font=("Consolas", 10), anchor="w", wraplength=390
        )
        self._searches_label.pack(fill="x", padx=10)

        # mismatch banner (hidden by default)
        self._banner = tk.Label(root, text="", bg=BAD, fg="white", font=("Segoe UI", 12, "bold"))

        # main alert area
        self._key_label = tk.Label(
            root,
            text="Waiting for listings…",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        )
        self._key_label.pack(fill="x", padx=10, pady=(6, 0))
        self._price_big = tk.Label(
            root, text="", bg=BG, fg=PRICE, font=("Segoe UI", 26, "bold"), anchor="w"
        )
        self._price_big.pack(fill="x", padx=10)
        self._detail = tk.Label(
            root,
            text="",
            bg=BG,
            fg=DIM,
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            wraplength=380,
        )
        self._detail.pack(fill="x", padx=10)
        self._chips = tk.Frame(root, bg=BG)  # colored warning chips
        self._chips.pack(fill="x", padx=10)
        # the top listing's mods, always visible (no hover needed); scoring
        # mods highlighted with their modifier
        self._top_mods = tk.Frame(root, bg=BG)
        self._top_mods.pack(fill="x", padx=10, pady=(2, 0))
        self._countdown = tk.Canvas(root, height=8, bg="#1d242c", highlightthickness=0)
        self._countdown.pack(fill="x", padx=10, pady=(4, 6))

        self._runners = tk.Frame(root, bg=BG)  # one clickable row per runner-up
        self._runners.pack(fill="x", padx=10)

        # live feed: every incoming listing, alerting or not; non-alerting
        # rows render diminished. Newest first. Hover a row for its mods.
        self._feed_entries: deque = deque(maxlen=max(config.alerts.feed_rows, 1))
        feed_box = tk.Frame(root, bg=BG)
        feed_box.pack(side="bottom", fill="x", padx=10, pady=(2, 0))
        tk.Frame(feed_box, bg="#1d242c", height=1).pack(fill="x", pady=(0, 3))
        tk.Label(
            feed_box,
            text=f"{'Price':<15}{'Profit':>7}{'Difficulty':>12}  Reward",
            bg=BG,
            fg=DIM,
            font=("Consolas", 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        self._feed = tk.Frame(feed_box, bg=BG)
        self._feed.pack(fill="x")

        # the top alert is clickable too - click any listing to travel to it;
        # hovering shows the map's full mod list
        for widget in (self._key_label, self._price_big, self._detail):
            widget.bind("<Button-1>", lambda e: self._click_top())
            widget.configure(cursor="hand2")
            self._bind_tooltip(widget, lambda: self._alerts[0].mods if self._alerts else ())
        self._status_line = tk.Label(
            root, text="", bg=BG, fg=DIM, font=("Consolas", 10), anchor="w"
        )
        self._status_line.pack(side="bottom", fill="x", padx=10, pady=(0, 6))

        # Warm-up: realize the banner once so its first real appearance does
        # not pay font-load/relayout cost, and prime the sound path (audible
        # "ready" chime; PlaySound loads the file synchronously even in
        # SND_ASYNC mode, so pay that cost now, not on the first snipe).
        self._banner.config(text="⚠ CURRENCY MISMATCH")
        self._banner.pack(fill="x", padx=10, pady=(6, 0), before=self._key_label)
        root.update_idletasks()
        self._banner.pack_forget()
        self._play_sound()

        root.after(DRAIN_MS, self._drain)
        root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------------ bus

    def _drain(self) -> None:
        for ev in self._bus.drain():
            if isinstance(ev, AlertsChanged):
                self._alerts = ev.alerts
                self._render_alerts()
                if ev.new_alert:
                    # log BEFORE the sound call so audio quirks never pollute
                    # the render latency figure; ev.new_view carries the
                    # arriving alert even when it ranks below the display cut
                    if ev.new_view is not None:
                        event(
                            "alert_shown",
                            listing_id=ev.new_view.listing_id,
                            frame_to_ui_ms=round(
                                (time.monotonic() - ev.new_view.created_monotonic) * 1000, 1
                            ),
                        )
                    self._play_sound()
            elif isinstance(ev, TabsChanged):
                n = len(ev.tabs)
                # a tab whose 30s heartbeat is >75s old is silently dead
                stale = sum(1 for t in ev.tabs if t.get("hello_age_s", 0) > 75)
                rewards = sorted(
                    {_display_name(t["search_reward"]) for t in ev.tabs if t.get("search_reward")}
                )
                text = f"Tabs: {n}" + (f" ({stale} stale)" if stale else "")
                self._tabs_label.config(text=text, fg=WARN if stale else GOOD if n else BAD)
                self._searches_label.config(
                    text=f"Searching: {', '.join(rewards)}" if rewards else ""
                )
            elif isinstance(ev, PriceStatus):
                color = {"live": GOOD, "stale": WARN, "manual": DIM}.get(ev.status, DIM)
                source = "poe.ninja" if ev.status in ("live", "stale") else "Prices"
                self._price_label.config(text=f"{source}: {ev.status}", fg=color)
            elif isinstance(ev, ClickOutcome):
                if ev.ok:
                    text = "Travel sent" + (f" ({ev.reason})" if ev.reason else "")
                else:
                    text = f"TRAVEL FAILED: {ev.reason}"
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
            elif isinstance(ev, ListingSeen):
                self._feed_entries.appendleft(ev.entry)
                self._render_feed()
        self._root.after(DRAIN_MS, self._drain)

    # ------------------------------------------------------------- rendering

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
        self._pinned_top = view
        self._pinned_until = time.monotonic() + self._config.alerts.traveled_display_seconds
        self._pin_token += 1
        token = self._pin_token

        def unpin() -> None:
            if token == self._pin_token:
                self._pinned_top = None
                self._render_alerts()

        self._root.after(int(self._config.alerts.traveled_display_seconds * 1000), unpin)
        self._render_alerts()

    def _render_alerts(self) -> None:
        pinned = self._pinned_top
        if not self._alerts and pinned is None:
            self._banner.pack_forget()
            self._key_label.config(text="No active alert", fg=DIM)
            self._price_big.config(text="")
            self._detail.config(text="")
            self._clear_frame(self._chips)
            self._clear_frame(self._top_mods)
            self._clear_frame(self._runners)
            self._countdown.delete("all")
            return

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
        self._key_label.config(
            text=f"{prefix}{_display_name(top.key)}   +{top.profit_div:.0f} div ({top.margin:.0%})",
            fg=GOOD,
        )
        self._price_big.config(text=f"{top.amount:g} {top.currency.upper()}")
        self._detail.config(
            text=f"Map asking price: {top.amount:g} {top.currency}   ·   "
            f"Reward avg: {top.reference_amount:g} {top.reference_currency}"
        )
        self._clear_frame(self._top_mods)
        for text, note in top.mods:
            tk.Label(
                self._top_mods,
                text=f"{text}" + (f"   {note}" if note else ""),
                bg=BG,
                fg=WARN if note else DIM,
                font=("Segoe UI", 10, "bold" if note else "normal"),
                anchor="w",
                justify="left",
                wraplength=380,
            ).pack(fill="x")
        self._clear_frame(self._chips)
        for label, color in top.special_warnings:
            tk.Label(
                self._chips,
                text=f"‼ {label.upper()}",
                bg=BAD if color == "red" else WARN,
                fg="white" if color == "red" else "#1a1a1a",
                font=("Segoe UI", 10, "bold"),
                padx=6,
            ).pack(side="left", padx=(0, 6))
        for label in top.warn_labels:
            tk.Label(
                self._chips, text=f"⚠ {label}", bg=BG, fg=WARN, font=("Segoe UI", 10, "bold")
            ).pack(side="left", padx=(0, 6))

        self._clear_frame(self._runners)
        for i, a in enumerate(runner_views):
            row = tk.Label(
                self._runners,
                text=f"#{i + 2}  +{a.profit_div:.0f}d  {a.amount:g} {a.currency}  "
                f"(avg {a.reference_amount:g})  {_display_name(a.key)}",
                bg=BG,
                fg=DIM,
                font=("Consolas", 10),
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
        width = self._countdown.winfo_width() or 380
        frac = remaining / total if total else 0
        color = GOOD if frac > 0.5 else WARN if frac > 0.25 else BAD
        self._countdown.create_rectangle(0, 0, width * frac, 10, fill=color, width=0)

    def _tick(self) -> None:
        self._draw_countdown()
        self._root.after(TICK_MS, self._tick)

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

        # general settings above the scoring grid
        general = tk.Frame(body, bg=BG)
        general.grid(row=0, column=0, columnspan=3, sticky="we", padx=10, pady=(8, 6))

        def general_row(r: int, label: str, value: str, width: int = 10) -> tk.Entry:
            tk.Label(general, text=label, bg=BG, fg=FG, font=("Consolas", 10)).grid(
                row=r, column=0, sticky="w", pady=(0, 2)
            )
            entry = tk.Entry(general, width=width, bg="#1d242c", fg=FG, insertbackground=FG)
            entry.insert(0, value)
            entry.grid(row=r, column=1, padx=(6, 0), sticky="w", pady=(0, 2))
            return entry

        thr_e = general_row(0, "Base margin alert threshold (divs)", _fmt(self._current_threshold))
        combo_e = general_row(1, "Hotkey combo", self._current_combo, width=16)
        base_e = general_row(2, "Base difficulty score (clean map)", _fmt(sc.base_default))
        tk.Label(
            general,
            text="The threshold is what a 100-difficulty map must profit.\n"
            "Required profit scales with difficulty:\n"
            "  difficulty 200 → 2× threshold, 50 → ½ threshold",
            bg=BG,
            fg=DIM,
            font=("Consolas", 10),
            anchor="w",
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Rules split into two exclusive groups: a rule either raises the base
        # difficulty (min_base) or multiplies the final score (multiplier),
        # never both. Warning-only rules (bismuth/blight) have no number to
        # tune and are not listed.
        base_rules = [r for r in sc.rules if r.min_base is not None]
        mult_rules = [r for r in sc.rules if r.multiplier is not None]

        row_cursor = [1]

        def section(title: str, column_title: str, rules, values) -> None:
            r = row_cursor[0]
            tk.Label(
                body, text=title, bg=BG, fg=FG, font=("Consolas", 10, "bold"), anchor="w"
            ).grid(row=r, column=0, sticky="w", padx=(10, 6), pady=(8, 1))
            tk.Label(body, text=column_title, bg=BG, fg=FG, font=("Consolas", 10, "bold")).grid(
                row=r, column=1, padx=(2, 10), pady=(8, 1)
            )
            row_cursor[0] += 1
            for rule, value in zip(rules, values, strict=True):
                r = row_cursor[0]
                tk.Label(
                    body, text=rule.label, bg=BG, fg=DIM, font=("Consolas", 10), anchor="w"
                ).grid(row=r, column=0, sticky="w", padx=(10, 6), pady=1)
                e = tk.Entry(body, width=7, bg="#1d242c", fg=FG, insertbackground=FG)
                e.insert(0, value)
                e.grid(row=r, column=1, padx=(2, 10))
                entries[rule.label] = e
                row_cursor[0] += 1

        section(
            "Base difficulty mods",
            "Base",
            base_rules,
            [_fmt(r.min_base) for r in base_rules],
        )
        section(
            "Difficulty multipliers",
            "Mult",
            mult_rules,
            [_fmt(r.multiplier) for r in mult_rules],
        )

        note = tk.Label(body, text="", bg=BG, fg=BAD, font=("Consolas", 10))
        note.grid(row=row_cursor[0], column=0, columnspan=2, pady=(4, 0))
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
                )
                new_threshold = float(thr_e.get())
            except ValueError as exc:
                note.config(text=f"Bad number: {exc}", fg=BAD)
                return
            new_combo = combo_e.get().strip()
            if not new_combo:
                note.config(text="Hotkey combo must not be empty", fg=BAD)
                return

            error = None
            if self._on_settings_change:
                error = self._on_settings_change(new_config, new_threshold, new_combo)
            if error:  # e.g. invalid hotkey combo - old combo stays active
                note.config(text=error, fg=BAD)
                return
            self._scoring_config = new_config
            self._current_threshold = new_threshold
            self._current_combo = new_combo
            if save and self._overrides_path is not None:
                save_scoring_overrides(
                    new_config,
                    self._overrides_path,
                    global_profit_div=new_threshold,
                    hotkey_combo=new_combo,
                )
                note.config(text=f"Saved to {self._overrides_path.name}", fg=GOOD)
            else:
                note.config(text="Applied (not saved)", fg=GOOD)

        buttons = tk.Frame(body, bg=BG)
        buttons.grid(row=len(sc.rules) + 3, column=0, columnspan=3, pady=8)
        tk.Button(buttons, text="Apply", command=lambda: collect(save=False)).pack(
            side="left", padx=4
        )
        tk.Button(buttons, text="Apply + Save", command=lambda: collect(save=True)).pack(
            side="left", padx=4
        )

    # ------------------------------------------------------------- live feed

    def _render_feed(self) -> None:
        self._clear_frame(self._feed)
        for e in self._feed_entries:
            profit = "?d" if e.profit_div is None else f"{e.profit_div:+.0f}d"
            note = {
                "blocked": "  [blocked]",
                "no_reference": "  [no ref]",
                "no_rate": "  [no rate]",
            }.get(e.verdict, "")
            price = f"{e.amount:g}d" if e.currency == "divine" else f"{e.amount:g} {e.currency}"
            fg = FG if e.verdict == "alert" else FAINT
            row = tk.Label(
                self._feed,
                text=f"{price:<15}{profit:>7}{e.difficulty:>12g}  {_display_name(e.key)}{note}",
                bg=BG,
                fg=fg,
                font=("Consolas", 10),
                anchor="w",
                cursor="hand2",
            )
            row.pack(fill="x")
            row.bind(
                "<Button-1>",
                lambda ev, lid=e.listing_id: self._on_travel and self._on_travel(lid),
            )
            self._bind_tooltip(row, e.mods)

    # ---------------------------------------------------------- mod tooltip

    def _bind_tooltip(self, widget: tk.Widget, mods) -> None:
        """mods: a tuple, or a callable returning one (for persistent widgets
        whose content changes between renders)."""
        provider = mods if callable(mods) else (lambda: mods)
        widget.bind("<Enter>", lambda e: self._show_tooltip(widget, provider()))
        widget.bind("<Leave>", lambda e: self._hide_tooltip())

    def _show_tooltip(self, widget: tk.Widget, mods) -> None:
        """mods: tuple of (mod text, scoring note). Mods that carry a
        difficulty modifier are highlighted with the modifier shown beside
        them; neutral mods render dim."""
        self._hide_tooltip()
        win = tk.Toplevel(self._root)
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, bg="#0a0d10", highlightthickness=1, highlightbackground="#3a4550")
        frame.pack()
        for text, note in mods or (("(no mods captured)", ""),):
            row = tk.Frame(frame, bg="#0a0d10")
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(
                row,
                text=text,
                bg="#0a0d10",
                fg=WARN if note else DIM,
                font=("Segoe UI", 10, "bold" if note else "normal"),
                anchor="w",
                justify="left",
                wraplength=320,
            ).pack(side="left")
            if note:
                tk.Label(
                    row,
                    text=f"  {note}",
                    bg="#0a0d10",
                    fg=BAD,
                    font=("Consolas", 10, "bold"),
                    anchor="e",
                ).pack(side="right")
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

    def _play_sound(self) -> None:
        if self._muted:
            return
        if winsound is None:
            self._root.bell()
            return

        # PlaySound loads the audio file synchronously even with SND_ASYNC
        # (~100-500ms when interrupting a playing sound), so it must never
        # run on the UI thread - burst alerts would delay rendering.
        def play() -> None:
            sound = self._config.alerts.sound
            if sound and Path(sound).exists():
                winsound.PlaySound(sound, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.PlaySound(
                    "SystemExclamation",
                    winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )

        threading.Thread(target=play, name="alert-sound", daemon=True).start()
