"""Render the real overlay with sample data and save README screenshots.

    python tools/screenshots.py            # writes docs/overlay-*.png

Builds the actual Overlay (no server, no game, no sound) and feeds it the
same bus events the app would, then grabs each scene off the screen
(the window shows for about a second per scene). On a scaled display the grab
is scaled back to the window's own size. Windows only.
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctypes  # noqa: E402
import ctypes.wintypes  # noqa: E402

from PIL import Image, ImageGrab  # noqa: E402

from sniper.alerts import AlertView  # noqa: E402
from sniper.bus import (  # noqa: E402
    AlertsChanged,
    Bus,
    FeedEntry,
    GameStatus,
    ListingSeen,
    PriceStatus,
    RewardPrice,
    RewardPrices,
    TabsChanged,
    WarmupStatus,
)
from sniper.config import load_config  # noqa: E402
from sniper.overlay import Overlay  # noqa: E402
from sniper.server import EXPECTED_USERSCRIPT_VERSION  # noqa: E402

OUT = ROOT / "docs"
REWARDS = ("Foil Nimis", "Foil Mageblood", "Foil Emperor's Mastery", "Foil Progenesis")
# reward, price, seconds since it was fetched
PRICES = (
    ("Foil Nimis", 160.5, 240),
    ("Foil Mageblood", 290, 720),
    ("Foil Emperor's Mastery", 101, 95),
    ("Foil Progenesis", 63, 1500),
)

NIMIS_MODS = (
    ("Players are assaulted by apparitions of Atziri, Queen of the Vaal", "base 40", "yellow"),
    ("Monsters have +6 to Maximum Power Charges", "×1.2", "yellow"),
    ("Monsters Kill things with 20% or lower Life on Hit", "×1.15", "yellow"),
    ("Monsters gain a Power Charge on Hit", "", "none"),
    ("Non-Unique Equipment found in Area drops as Currency instead", "", "none"),
    ("All Strongboxes in Area must be opened to claim Reward", "", "none"),
    ("Maximum number of Players in Area is one", "", "none"),
)
MASTERY_MODS = (
    ("Area contains The Feared", "base 80", "red"),
    ("Players have 40% less Recovery Rate of Life and Energy Shield", "×1.3", "yellow"),
    ("Monsters gain a Power Charge on Hit", "", "none"),
    ("Monsters have 100% increased Area of Effect", "", "none"),
    ("Area contains 10 additional guarded Exquisite Vaal Vessels", "", "none"),
)


def view(lid, key, amount, net, difficulty, mods=(), lag=900.0, **kw) -> AlertView:
    now = time.monotonic()
    fields = dict(
        listing_id=lid,
        key=key,
        item_name="",
        amount=amount,
        currency="divine",
        net_div=net,
        required_net_div=2 * difficulty / 100,
        difficulty=difficulty,
        margin=0.5,
        seller="",
        mismatch=False,
        warn_labels=(),
        special_warnings=(),
        pairings=(),
        mods=mods,
        reference_amount=amount + net + 1,  # + running_cost, so the parts add up
        reference_currency="divine",
        reference_source="trade",
        index_lag_ms=lag,
        created_monotonic=now,
        expires_at_monotonic=now + 16 * kw.pop("life", 0.62),
    )
    fields.update(kw)
    return AlertView(**fields)


FEED = (
    # minutes ago, reward, price, net, difficulty, verdict, jackpot
    (0, "Foil Emperor's Mastery", 52, 48, 105.6, "alert", False),
    (1, "Foil Progenesis", 41, 22, 71, "alert", False),
    (2, "Foil Mageblood", 150, -12, 190, "below_threshold", False),
    (3, "Foil Fortress Covenant", 9, 3, 72, "below_threshold", False),
    (5, "Foil Nimis", 120, 35, 289, "alert", False),
    (8, "Foil Emperor's Mastery", 60, 1, 158, "below_threshold", False),
    (11, "Foil Progenesis", 18, 6, 72, "below_threshold", False),
    (13, "Foil Nimis", 80, 79.5, 55.2, "alert", True),
    (15, "Foil Mageblood", 95, 4, 138, "below_threshold", False),
)


def feed_events(skip_first: bool = False):
    now = time.monotonic()
    rows = FEED[1:] if skip_first else FEED
    for minutes, key, price, net, diff, verdict, jackpot in reversed(rows):
        yield ListingSeen(
            FeedEntry(
                listing_id=f"{key}{minutes}",
                key=key,
                amount=price,
                currency="divine",
                net_div=net,
                difficulty=diff,
                verdict=verdict,
                received_monotonic=now - minutes * 60,
                jackpot=jackpot,
            )
        )


def capture(root: tk.Tk, path: Path) -> None:
    """Grab the window's client area from the screen.

    The app is DPI-unaware, so on a scaled display Windows stretches it; the
    grab comes back at physical size and is scaled down to the window's own
    size, which is what the app looks like at 100%.
    """
    for _ in range(6):
        root.update()
        time.sleep(0.05)
    # Ask for the client rect in PHYSICAL pixels: switch this thread to
    # per-monitor DPI awareness just for these calls (Tk's own coordinates
    # are virtualised to 100% and would not line up with the grab).
    user32 = ctypes.windll.user32
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        hwnd = int(root.wm_frame(), 16)
        client = ctypes.wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = ctypes.wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        shot = ImageGrab.grab(all_screens=True)
        vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    box = (
        origin.x - vx,
        origin.y - vy,
        origin.x - vx + client.right,
        origin.y - vy + client.bottom,
    )
    w, h = root.winfo_width(), root.winfo_height()
    img = shot.crop(box).resize((w, h), Image.LANCZOS)
    img.save(path, optimize=True)
    print("wrote", path.relative_to(ROOT))


def scene(name: str, events, height: int = 800) -> None:
    config = load_config(ROOT / "config.yaml")
    root = tk.Tk()
    bus = Bus()
    overlay = Overlay(root, bus, config, on_price_recheck=lambda reward: None)
    overlay._play_sound = lambda *a, **k: None  # silent: no alarm while capturing
    overlay._sound.submit = lambda command: None  # the jackpot alarm bypasses _play_sound
    # on-screen for the moment of the grab (the overlay is always-on-top)
    root.geometry(f"515x{height}+60+60")
    for ev in events:
        bus.put(ev)
    overlay._drain_once()
    capture(root, OUT / f"overlay-{name}.png")
    for pending in root.tk.call("after", "info"):  # the overlay's tick/drain timers
        root.after_cancel(pending)
    root.destroy()


def status_events(tabs: int):
    tab = {"search_reward": None, "version": EXPECTED_USERSCRIPT_VERSION, "hello_age_s": 3}
    yield TabsChanged(tuple({**tab, "search_reward": r} for r in REWARDS[:tabs]))
    yield GameStatus(running=True)
    yield PriceStatus("live", "Covens")
    # what each reward is worth and how long ago it was fetched
    yield RewardPrices(
        tuple(
            RewardPrice(reward, amount, "divine", "trade", age)
            for reward, amount, age in PRICES
            if reward in REWARDS[:tabs]
        )
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)

    mastery = view(
        "m",
        "Foil Emperor's Mastery",
        52,
        48,
        105.6,
        MASTERY_MODS,
        reference_source="cached",
        pairings=(("Power charges + cull", "Burst kills through any recovery", 1.4),),
        stash=("7", 8, 8),
    )
    runners = (
        view("p", "Foil Progenesis", 41, 22, 71),
        view("n", "Foil Nimis", 120, 35, 289),
    )
    scene(
        "alert",
        [
            *status_events(4),
            *feed_events(),
            AlertsChanged((mastery, *runners), new_alert=False),
        ],
    )

    nimis = view(
        "j",
        "Foil Nimis",
        80,
        79.5,
        55.2,
        NIMIS_MODS,
        lag=1834.0,
        jackpot=True,
        life=0.86,
        stash=("Shop", 1, 0),
    )
    jp_feed = list(feed_events(skip_first=True))
    jp_feed.append(
        ListingSeen(
            FeedEntry(
                "j",
                "Foil Nimis",
                80,
                "divine",
                79.5,
                55.2,
                "alert",
                jackpot=True,
                received_monotonic=time.monotonic(),
            )
        )
    )
    scene(
        "jackpot",
        [
            *status_events(4),
            *jp_feed,
            AlertsChanged((nimis, view("m", "Foil Emperor's Mastery", 52, 48, 105.6)), False),
        ],
    )

    sup = view(
        "s",
        "Foil Nimis",
        80,
        79.5,
        55.2,
        NIMIS_MODS,
        lag=1834.0,
        jackpot=True,
        super_jackpot=True,
        life=0.86,
    )
    sup_feed = list(feed_events(skip_first=True))
    sup_feed.append(
        ListingSeen(
            FeedEntry(
                "s",
                "Foil Nimis",
                80,
                "divine",
                79.5,
                55.2,
                "alert",
                jackpot=True,
                super_jackpot=True,
                received_monotonic=time.monotonic(),
            )
        )
    )
    scene(
        "super-jackpot",
        [
            *status_events(4),
            *sup_feed,
            AlertsChanged(
                (sup, view("j", "Foil Mageblood", 80, 25, 60, jackpot=True)), new_alert=False
            ),
        ],
    )

    # no alert: the card becomes the reward price table, one row per state
    scene(
        "prices",
        [
            *status_events(4),
            *feed_events(),
            RewardPrices(
                (
                    RewardPrice("Foil Nimis", 160.5, "divine", "trade", 240),
                    RewardPrice("Foil Mageblood", 290, "divine", "trade", 3 * 3600),
                    RewardPrice("Foil Emperor's Mastery", 101, "divine", "cached", 5 * 3600),
                    RewardPrice("Foil Progenesis", 63, "divine", "trade", 1700),
                    RewardPrice("Foil Headhunter", None, "", "unpriced", None),
                )
            ),
            WarmupStatus(
                False,
                4,
                5,
                (
                    ("Foil Progenesis", "working", "63 divine", 1700),
                    ("Foil Headhunter", "unpriced", "", None),
                ),
            ),
        ],
        height=560,
    )

    # first launch: nothing connected yet, the card is the setup guide
    scene("setup", [GameStatus(running=True), PriceStatus("live", "Covens")], height=420)


if __name__ == "__main__":
    main()
