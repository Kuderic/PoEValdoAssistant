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


# ------------------------------------------------- re-listing at a new price
# A re-listed item keeps its trade listing id but carries a new price. The
# userscript forwards it again (dedup keys on id + price); the store must
# treat it as news rather than as a duplicate.


def test_relisted_at_new_price_replaces_active_alert():
    store = AlertStore(expiry_s=10)
    assert store.insert(alert_decision("relist", 100))
    assert store.insert(alert_decision("relist", 80))  # price dropped
    assert len(store) == 1  # replaced, not duplicated
    view = store.views()[0]
    assert view.amount == 80  # the overlay shows the CURRENT price


def test_relisted_at_higher_price_also_realerts():
    """Any price change re-alerts - a raise is still a changed listing."""
    store = AlertStore(expiry_s=10)
    assert store.insert(alert_decision("relist", 80))
    assert store.insert(alert_decision("relist", 100))
    assert store.views()[0].amount == 100


def test_same_price_repush_is_still_a_duplicate():
    store = AlertStore(expiry_s=10)
    assert store.insert(alert_decision("dup", 100))
    assert not store.insert(alert_decision("dup", 100))


def test_relisting_never_resurrects_a_consumed_listing():
    """Single consumption is a hard guarantee: a re-price must not undo it."""
    store = AlertStore(expiry_s=10)
    store.insert(alert_decision("gone", 100))
    assert store.consume_best() is not None
    assert not store.insert(alert_decision("gone", 50))  # re-listed cheaper
    assert len(store) == 0


# ----------------------------------------------- ranking by profit/difficulty
# The hotkey takes the best VALUE (profit per 100 difficulty), not the
# biggest absolute profit: a hard map's larger surplus is not worth more.


def scored_decision(listing_id: str, amount: float, difficulty: float):
    """An alert-verdict decision with an explicit difficulty score."""
    from dataclasses import replace

    d = alert_decision(listing_id, amount)
    return replace(d, difficulty=difficulty)


def test_best_is_highest_profit_per_difficulty_not_biggest_profit():
    store = AlertStore(expiry_s=10)
    # hard map: +100 div profit but difficulty 1000 -> 10 per 100D
    store.insert(scored_decision("hard", 100, difficulty=1000))
    # easy map: +50 div profit at difficulty 25 -> 200 per 100D
    store.insert(scored_decision("easy", 150, difficulty=25))
    best = store.consume_best()
    assert best.decision.listing.listing_id == "easy"


def test_views_ranked_by_profit_per_difficulty():
    store = AlertStore(expiry_s=10, max_display=3)
    store.insert(scored_decision("mid", 100, difficulty=200))  # 50 per 100D
    store.insert(scored_decision("worst", 100, difficulty=1000))  # 10 per 100D
    store.insert(scored_decision("top", 100, difficulty=50))  # 200 per 100D
    assert [v.listing_id for v in store.views()] == ["top", "mid", "worst"]


def test_zero_difficulty_ranks_last_without_crashing():
    store = AlertStore(expiry_s=10)
    store.insert(scored_decision("nodiff", 100, difficulty=0))
    store.insert(scored_decision("normal", 100, difficulty=25))
    assert store.consume_best().decision.listing.listing_id == "normal"
