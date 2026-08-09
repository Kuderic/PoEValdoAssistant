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
from pathlib import Path

from sniper.alerts import AlertView
from sniper.bus import (
    AlertsChanged,
    Bus,
    ClickOutcome,
    GameStatus,
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
        on_scoring_change=None,  # Callable[[ModScoringConfig], None]: tuning applied
        overrides_path=None,  # Path for scoring_overrides.yaml persistence
    ):
        self._root = root
        self._bus = bus
        self._config = config
        self._on_travel = on_travel
        self._on_scoring_change = on_scoring_change
        self._overrides_path = overrides_path
        self._scoring_config = config.mod_scoring
        self._tune_window: tk.Toplevel | None = None
        self._alerts: tuple[AlertView, ...] = ()
        self._muted = False

        root.title("Valdo Sniper")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry("400x340+40+40")
        root.minsize(340, 240)

        # header: tab dots | tune button | price pill | game pill
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=10, pady=(8, 2))
        self._tabs_label = tk.Label(header, text="tabs: 0", bg=BG, fg=DIM, font=("Consolas", 10))
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
            header, text="prices: manual", bg=BG, fg=DIM, font=("Consolas", 10)
        )
        self._price_label.pack(side="right", padx=(0, 10))

        # mismatch banner (hidden by default)
        self._banner = tk.Label(root, text="", bg=BAD, fg="white", font=("Segoe UI", 12, "bold"))

        # main alert area
        self._key_label = tk.Label(
            root,
            text="waiting for listings…",
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
        self._countdown = tk.Canvas(root, height=8, bg="#1d242c", highlightthickness=0)
        self._countdown.pack(fill="x", padx=10, pady=(4, 6))

        self._runners = tk.Frame(root, bg=BG)  # one clickable row per runner-up
        self._runners.pack(fill="x", padx=10)

        # traveled panel: pins the traveled map's mods for a few seconds so
        # they're readable during the loading screen
        self._traveled = tk.Frame(root, bg="#182029")
        self._traveled_token = 0

        # the top alert is clickable too - click any listing to travel to it
        for widget in (self._key_label, self._price_big, self._detail):
            widget.bind("<Button-1>", lambda e: self._click_top())
            widget.configure(cursor="hand2")
        self._status_line = tk.Label(root, text="", bg=BG, fg=DIM, font=("Consolas", 9), anchor="w")
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
                text = f"tabs: {n}" + (f" ({stale} stale)" if stale else "")
                self._tabs_label.config(text=text, fg=WARN if stale else GOOD if n else BAD)
            elif isinstance(ev, PriceStatus):
                color = {"live": GOOD, "stale": WARN, "manual": DIM}.get(ev.status, DIM)
                source = "poe.ninja" if ev.status in ("live", "stale") else "prices"
                self._price_label.config(text=f"{source}: {ev.status}", fg=color)
            elif isinstance(ev, ClickOutcome):
                if ev.ok:
                    text = "travel sent" + (f" ({ev.reason})" if ev.reason else "")
                else:
                    text = f"TRAVEL FAILED: {ev.reason}"
                self._status_line.config(text=text, fg=GOOD if ev.ok else BAD)
            elif isinstance(ev, GameStatus):
                self._game_label.config(
                    text="PoE: running" if ev.running else "PoE: NOT RUNNING",
                    fg=GOOD if ev.running else BAD,
                )
            elif isinstance(ev, Traveled):
                self._show_traveled(ev.view)
        self._root.after(DRAIN_MS, self._drain)

    # ------------------------------------------------------------- rendering

    def _clear_frame(self, frame: tk.Frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _click_top(self) -> None:
        if self._alerts and self._on_travel:
            self._on_travel(self._alerts[0].listing_id)

    def _render_alerts(self) -> None:
        if not self._alerts:
            self._banner.pack_forget()
            self._key_label.config(text="no active alert", fg=DIM)
            self._price_big.config(text="")
            self._detail.config(text="")
            self._clear_frame(self._chips)
            self._clear_frame(self._runners)
            self._countdown.delete("all")
            return

        top = self._alerts[0]
        if top.mismatch:
            self._banner.config(
                text=f"⚠ MISMATCH: {top.currency.upper()} ≠ ref {top.reference_currency.upper()}"
            )
            self._banner.pack(fill="x", padx=10, pady=(6, 0), before=self._key_label)
        else:
            self._banner.pack_forget()

        self._key_label.config(
            text=f"{_display_name(top.key)}   +{top.profit_div:.0f} div ({top.margin:.0%})",
            fg=GOOD,
        )
        self._price_big.config(text=f"{top.amount:g} {top.currency.upper()}")
        self._detail.config(text=f"avg {top.reference_amount:g} {top.reference_currency}")
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
        for i, a in enumerate(self._alerts[1:]):
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
        self._draw_countdown()

    def _draw_countdown(self) -> None:
        self._countdown.delete("all")
        if not self._alerts:
            return
        top = self._alerts[0]
        total = self._config.alerts.expiry_seconds
        remaining = max(0.0, top.expires_at_monotonic - time.monotonic())
        width = self._countdown.winfo_width() or 380
        frac = remaining / total if total else 0
        color = GOOD if frac > 0.5 else WARN if frac > 0.25 else BAD
        self._countdown.create_rectangle(0, 0, width * frac, 10, fill=color, width=0)

    def _tick(self) -> None:
        self._draw_countdown()
        self._root.after(TICK_MS, self._tick)

    # ---------------------------------------------------------- tuning panel

    def _open_tuning(self) -> None:
        """⚙ panel: edit base_default, div_per_point, and every rule's
        min_base/multiplier. Apply swaps the live engine; Save also persists
        to scoring_overrides.yaml (merged over config.yaml on next start)."""
        if self._tune_window is not None and self._tune_window.winfo_exists():
            self._tune_window.lift()
            return
        win = tk.Toplevel(self._root)
        self._tune_window = win
        win.title("Mod scoring")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.geometry("+460+40")

        sc = self._scoring_config
        entries: dict[str, tuple[tk.Entry, tk.Entry]] = {}

        def add_row(r: int, name: str, v1: str, v2: str, header: bool = False):
            fg = FG if header else DIM
            font = ("Consolas", 10, "bold") if header else ("Consolas", 10)
            tk.Label(win, text=name, bg=BG, fg=fg, font=font, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(10, 6), pady=1
            )
            e1 = e2 = None
            if not header:
                e1 = tk.Entry(win, width=7, bg="#1d242c", fg=FG, insertbackground=FG)
                e1.insert(0, v1)
                e1.grid(row=r, column=1, padx=2)
                e2 = tk.Entry(win, width=7, bg="#1d242c", fg=FG, insertbackground=FG)
                e2.insert(0, v2)
                e2.grid(row=r, column=2, padx=(2, 10))
            return e1, e2

        add_row(0, "rule", "", "", header=True)
        tk.Label(win, text="base", bg=BG, fg=FG, font=("Consolas", 10, "bold")).grid(
            row=0, column=1
        )
        tk.Label(win, text="mult", bg=BG, fg=FG, font=("Consolas", 10, "bold")).grid(
            row=0, column=2, padx=(0, 10)
        )
        base_e, dpp_e = add_row(
            1, "base_default / div_per_point", _fmt(sc.base_default), _fmt(sc.div_per_point)
        )
        for i, rule in enumerate(sc.rules):
            entries[rule.label] = add_row(
                i + 2, rule.label, _fmt(rule.min_base), _fmt(rule.multiplier)
            )

        note = tk.Label(win, text="", bg=BG, fg=BAD, font=("Consolas", 9))
        note.grid(row=len(sc.rules) + 2, column=0, columnspan=3, pady=(4, 0))

        def collect(save: bool) -> None:
            from dataclasses import replace

            from sniper.config import ModScoringConfig, save_scoring_overrides

            try:
                new_rules = tuple(
                    replace(
                        rule,
                        min_base=_parse(entries[rule.label][0].get()),
                        multiplier=_parse(entries[rule.label][1].get()),
                    )
                    for rule in sc.rules
                )
                new_config = ModScoringConfig(
                    base_default=float(base_e.get()),
                    div_per_point=float(dpp_e.get()),
                    rules=new_rules,
                )
            except ValueError as exc:
                note.config(text=f"bad number: {exc}")
                return
            self._scoring_config = new_config
            if self._on_scoring_change:
                self._on_scoring_change(new_config)
            if save and self._overrides_path is not None:
                save_scoring_overrides(new_config, self._overrides_path)
                note.config(text=f"saved to {self._overrides_path.name}", fg=GOOD)
            else:
                note.config(text="applied (not saved)", fg=GOOD)

        buttons = tk.Frame(win, bg=BG)
        buttons.grid(row=len(sc.rules) + 3, column=0, columnspan=3, pady=8)
        tk.Button(buttons, text="Apply", command=lambda: collect(save=False)).pack(
            side="left", padx=4
        )
        tk.Button(buttons, text="Apply + Save", command=lambda: collect(save=True)).pack(
            side="left", padx=4
        )

    # --------------------------------------------------------- traveled panel

    def _show_traveled(self, view) -> None:
        """Pin the traveled map's mods for traveled_display_seconds so they
        can be read during the loading screen. Display only - the alert is
        already consumed and can never be clicked again."""
        self._traveled_token += 1
        token = self._traveled_token
        self._clear_frame(self._traveled)
        tk.Label(
            self._traveled,
            text=f"➜ TRAVELING: {_display_name(view.key)}  ·  {view.amount:g} {view.currency}",
            bg="#182029",
            fg=GOOD,
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=6, pady=(4, 2))
        for mod in view.mods:
            tk.Label(
                self._traveled,
                text=f"  {mod}",
                bg="#182029",
                fg=FG,
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=370,
            ).pack(fill="x", padx=6)
        tk.Frame(self._traveled, bg="#182029", height=4).pack()
        self._traveled.pack(fill="x", padx=10, pady=(4, 0), after=self._runners)

        def clear() -> None:
            if token == self._traveled_token:  # a newer travel supersedes us
                self._traveled.pack_forget()
                self._clear_frame(self._traveled)

        delay_ms = int(self._config.alerts.traveled_display_seconds * 1000)
        self._root.after(delay_ms, clear)

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
