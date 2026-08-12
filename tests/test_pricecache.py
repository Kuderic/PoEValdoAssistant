"""Persisted reward prices: the startup head start.

Without a cache a restart either blacks out (every listing held until the
trade API answers) or judges listings against poe.ninja's inaccurate median.
The cache must therefore load fast, refuse anything too stale to trade on,
and never take the app down when the file is missing or corrupt.
"""

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_config, make_listing_frame

from sniper import pricecache
from sniper.__main__ import App
from sniper.bus import Bus, ListingSeen
from sniper.models import parse_frame
from sniper.prices import PriceBook

HOUR = 3600.0


def write_cache(path: Path, rewards: dict[str, float], age_s: float = 0.0) -> None:
    at = time.time() - age_s
    path.write_text(
        json.dumps({"rewards": {r: {"divine": v, "at": at} for r, v in rewards.items()}}),
        encoding="utf-8",
    )


def test_round_trip_keeps_the_original_fetch_time(tmp_path):
    """The fetch time travels with the price: the UI ages it, so stamping
    it with the load time would report a stale price as brand new."""
    path = tmp_path / pricecache.CACHE_NAME
    fetched = time.time() - 9 * 60  # calculated 9 minutes ago
    pricecache.save(path, {"Foil Mageblood": (202.0, fetched)})
    loaded = pricecache.load(path, max_age_s=HOUR)
    assert loaded["Foil Mageblood"][0] == 202.0
    assert loaded["Foil Mageblood"][1] == pytest.approx(fetched, abs=1)


def test_stale_entries_are_dropped(tmp_path):
    path = tmp_path / pricecache.CACHE_NAME
    write_cache(path, {"Foil Mageblood": 202.0}, age_s=13 * HOUR)
    assert pricecache.load(path, max_age_s=12 * HOUR) == {}
    assert list(pricecache.load(path, max_age_s=24 * HOUR)) == ["Foil Mageblood"]


def test_missing_or_corrupt_cache_is_not_fatal(tmp_path):
    assert pricecache.load(tmp_path / "nope.json", max_age_s=HOUR) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert pricecache.load(bad, max_age_s=HOUR) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"rewards": "not a mapping"}), encoding="utf-8")
    assert pricecache.load(wrong, max_age_s=HOUR) == {}


def test_junk_entries_are_skipped_individually(tmp_path):
    path = tmp_path / pricecache.CACHE_NAME
    path.write_text(
        json.dumps(
            {
                "rewards": {
                    "good": {"divine": 100.0, "at": time.time()},
                    "no timestamp": {"divine": 50.0},
                    "not a dict": 5,
                    "zero": {"divine": 0, "at": time.time()},
                }
            }
        ),
        encoding="utf-8",
    )
    assert list(pricecache.load(path, max_age_s=HOUR)) == ["good"]


def test_save_is_atomic_leaving_no_partial_file(tmp_path):
    path = tmp_path / pricecache.CACHE_NAME
    pricecache.save(path, {"Foil Mageblood": (202.0, time.time())})
    assert path.is_file()
    assert not list(tmp_path.glob("*.tmp"))


# ------------------------------------------------------- effect on startup


def make_app(tmp_path, **kw):
    config = make_config(prices={})
    config = replace(config, trade_pricing=replace(config.trade_pricing, enabled=True, **kw))
    return App(config, Bus(), cache_path=tmp_path / pricecache.CACHE_NAME)


def feed(app, reward="Foil Mageblood"):
    app.server._handle_listing(
        parse_frame(make_listing_frame(reward=reward, price={"amount": 100, "currency": "divine"}))
    )
    return [e for e in app.bus.drain() if isinstance(e, ListingSeen)]


def test_cached_price_lets_the_first_listing_through(tmp_path):
    """The whole point: no startup blackout."""
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0})
    app = make_app(tmp_path)
    assert app.book.has_primary_price("Foil Mageblood")
    assert len(feed(app)) == 1


def test_without_a_cache_the_listing_is_still_held(tmp_path):
    app = make_app(tmp_path)
    assert feed(app) == []


def test_cached_price_is_flagged_as_provisional(tmp_path):
    """It is real trade data, but unconfirmed this session - the UI marks
    it, so it must not masquerade as a fresh average."""
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0})
    app = make_app(tmp_path)
    assert app.book.reference_for("Foil Mageblood").source == "cached"


def test_refresh_replaces_the_cached_marker(tmp_path):
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0})
    app = make_app(tmp_path)
    app.book.set_trade_price("Foil Mageblood", 208.0)  # the background refresh
    ref = app.book.reference_for("Foil Mageblood")
    assert (ref.source, ref.display_amount) == ("trade", 208.0)


def test_only_confirmed_prices_are_written_back(tmp_path):
    """A cached value must not be rewritten forever without the trade API
    ever confirming it, or a stale price could outlive its max age."""
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0})
    app = make_app(tmp_path)
    assert app.book.trade_prices() == {}  # nothing confirmed yet
    app.book.set_trade_price("Foil Nimis", 310.0)
    assert list(app.book.trade_prices()) == ["Foil Nimis"]
    assert app.book.trade_prices()["Foil Nimis"][0] == 310.0


def test_expired_cache_does_not_seed(tmp_path):
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0}, age_s=99 * HOUR)
    app = make_app(tmp_path, cache_max_age_minutes=60)
    assert not app.book.has_primary_price("Foil Mageblood")


def test_price_book_without_a_cache_path_is_unaffected():
    """--headless and tests construct App with no cache; must still work."""
    book = PriceBook(make_config())
    assert book.trade_prices() == {}


def test_restored_price_reports_its_true_age_not_zero(tmp_path):
    """A price fetched 9 minutes before the restart must still read as 9
    minutes old - the UI shows this, so 'just now' would be a lie."""
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0}, age_s=9 * 60)
    app = make_app(tmp_path)
    assert app.book.price_age_s("Foil Mageblood") == pytest.approx(9 * 60, abs=5)


def test_fresh_fetch_resets_the_age(tmp_path):
    write_cache(tmp_path / pricecache.CACHE_NAME, {"Foil Mageblood": 202.0}, age_s=9 * 60)
    app = make_app(tmp_path)
    app.book.set_trade_price("Foil Mageblood", 208.0)
    assert app.book.price_age_s("Foil Mageblood") == pytest.approx(0, abs=2)


def test_age_is_none_for_an_unpriced_reward(tmp_path):
    assert make_app(tmp_path).book.price_age_s("Foil Nothing") is None
