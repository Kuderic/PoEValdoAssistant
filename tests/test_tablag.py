"""A live-search tab that has gone slow, and where a listing's delay went.

From the logs: a busy Mageblood tab crept from ~1.6 s behind GGG's index
time just after loading to 4-6 s by evening, and dropped back to ~1.6 s on
reload. Quiet tabs barely moved. App restarts reconnect every tab without
reloading the page, so they must not reset a tab's history.
"""

from __future__ import annotations

from conftest import make_config, make_listing_frame

from sniper.models import FetchTiming, Hello, Listing, parse_frame
from sniper.modrules import ModRules
from sniper.prices import PriceBook
from sniper.server import SniperServer, TabState
from sniper.tablag import FRESH_PAGE_S, MIN_SAMPLES, WINDOW, TabLagMonitor


def feed(m: TabLagMonitor, tab: str, lag_ms: float, n: int = WINDOW):
    changes = [c for _ in range(n) if (c := m.record(tab, lag_ms)) is not None]
    return changes[-1] if changes else None


def fresh(m: TabLagMonitor, *tabs: str) -> None:
    for t in tabs:
        m.page_seen(t, page_id=f"page-{t}", page_age_s=5)


# ------------------------------------------------------------------- rule


def test_a_tab_that_crept_up_from_its_fresh_delay_is_slow():
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "mb", 1600)
    feed(m, "nimis", 1500)
    assert feed(m, "mb", 4300) is True
    recent, base, slow = m.status("mb")
    assert (recent, base, slow) == (4300, 1600, True)
    assert m.status("nimis")[2] is False


def test_small_delays_are_never_worth_a_reload():
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "mb", 600)
    feed(m, "nimis", 600)
    assert feed(m, "mb", 2000) is None  # 3.3x its fresh delay, but only 2s


def test_when_every_tab_is_slow_it_is_ggg_not_the_tab():
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "mb", 1600)
    feed(m, "nimis", 1500)
    feed(m, "nimis", 4000)
    assert feed(m, "mb", 4300) is None


def test_a_tab_slow_from_the_start_is_not_flagged():
    """A reload would not help a tab that was never faster."""
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "nimis", 1500)
    assert feed(m, "mb", 4300, n=3 * WINDOW) is None


def test_a_reload_starts_over_and_clears_the_warning():
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "mb", 1600)
    feed(m, "nimis", 1500)
    feed(m, "mb", 4300)
    m.page_seen("mb", page_id="reloaded", page_age_s=3)
    assert m.status("mb") == (None, None, False)


def test_an_app_reconnect_to_the_same_page_keeps_the_history():
    m = TabLagMonitor()
    fresh(m, "mb", "nimis")
    feed(m, "mb", 1600)
    feed(m, "nimis", 1500)
    feed(m, "mb", 4300)
    m.page_seen("mb", page_id="page-mb", page_age_s=7 * 3600)  # every 30s hello
    assert m.status("mb")[2] is True


def test_after_an_app_restart_an_old_page_is_judged_against_the_other_tabs():
    """The restart lost the history and the page is already slow: its first
    listings are no fresh baseline, so the other tabs are the yardstick."""
    m = TabLagMonitor()
    m.page_seen("mb", page_id="old", page_age_s=6 * 3600)
    fresh(m, "nimis")
    feed(m, "nimis", 1500)
    assert feed(m, "mb", 4300) is True
    assert m.status("mb")[1] is None  # no fresh figure to quote


def test_an_old_page_alone_cannot_be_judged():
    m = TabLagMonitor()
    m.page_seen("mb", page_id="old", page_age_s=FRESH_PAGE_S + 1)
    assert feed(m, "mb", 6000) is None


def test_a_lone_fresh_tab_is_judged_against_itself():
    m = TabLagMonitor()
    fresh(m, "mb")
    feed(m, "mb", 1600)
    assert feed(m, "mb", 4300) is True


def test_it_recovers_only_well_below_the_trigger():
    """No flicker on a borderline tab: it trips at 2.5s and 1.6x, and clears
    only under 2s or 1.3x its fresh delay."""
    m = TabLagMonitor()
    fresh(m, "mb")
    feed(m, "mb", 1500)
    assert feed(m, "mb", 2600) is True
    assert feed(m, "mb", 2100) is None  # under the trigger, above the release
    assert feed(m, "mb", 1800) is False


def test_older_scripts_without_a_page_id_reset_on_reconnect():
    m = TabLagMonitor()
    m.page_seen("mb", page_id=None, page_age_s=None)
    feed(m, "mb", 1600)
    feed(m, "mb", 4300)
    m.forget("mb")  # disconnect
    m.page_seen("mb", page_id=None, page_age_s=None)
    assert m.status("mb") == (None, None, False)


def test_too_few_listings_say_nothing():
    m = TabLagMonitor()
    fresh(m, "mb")
    assert feed(m, "mb", 9000, n=MIN_SAMPLES - 1) is None
    assert m.status("mb")[0] is None


# ----------------------------------------------------------------- server


def server_with_tab(tab_id: str) -> SniperServer:
    config = make_config()
    server = SniperServer(config, PriceBook(config), ModRules([]))
    server.tabs[tab_id] = TabState(ws=None, search_id="s", last_hello_monotonic=0)
    server._tab_lag.page_seen(tab_id, page_id="p", page_age_s=1)
    return server


def listing(lid: str, lag_s: int) -> Listing:
    frame = make_listing_frame(
        listing_id=lid,
        indexed_at="2026-08-09T12:00:00Z",
        detected_at=f"2026-08-09T12:00:0{lag_s}.000Z",
        capture="net",
    )
    lst = parse_frame(frame)
    assert isinstance(lst, Listing)
    return lst


def test_the_snapshot_names_a_slow_tab():
    server = server_with_tab("tab1")
    for i in range(WINDOW):
        server._handle_listing(listing(f"a{i}", 1))
    for i in range(WINDOW):
        server._handle_listing(listing(f"b{i}", 5))
    (tab,) = server.tab_snapshot()
    assert (tab["lag_ms"], tab["fresh_lag_ms"], tab["slow"]) == (5000, 1000, True)


def test_dom_captured_listings_carry_no_delay_and_are_ignored():
    server = server_with_tab("tab1")
    for i in range(WINDOW):
        server._handle_listing(listing(f"a{i}", 1))
    lst = parse_frame(make_listing_frame(listing_id="dom", capture="dom"))
    server._handle_listing(lst)
    assert server.tab_snapshot()[0]["lag_ms"] == 1000


# ----------------------------------------------------------------- frames


def test_timing_splits_the_delay():
    lst = parse_frame(
        make_listing_frame(
            indexed_at="2026-08-09T12:00:00Z",
            detected_at="2026-08-09T12:00:04.300Z",
            timing={"push_to_fetch_ms": 900, "fetch_ms": 400, "hidden": True},
        )
    )
    assert lst.timing == FetchTiming(push_to_fetch_ms=900.0, fetch_ms=400.0, hidden=True)
    assert lst.index_to_push_ms == 3000  # 4300 - 900 - 400: before the push


def test_missing_or_bad_timing_is_dropped_field_by_field():
    lst = parse_frame(make_listing_frame(timing={"push_to_fetch_ms": None, "fetch_ms": "x"}))
    assert lst.timing == FetchTiming(None, None, None)
    assert lst.index_to_push_ms is None
    assert parse_frame(make_listing_frame()).timing is None


def test_hello_carries_the_page_identity():
    hello = parse_frame(
        {"type": "hello", "search_id": "s", "tab_id": "t", "page_id": "abc", "page_age_s": 12}
    )
    assert isinstance(hello, Hello)
    assert (hello.page_id, hello.page_age_s) == ("abc", 12.0)
    old = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t"})
    assert (old.page_id, old.page_age_s) == (None, None)
