"""Non-Valdo live searches: the user's own search IS the filter.

A tab watching anything other than a Valdo map reward has no reward to
price, no map mods to score, and no threshold to clear - so its listings
alert on arrival. These tests pin the three properties that make that safe:
it never reaches the pricing budget, it never pollutes the Valdo ranking,
and it is never held by the startup warm-up.
"""

from __future__ import annotations

import time

from conftest import make_config, make_listing_frame

from sniper import margin
from sniper.alerts import AlertStore
from sniper.models import Hello, Listing, parse_frame
from sniper.modrules import ModRules
from sniper.prices import PriceBook

NO_RULES = ModRules([])


def listing(**overrides) -> Listing:
    frame = parse_frame(make_listing_frame(**overrides))
    assert isinstance(frame, Listing), frame
    return frame


def generic(**overrides) -> Listing:
    """A listing as a generic tab sends it: flagged, and with no reward."""
    overrides.setdefault("generic", True)
    overrides.setdefault("reward", None)
    overrides.setdefault("type_line", "Vermillion Ring")
    overrides.setdefault("item_name", "Doom Coil")
    return listing(**overrides)


def decide(lst, config=None):
    config = config or make_config()
    return margin.evaluate(lst, PriceBook(config), config.thresholds, NO_RULES)


# ------------------------------------------------------------------ decision


def test_generic_listing_alerts_with_no_reference_price():
    """The whole point: an unpriceable item still reaches the overlay. The
    Valdo path would return no_reference here and never alert."""
    d = decide(generic())
    assert d.verdict == "alert"
    assert d.reference is None and d.net_div is None


def test_a_rewardless_listing_alerts_even_when_the_tab_says_valdo():
    """Regression: after an in-place search edit the tab's query state went
    stale and too few rows were on screen for the row fallback, so it sent
    an Ephemeral Edge with generic:false. It sat as no_reference: no alert,
    no sound. Every Valdo map has a Reward, so no reward means not Valdo."""
    lst = listing(reward=None, item_name="Ephemeral Edge", generic=False)
    assert lst.generic is True
    assert decide(lst).verdict == "alert"


def test_a_listing_with_a_reward_stays_in_the_valdo_lane():
    lst = listing(generic=False)
    assert lst.reward and lst.generic is False


def test_a_rewardless_listing_marks_its_tab_as_a_live_search():
    """So the status strip counts it as a live search, not 'unidentified'."""
    from sniper.server import SniperServer, TabState

    config = make_config()
    server = SniperServer(config, PriceBook(config), NO_RULES)
    lst = listing(reward=None, generic=False)
    server.tabs[lst.tab_id] = TabState(ws=None, search_id=lst.search_id, last_hello_monotonic=0)
    server._handle_listing(lst)
    assert server.tabs[lst.tab_id].search_kind == "any"
    assert server.active_rewards() == set()


def test_key_prefers_the_base_type():
    """For a rare, "Vermillion Ring" identifies it; the rare name does not."""
    assert decide(generic()).key == "Vermillion Ring"
    assert decide(generic(type_line=None)).key == "Doom Coil"


def test_difficulty_stays_zero_so_p100d_never_computes():
    """difficulty 0 makes net_yield return None, keeping
    these out of the Yield column and the Valdo ranking maths."""
    d = decide(generic(mods=["+45 to maximum Life", "+38% to Fire Resistance"]))
    assert d.difficulty == 0.0
    assert margin.net_yield(d.net_div, d.difficulty) is None


def test_mods_survive_unscored_for_display():
    d = decide(generic(mods=["+45 to maximum Life"]))
    assert d.mods_annotated == (("+45 to maximum Life", "", "none"),)


def test_map_mods_on_a_generic_listing_are_not_scored():
    """A generic search could return anything, including text that matches a
    map-mod rule. Scoring it would invent a difficulty out of nothing."""
    config = make_config(scoring_base_default=40.0)
    d = decide(generic(mods=["Players are sent to the Void", "Area contains The Feared"]), config)
    assert d.difficulty == 0.0
    assert d.special_warnings == () and d.pairings == ()


# ------------------------------------------------- the pricing-budget guard


def test_a_generic_listing_never_registers_a_reward():
    """active_rewards() drives trade-API pricing, which runs on a budget
    that already peaks near its cap. A generic tab must add nothing to it."""
    from sniper.server import SniperServer

    config = make_config()
    server = SniperServer(config, PriceBook(config), NO_RULES)
    server._handle_listing(generic())
    assert server.active_rewards() == set()


def test_hello_search_kind_defaults_to_valdo():
    """An older userscript sends no search_kind; it must not be read as a
    generic tab, which would alert on every Valdo listing regardless of
    profit."""
    frame = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t"})
    assert isinstance(frame, Hello) and frame.search_kind == "valdo"
    frame = parse_frame({"type": "hello", "search_id": "s", "tab_id": "t", "search_kind": "any"})
    assert frame.search_kind == "any"


def test_generic_flag_defaults_off_for_an_old_userscript():
    assert listing().generic is False


# ---------------------------------------------------------------- ranking


def store_with(*decisions) -> AlertStore:
    store = AlertStore(expiry_s=60, max_display=5)
    for d in decisions:
        store.insert(d)
        # Windows' time.monotonic() ticks at ~15.6ms, so a shorter sleep
        # leaves both alerts with an IDENTICAL created_monotonic and the
        # recency tiebreak undefined. Ties fall back to insertion order.
        time.sleep(0.02)
    return store


def valdo_alert(listing_id: str, price: float):
    """A profitable Valdo listing; cheaper price -> higher profit -> higher
    yield, so price alone controls the Valdo ranking here."""
    config = make_config(min_yield=1.0, currency_rates={"chaos": 1, "divine": 200})
    book = PriceBook(config)
    book.set_trade_price("Foil Mageblood", 200.0)
    lst = listing(listing_id=listing_id, price={"amount": price, "currency": "divine"})
    return margin.evaluate(lst, book, config.thresholds, NO_RULES)


def test_valdo_only_ranking_is_unchanged_by_the_feature():
    """yield decides the hotkey target in ~16% of alerts; adding generic
    support must not disturb that when no generic tab is open."""
    store = store_with(valdo_alert("cheap", 10), valdo_alert("pricey", 150))
    assert [v.listing_id for v in store.views()] == ["cheap", "pricey"]


def test_a_newer_generic_alert_takes_the_top_slot():
    store = store_with(valdo_alert("valdo", 10), decide(generic(listing_id="ring")))
    assert store.views()[0].listing_id == "ring"


def test_a_generic_alert_stays_on_top_of_newer_valdo_alerts():
    store = store_with(
        decide(generic(listing_id="ring")),
        valdo_alert("pricey", 150),
        valdo_alert("cheap", 10),
    )
    ranked = [v.listing_id for v in store.views()]
    # the generic hit keeps the top slot, and below it yield still decides
    # the Valdo order - "cheap" outranks the newer "pricey"
    assert ranked == ["ring", "cheap", "pricey"]
    assert store.consume_best().decision.listing.listing_id == "ring"


def test_generic_alerts_among_themselves_rank_newest_first():
    store = store_with(
        decide(generic(listing_id="old")),
        decide(generic(listing_id="new")),
    )
    assert [v.listing_id for v in store.views()] == ["new", "old"]


def test_the_view_carries_the_flag_for_the_overlay():
    store = store_with(decide(generic(listing_id="ring")))
    assert store.views()[0].generic is True
    assert store.views()[0].item_name == "Doom Coil"


# --------------------------------------------------- stale-userscript guard


def test_expected_userscript_version_matches_the_shipped_script():
    """A tab keeps running the script it loaded with, so the app warns when
    one is behind. That warning is only meaningful if the constant tracks
    the file - this test is what makes forgetting to bump it fail loudly
    rather than silently disable the warning."""
    import re
    from pathlib import Path

    from sniper.server import EXPECTED_USERSCRIPT_VERSION

    src = Path("userscript/valdo-sniper.user.js").read_text(encoding="utf-8")
    header = re.search(r"^// @version\s+(\S+)", src, re.M).group(1)
    inline = re.search(r"SCRIPT_VERSION = '([^']+)'", src).group(1)
    assert header == inline == EXPECTED_USERSCRIPT_VERSION
