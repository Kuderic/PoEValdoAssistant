"""The bus waker must never block the thread that publishes.

bus.put runs on the asyncio thread - the detection path. It used to call
root.event_generate directly, and a Tcl call from a non-main thread waits
until the Tk thread gets round to running it: every put stalled behind
whatever the overlay was rendering. Under a burst of listings that backed
decisions up by seconds (2 s median at 10 listings/s in a soak).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk

import pytest
from conftest import make_config

from sniper.bus import Bus, GameStatus
from sniper.overlay import Overlay


def test_bus_put_does_not_wait_for_a_busy_tk_thread():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    bus = Bus()
    Overlay(root, bus, make_config())
    timings: list[float] = []

    def producer() -> None:
        for _ in range(20):
            start = time.perf_counter()
            bus.put(GameStatus(True))
            timings.append(time.perf_counter() - start)

    def busy_render() -> None:
        # stands in for a long render: the Tk thread is occupied while the
        # asyncio side keeps publishing. (Not joined: with a blocking waker
        # that would deadlock, which is the bug itself.)
        threading.Thread(target=producer, daemon=True).start()
        time.sleep(0.5)

    root.after(200, busy_render)
    root.after(1500, root.quit)
    try:
        root.mainloop()
        # and the events were still delivered once the Tk thread was free
        assert bus.drain() == []
    finally:
        root.destroy()
    assert len(timings) == 20
    assert max(timings) < 0.05, f"bus.put blocked for {max(timings) * 1000:.0f} ms"
