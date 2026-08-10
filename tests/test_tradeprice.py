"""TradePricer: staged filter ladder against a mocked trade API.

Ladder: unid+uncorrupted -> (fewer than min_unid_listings) identified+
uncorrupted -> (none) identified+corrupted. corrupted_uniques skip the
ladder and search unid+corrupted directly.
"""

import json

import httpx
import pytest
from conftest import make_config

from sniper.prices import PriceBook
from sniper.tradeprice import TradeBackoff, TradePricer

FETCH_RESPONSE = {
    "result": [
        {"listing": {"price": {"amount": 165, "currency": "divine"}}, "item": {}},
        {"listing": {"price": {"amount": 220, "currency": "divine"}}, "item": {}},
        None,  # trade API returns null entries sometimes
        {"listing": {"price": {"amount": 39600, "currency": "chaos"}}, "item": {}},
        {"listing": {"price": {"amount": 0, "currency": "divine"}}, "item": {}},  # junk
    ]
}

CORRUPTED_UNIQUES = (
    "Impossible Escape",
    "Forbidden Flame",
    "Forbidden Flesh",
    "Rain of Splinters",
)


def query_filters(request: httpx.Request) -> tuple[str, str]:
    body = json.loads(request.content)
    misc = body["query"]["filters"]["misc_filters"]["filters"]
    return misc["identified"]["option"], misc["corrupted"]["option"]


def make_handler(totals: dict[tuple[str, str], int]):
    """totals: (identified, corrupted) option pair -> total listings."""
    searches: list[tuple[str, str]] = []
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/trade/search/"):
            pair = query_filters(request)
            searches.append(pair)
            total = totals.get(pair, 0)
            hashes = [f"h{i}" for i in range(min(total, 3))]
            return httpx.Response(200, json={"id": "q1", "total": total, "result": hashes})
        fetches.append(request.url.path)
        return httpx.Response(200, json=FETCH_RESPONSE)

    return handler, searches, fetches


def pricer_with(handler, **kwargs) -> TradePricer:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.pathofexile.com"
    )
    kwargs.setdefault("corrupted_uniques", CORRUPTED_UNIQUES)
    return TradePricer(client=client, spacing_s=0, **kwargs)


async def test_plenty_of_unid_listings_single_search():
    handler, searches, fetches = make_handler({("false", "false"): 25})
    pricer = pricer_with(handler)
    listings, mode = await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    assert mode == "unid"
    assert searches == [("false", "false")]
    assert listings == [(165.0, "divine"), (220.0, "divine"), (39600.0, "chaos")]
    assert fetches == ["/api/trade/fetch/h0,h1,h2"]


async def test_few_unid_falls_back_to_identified():
    # Headhunter case: 2 unid listings < 10 -> identified+uncorrupted
    handler, searches, fetches = make_handler({("false", "false"): 2, ("true", "false"): 40})
    pricer = pricer_with(handler)
    listings, mode = await pricer.fetch_reward_listings("Allflame", "Foil Headhunter")
    assert mode == "identified"
    assert searches == [("false", "false"), ("true", "false")]
    assert len(fetches) == 1


async def test_no_uncorrupted_at_all_falls_back_to_corrupted():
    # Fortress Covenant case: no unid, no identified+uncorrupted
    handler, searches, fetches = make_handler(
        {("false", "false"): 0, ("true", "false"): 0, ("true", "true"): 15}
    )
    pricer = pricer_with(handler)
    listings, mode = await pricer.fetch_reward_listings("Allflame", "Foil Fortress Covenant")
    assert mode == "identified+corrupted"
    assert searches == [("false", "false"), ("true", "false"), ("true", "true")]
    assert listings  # priced from corrupted identified copies


async def test_corrupted_exceptions_skip_the_ladder():
    handler, searches, fetches = make_handler({("false", "true"): 7})
    pricer = pricer_with(handler)
    for reward in ("Foil Forbidden Flame", "Foil Rain of Splinters", "Foil Impossible Escape"):
        listings, mode = await pricer.fetch_reward_listings("Allflame", reward)
        assert mode == "unid+corrupted"
    assert searches == [("false", "true")] * 3  # one search each, no ladder


async def test_adorned_no_longer_an_exception():
    handler, searches, fetches = make_handler({("false", "false"): 25})
    pricer = pricer_with(handler)
    _, mode = await pricer.fetch_reward_listings("Allflame", "Foil The Adorned")
    assert mode == "unid"


async def test_empty_everywhere_returns_no_listings():
    handler, searches, fetches = make_handler({})
    pricer = pricer_with(handler)
    listings, mode = await pricer.fetch_reward_listings("Allflame", "Foil Nothing")
    assert listings == [] and mode == "identified+corrupted"
    assert fetches == []  # nothing to fetch at any stage


async def test_429_backoff_skips_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "300"})

    pricer = pricer_with(handler)
    with pytest.raises(TradeBackoff):
        await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    assert pricer.in_backoff
    assert pricer.backoff_remaining > 60
    with pytest.raises(TradeBackoff):
        await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    assert len(calls) == 1  # second attempt refused locally


def test_unique_name_strips_foil():
    assert TradePricer.unique_name("Foil Mageblood") == "Mageblood"
    assert TradePricer.unique_name("Mageblood") == "Mageblood"


async def test_averaging_via_book():
    handler, *_ = make_handler({("false", "false"): 25})
    pricer = pricer_with(handler)
    listings, _ = await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    book = PriceBook(make_config())
    divs = [book.to_divine(a, c) for a, c in listings]
    assert divs == [165.0, 220.0, 220.0]  # 39600 chaos / 180
