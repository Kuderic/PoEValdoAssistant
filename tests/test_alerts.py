import time

from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.alerts import AlertStore
from sniper.models import parse_frame
from sniper.modrules import ModRules
from sniper.prices import PriceBook

CONFIG = make_config()
BOOK = PriceBook(CONFIG)
NO_RULES = ModRules([])


def alert_decision(listing_id: str, amount: float, currency: str = "divine"):
    lst = parse_frame(
        make_listing_frame(listing_id=listing_id, price={"amount": amount, "currency": currency})
    )
    d = margin.evaluate(lst, BOOK, CONFIG.thresholds, NO_RULES)
    assert d.verdict == "alert", d
    return d


def test_insert_dedup_and_non_alert_rejected():
    store = AlertStore(expiry_s=10)
    d = alert_decision("a", 100)
    assert store.insert(d)
    assert not store.insert(d)  # duplicate id
    below = parse_frame(
        make_listing_frame(listing_id="b", price={"amount": 190, "currency": "divine"})
    )
    dec = margin.evaluate(below, BOOK, CONFIG.thresholds, NO_RULES)
    assert not store.insert(dec)  # below_threshold never enters the store


def test_best_margin_wins_and_views_ranked():
    store = AlertStore(expiry_s=10, max_display=3)
    store.insert(alert_decision("small", 140))  # 30% margin
    store.insert(alert_decision("big", 60))  # 70% margin
    store.insert(alert_decision("mid", 100))  # 50% margin
    views = store.views()
    assert [v.listing_id for v in views] == ["big", "mid", "small"]
    best = store.consume_best()
    assert best.decision.listing.listing_id == "big"


def test_consume_all_clears_runners_up():
    store = AlertStore(expiry_s=10, consume_mode="all")
    store.insert(alert_decision("one", 100))
    store.insert(alert_decision("two", 60))
    assert store.consume_best().decision.listing.listing_id == "two"
    assert store.consume_best() is None  # second press: nothing
    assert len(store) == 0


def test_consume_top_keeps_runners_up():
    store = AlertStore(expiry_s=10, consume_mode="top")
    store.insert(alert_decision("one", 100))
    store.insert(alert_decision("two", 60))
    assert store.consume_best().decision.listing.listing_id == "two"
    assert store.consume_best().decision.listing.listing_id == "one"
    assert store.consume_best() is None


def test_consumed_id_never_returns():
    store = AlertStore(expiry_s=10)
    d = alert_decision("once", 100)
    store.insert(d)
    store.consume_best()
    assert not store.insert(d)  # re-pushed row cannot re-alert
    assert store.consume_best() is None


def test_expiry_prunes_and_blocks_consumption():
    store = AlertStore(expiry_s=0.05)
    store.insert(alert_decision("fast", 100))
    assert len(store) == 1
    time.sleep(0.08)
    assert store.prune()
    assert len(store) == 0
    store.insert(alert_decision("fast2", 100))
    time.sleep(0.08)
    assert store.consume_best() is None  # expired mid-wait: not clickable


def test_views_capped_by_max_display():
    store = AlertStore(expiry_s=10, max_display=2)
    for i, amount in enumerate((100, 90, 80, 70)):
        store.insert(alert_decision(f"x{i}", amount))
    assert len(store.views()) == 2


def test_consume_specific_leaves_others():
    store = AlertStore(expiry_s=10, consume_mode="all")
    store.insert(alert_decision("one", 100))
    store.insert(alert_decision("two", 60))
    assert store.consume("one").decision.listing.listing_id == "one"
    assert store.consume("one") is None  # already consumed
    assert len(store) == 1  # "two" untouched even in consume_mode=all
    assert store.consume_best().decision.listing.listing_id == "two"
