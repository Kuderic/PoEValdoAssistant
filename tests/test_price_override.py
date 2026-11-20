"""Session price overrides, end to end through App.

A reference price is occasionally wrong (thin unid market, an average that
lands badly), and every profit figure downstream inherits the error. The
settings panel pins a corrected price for the session; these cover the path
from that click to the decisions it changes, and back out again.
"""

from dataclasses import replace

from conftest import make_config, make_listing_frame

from sniper.__main__ import App
from sniper.bus import Bus, ListingSeen, RewardPrices
from sniper.models import parse_frame

REWARD = "Foil Mageblood"


def make_app(**kwargs):
    """No manual config price and no trade pricing: poe.ninja's median is the
    only automatic source, which is exactly when a correction is wanted."""
    config = make_config(prices={}, **kwargs)
    config = replace(config, trade_pricing=replace(config.trade_pricing, enabled=False))
    app = App(config, Bus())
    app.book._ninja_rewards = {REWARD: 180.0 * 210}  # 210 divine at the config rate
    app.book._last_refresh_monotonic = 10**9  # count it as a live refresh
    return app


def feed(app, listing_id="a", amount=200.0):
    app.server._handle_listing(
        parse_frame(
            make_listing_frame(
                listing_id=listing_id,
                reward=REWARD,
                price={"amount": amount, "currency": "divine"},
            )
        )
    )
    return [e.entry for e in app.bus.drain() if isinstance(e, ListingSeen)][-1]


def test_override_changes_the_profit_of_later_listings():
    app = make_app()
    assert feed(app, "before").net_div == 10.0  # 210 reference - 200 listed

    app.set_price_override(REWARD, 250.0)
    entry = feed(app, "after")
    assert entry.net_div == 50.0
    assert entry.reference_source == "override"


def test_override_can_push_a_listing_over_the_alert_threshold():
    app = make_app(min_yield=20.0, scoring_base_default=100.0)
    assert feed(app, "below").verdict == "below_threshold"
    app.set_price_override(REWARD, 250.0)
    assert feed(app, "alerting").verdict == "alert"


def test_clearing_the_override_restores_the_automatic_price():
    app = make_app()
    app.set_price_override(REWARD, 250.0)
    assert feed(app, "pinned").net_div == 50.0

    app.set_price_override(REWARD, None)
    entry = feed(app, "cleared")
    assert entry.net_div == 10.0
    assert entry.reference_source == "live"


def test_alerts_already_raised_keep_their_original_numbers():
    """Like the scoring panel, a correction applies from now on; re-judging
    live alerts under the user's hands would move a travel target mid-press."""
    app = make_app(min_yield=1.0, scoring_base_default=100.0)
    feed(app, "raised")
    before = app.store.views()[0].net_div
    app.set_price_override(REWARD, 250.0)
    assert app.store.views()[0].net_div == before


def test_override_is_published_beside_the_price_it_replaced():
    """The panel needs both: what is in force, and what clearing returns to."""
    app = make_app()
    feed(app, "a")  # registers the reward as active
    app.set_price_override(REWARD, 250.0)
    entry = [e for e in app.bus.drain() if isinstance(e, RewardPrices)][-1].entries[0]
    assert (entry.reward, entry.amount, entry.source) == (REWARD, 250.0, "override")
    assert entry.override_div == 250.0
    assert entry.auto_amount == 210.0 and entry.auto_currency == "divine"


def test_override_releases_a_listing_held_by_the_warmup():
    """A reward the trade API cannot price is exactly the case a hand-set
    price exists for, so pinning one must let its listings through."""
    config = replace(make_config(prices={}))
    app = App(config, Bus())
    app._warmup_active = True
    app.server._handle_listing(parse_frame(make_listing_frame(listing_id="held", reward=REWARD)))
    assert [e for e in app.bus.drain() if isinstance(e, ListingSeen)] == []

    app.set_price_override(REWARD, 250.0)
    assert not app._warmup_active
    app.server._handle_listing(parse_frame(make_listing_frame(listing_id="freed", reward=REWARD)))
    assert len([e for e in app.bus.drain() if isinstance(e, ListingSeen)]) == 1
