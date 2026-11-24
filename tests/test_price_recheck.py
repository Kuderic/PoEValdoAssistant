"""The price panel's "recheck" button: re-price one reward now.

One click queues one fetch through the normal pricer (so its rate-limit
handling still applies), it jumps the scheduled queue, and repeats while that
fetch is pending do nothing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from conftest import make_config

from sniper.__main__ import App
from sniper.bus import Bus
from sniper.overlay import _price_status
from sniper.tradeprice import TradeBackoff

A = "Foil Mageblood"
B = "Foil Headhunter"


class FakePricer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.backoff_once = False
        self.in_backoff = False
        self.backoff_remaining = 0.0

    async def fetch_reward_listings(self, league, reward):
        if self.backoff_once:
            self.backoff_once = False
            self.in_backoff = True  # as the real pricer does after a 429
            raise TradeBackoff("HTTP 429")
        self.calls.append(reward)
        return [(150.0, "divine")] * 5, "unid"

    async def pause_between_rewards(self) -> None:
        await asyncio.sleep(0)


def make_app() -> App:
    config = make_config()
    config = replace(config, trade_pricing=replace(config.trade_pricing, enabled=True))
    app = App(config, Bus())
    app._warmup_active = False
    now = time.monotonic()
    app.server._recent_rewards = {A: now, B: now}
    return app


async def run_until(app: App, pricer: FakePricer, done, timeout: float = 2.0) -> None:
    task = asyncio.create_task(app.trade_price_loop(pricer))
    try:
        deadline = time.monotonic() + timeout
        while not done() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()


def test_a_recheck_reprices_that_reward_straight_away():
    """Both rewards were just priced, so neither is due for refresh_minutes;
    the click still re-prices A within moments, and only A."""

    async def scenario():
        app, pricer = make_app(), FakePricer()
        task = asyncio.create_task(app.trade_price_loop(pricer))
        while len(pricer.calls) < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # the loop is napping now
        app.request_price_recheck(A)
        start = time.monotonic()
        while len(pricer.calls) < 3 and time.monotonic() - start < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        return pricer.calls, time.monotonic() - start

    calls, waited = asyncio.run(scenario())
    assert sorted(calls[:2]) == sorted([A, B])
    assert calls[2:] == [A]
    assert waited < 1.0  # woke the nap rather than waiting it out


def test_repeat_clicks_while_queued_ask_for_one_fetch():
    async def scenario():
        app, pricer = make_app(), FakePricer()
        app.request_price_recheck(A)
        app.request_price_recheck(A)
        app.request_price_recheck(A)
        await run_until(app, pricer, lambda: len(pricer.calls) >= 2)
        await asyncio.sleep(0.1)
        return pricer.calls

    calls = asyncio.run(scenario())
    # first pass: the forced A first, then B (due); no extra A fetches
    assert calls == [A, B]


def test_a_queued_recheck_shows_as_queued():
    app = make_app()
    app.request_price_recheck(A)
    states = {r: s for r, s, _p, _a in app._pricing_rows([A, B])}
    assert states[A] == "queued"
    assert states[B] != "queued"


def test_a_recheck_survives_a_rate_limit_backoff():
    """A 429 stops the batch; the user's request is kept and runs after."""

    async def scenario():
        app, pricer = make_app(), FakePricer()
        pricer.backoff_once = True
        app.request_price_recheck(A)
        await run_until(app, pricer, lambda: False, timeout=0.05)  # hits the 429
        assert A in app._recheck and pricer.calls == []
        pricer.in_backoff = False  # the backoff lifts
        await run_until(app, pricer, lambda: A in pricer.calls)
        return pricer.calls

    assert asyncio.run(scenario())[0] == A  # still first in line


def test_recheck_ignores_unknown_rewards_and_disabled_trade_pricing():
    app = make_app()
    app.request_price_recheck("Foil Not Searched")
    assert app._recheck == set()

    off = App(make_config(), Bus())  # trade pricing disabled
    off.server._recent_rewards = {A: time.monotonic()}
    off.request_price_recheck(A)
    assert off._recheck == set()


def test_the_panel_row_says_recheck_queued():
    class Entry:
        amount, currency, source, override_div = 160.0, "divine", "trade", None

    assert _price_status(Entry(), "queued", 240.0, 1800.0) == ("160d", "recheck queued", "good")
