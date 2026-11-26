"""The reward price panel fills in during a pricing pass, not after it.

Each reward takes 60-95 s under the trade API's rate-limit pacing, so with
4-6 rewards a pass runs 5-8 minutes. Pushing prices only when the pass
ended left every row at "—" for that long after each start, even with last
session's prices sitting in the cache.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from conftest import make_config
from test_price_recheck import A, B

from sniper.__main__ import App
from sniper.bus import Bus, RewardPrices


def make_app() -> App:
    """Both rewards active, trade pricing on, and no manual prices (the test
    config pins Mageblood, which would mask the trade/cached figures)."""
    config = make_config()
    config = replace(
        config,
        prices={},
        trade_pricing=replace(config.trade_pricing, enabled=True),
    )
    app = App(config, Bus())
    app._warmup_active = False
    now = time.monotonic()
    app.server._recent_rewards = {A: now, B: now}
    return app


class GatedPricer:
    """Prices A, then blocks: the pacing pause before B never ends, so the
    test sees what is on screen mid-pass."""

    in_backoff = False
    backoff_remaining = 0.0

    def __init__(self, block_first_fetch: bool = False) -> None:
        self.calls: list[str] = []
        self._block_fetch = block_first_fetch
        self._never = asyncio.Event()

    async def fetch_reward_listings(self, league, reward):
        if self._block_fetch:
            await self._never.wait()
        self.calls.append(reward)
        return [(150.0, "divine")] * 5, "unid"

    async def pause_between_rewards(self) -> None:
        await self._never.wait()


def latest_prices(app) -> dict[str, tuple[float | None, str]]:
    pushes = [e for e in app.bus.drain() if isinstance(e, RewardPrices)]
    assert pushes, "nothing reached the overlay"
    return {p.reward: (p.amount, p.source) for p in pushes[-1].entries}


async def run_for(app, pricer, until) -> None:
    task = asyncio.create_task(app.trade_price_loop(pricer))
    try:
        deadline = time.monotonic() + 2
        while not until() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
    finally:
        task.cancel()


def test_each_price_shows_as_soon_as_it_is_fetched():
    async def scenario():
        app, pricer = make_app(), GatedPricer()
        await run_for(app, pricer, lambda: pricer.calls)
        return pricer.calls, latest_prices(app)

    calls, shown = asyncio.run(scenario())
    first, second = calls[0], ({A, B} - {calls[0]}).pop()
    assert calls == [first]  # mid-pass: the second reward is still waiting
    assert shown[first] == (150.0, "trade")
    assert shown[second] == (None, "unpriced")


def test_cached_prices_show_before_the_first_fetch_lands():
    async def scenario():
        app, pricer = make_app(), GatedPricer(block_first_fetch=True)
        app.book.set_trade_price(A, 140.0, cached=True, priced_at=time.time() - 3600)
        await run_for(app, pricer, lambda: False)
        return pricer.calls, latest_prices(app)

    calls, shown = asyncio.run(scenario())
    assert calls == []  # the first fetch is still in flight
    assert shown[A] == (140.0, "cached")
