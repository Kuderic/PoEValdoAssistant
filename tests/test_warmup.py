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
    status = [e for e in app.bus.drain() if isinstance(e, WarmupStatus)][-1]
    assert (status.calculating, status.priced, status.total) == (True, 0, 1)


def test_progress_names_each_reward_and_its_state():
    """The panel shows what is being priced, not just how many."""
    app = make_app()
    feed_listing(app, "a", reward=REWARD)
    feed_listing(app, "b", reward="Foil Headhunter")
    app._pricing_now = REWARD  # this one is in flight
    app._update_warmup()
    rows = {r: (state, price) for r, state, price, _age in app.bus.drain()[-1].rewards}
    assert rows[REWARD][0] == "working"
    assert rows["Foil Headhunter"][0] == "pending"


def test_progress_reports_the_settled_price():
    app = make_app()
    feed_listing(app, "a", reward=REWARD)
    app.book.set_trade_price(REWARD, 202.0)
    app._update_warmup()
    rows = {r: (state, price) for r, state, price, _age in app.bus.drain()[-1].rewards}
    assert rows[REWARD][0] == "done"
    assert rows[REWARD][1] == "202 divine"  # no '~': a settled trade average


def test_a_reward_with_no_listings_reads_as_unpriced():
    app = make_app()
    feed_listing(app, "a", reward=REWARD)
    app._warmup_settled.add(REWARD)
    app._warmup_active = True  # inspect the row before the warm-up closes
    app._update_warmup()
    rows = {r: state for r, state, _price, _age in app.bus.drain()[-1].rewards}
    assert rows[REWARD] == "unpriced"


def test_only_the_unpriced_reward_is_held():
    """Per-reward gating: a priced reward's listings flow immediately even
    while another reward is still being priced."""
    app = make_app()
    app.book.set_trade_price(REWARD, 200.0)
    assert len(feed_listing(app, "priced", reward=REWARD)) == 1
    assert feed_listing(app, "other", reward="Foil Headhunter") == []


# --------------------------------------------- progress after the warm-up
# The same panel has to cover a scheduled re-price and a search the user
# opens mid-session, not just startup.


def test_progress_still_published_after_warmup_ends():
    app = make_app()
    feed_listing(app, "a", reward=REWARD)
    app.book.set_trade_price(REWARD, 202.0)
    app._update_warmup()
    assert not app._warmup_active
    app.bus.drain()

    app._pricing_now = REWARD  # a scheduled re-price starts
    app._update_warmup()
    status = [e for e in app.bus.drain() if isinstance(e, WarmupStatus)][-1]
    assert status.calculating is False  # listings are NOT held any more
    assert dict((r, s) for r, s, _price, _age in status.rewards)[REWARD] == "working"


def test_a_reward_from_a_new_tab_shows_as_pending():
    """Opening another live search mid-session must surface its pricing."""
    app = make_app()
    feed_listing(app, "a", reward=REWARD)
    app.book.set_trade_price(REWARD, 202.0)
    app._update_warmup()
    app.bus.drain()

    feed_listing(app, "b", reward="Foil Nimis")  # new search appears
    app._update_warmup()
    rows = {r: s for r, s, _price, _age in app.bus.drain()[-1].rewards}
    assert rows == {REWARD: "done", "Foil Nimis": "pending"}
