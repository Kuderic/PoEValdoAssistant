"""Startup price warm-up.

Listings are held back until their reward has a primary (trade or manual)
price, so the first decisions of a session are never judged against
poe.ninja's per-map median - which is the inaccurate fallback. A bug here
costs snipes, so the release paths are all covered: priced, settled,
timed out, and disabled.
"""

from dataclasses import replace

from conftest import make_config, make_listing_frame

from sniper.__main__ import App
from sniper.bus import Bus, ListingSeen, WarmupStatus
from sniper.models import parse_frame

REWARD = "Foil Mageblood"


def make_app(*, trade_pricing=True, prices=None):
    config = make_config(prices=prices if prices is not None else {})
    config = replace(config, trade_pricing=replace(config.trade_pricing, enabled=trade_pricing))
    return App(config, Bus())


def feed_listing(app, listing_id="a", reward=REWARD, amount=100.0):
    app.server._handle_listing(
        parse_frame(
            make_listing_frame(
                listing_id=listing_id,
                reward=reward,
                price={"amount": amount, "currency": "divine"},
            )
        )
    )
    return [e for e in app.bus.drain() if isinstance(e, ListingSeen)]


def test_listing_held_while_reward_unpriced():
    app = make_app()
    assert app._warmup_active
    assert feed_listing(app) == []  # nothing surfaces to the UI


def test_held_listing_still_registers_its_reward_for_pricing():
    """The hold must not hide the reward from the price loop, or nothing
    would ever get priced and the warm-up could never end."""
    app = make_app()
    feed_listing(app)
    assert REWARD in app.server.active_rewards()


def test_listing_flows_once_the_reward_is_priced():
    app = make_app()
    assert feed_listing(app, "held") == []
    app.book.set_trade_price(REWARD, 200.0)
    app._update_warmup()
    assert not app._warmup_active
    assert len(feed_listing(app, "released")) == 1


def test_manual_price_is_primary_so_nothing_is_held():
    """A manual override needs no fetch - it is authoritative already."""
    from sniper.config import ManualPrice

    app = make_app(prices={REWARD: ManualPrice(currency="divine", amount=200)})
    assert len(feed_listing(app)) == 1


def test_no_warmup_when_trade_pricing_disabled():
    """Then poe.ninja IS the intended source; there is nothing to wait for."""
    app = make_app(trade_pricing=False)
    assert not app._warmup_active
    assert len(feed_listing(app)) == 1


def test_timeout_releases_the_hold():
    """A reward the trade API never answers for must not stall forever."""
    import time

    app = make_app()
    assert feed_listing(app, "held") == []
    app._warmup_deadline = time.monotonic() - 1
    app._update_warmup()
    assert not app._warmup_active
    assert len(feed_listing(app, "released")) == 1


def test_reward_with_no_trade_listings_does_not_stall_warmup():
    """'trade_price_empty' rewards are settled: unpriceable, not pending."""
    app = make_app()
    feed_listing(app, "held")
    app._warmup_settled.add(REWARD)
    app._update_warmup()
    assert not app._warmup_active


def test_progress_is_published_for_the_overlay():
    app = make_app()
    feed_listing(app, "held")
    app._update_warmup()
    status = [e for e in app.bus.drain() if isinstance(e, WarmupStatus)]
    assert status[-1] == WarmupStatus(calculating=True, priced=0, total=1)


def test_only_the_unpriced_reward_is_held():
    """Per-reward gating: a priced reward's listings flow immediately even
    while another reward is still being priced."""
    app = make_app()
    app.book.set_trade_price(REWARD, 200.0)
    assert len(feed_listing(app, "priced", reward=REWARD)) == 1
    assert feed_listing(app, "other", reward="Foil Headhunter") == []
