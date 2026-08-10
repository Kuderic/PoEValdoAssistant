"""TradePricer: unid-unique search/fetch against a mocked trade API."""

import json

import httpx
import pytest
from conftest import make_config

from sniper.prices import PriceBook
from sniper.tradeprice import TradeBackoff, TradePricer

SEARCH_RESPONSE = {"id": "abc123", "total": 6, "result": ["h1", "h2", "h3"]}
FETCH_RESPONSE = {
    "result": [
        {"listing": {"price": {"amount": 165, "currency": "divine"}}, "item": {}},
        {"listing": {"price": {"amount": 220, "currency": "divine"}}, "item": {}},
        None,  # trade API returns null entries sometimes
        {"listing": {"price": {"amount": 39600, "currency": "chaos"}}, "item": {}},
        {"listing": {"price": {"amount": 0, "currency": "divine"}}, "item": {}},  # junk
    ]
}


def pricer_with(handler) -> tuple[TradePricer, list]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped), base_url="https://www.pathofexile.com"
    )
    return TradePricer(client=client, spacing_s=0), calls


async def test_search_and_fetch_flow():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/trade/search/"):
            body = json.loads(request.content)
            assert body["query"]["name"] == "Mageblood"  # Foil prefix stripped
            assert body["query"]["filters"]["misc_filters"]["filters"]["identified"] == {
                "option": "false"
            }
            assert "foil_variation" not in json.dumps(body)  # foil doesn't matter
            assert body["sort"] == {"price": "asc"}
            return httpx.Response(200, json=SEARCH_RESPONSE)
        assert request.url.path == "/api/trade/fetch/h1,h2,h3"
        assert request.url.params["query"] == "abc123"
        return httpx.Response(200, json=FETCH_RESPONSE)

    pricer, calls = pricer_with(handler)
    listings = await pricer.fetch_unid_listings("Allflame", "Foil Mageblood")
    assert listings == [(165.0, "divine"), (220.0, "divine"), (39600.0, "chaos")]
    assert len(calls) == 2

    # averaging via the book: (165 + 220) * 180 + 39600 = 108900 / 3 = 36300 chaos
    book = PriceBook(make_config())
    chaos = [book.to_chaos(a, c) for a, c in listings]
    assert sum(chaos) / len(chaos) == (165 * 180 + 220 * 180 + 39600) / 3


async def test_empty_search_returns_no_listings():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "total": 0, "result": []})

    pricer, calls = pricer_with(handler)
    assert await pricer.fetch_unid_listings("Allflame", "Foil Nothing") == []
    assert len(calls) == 1  # no fetch call when search is empty


async def test_429_backoff_skips_network():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "300"})

    pricer, calls = pricer_with(handler)
    with pytest.raises(TradeBackoff):
        await pricer.fetch_unid_listings("Allflame", "Foil Mageblood")
    assert pricer.in_backoff
    assert pricer.backoff_remaining > 60
    with pytest.raises(TradeBackoff):
        await pricer.fetch_unid_listings("Allflame", "Foil Mageblood")
    assert len(calls) == 1  # second attempt refused locally


def test_unique_name_strips_foil():
    assert TradePricer.unique_name("Foil Mageblood") == "Mageblood"
    assert TradePricer.unique_name("Mageblood") == "Mageblood"


async def test_corrupted_false_by_default_and_true_for_exceptions():
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/trade/search/"):
            queries.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "total": 0, "result": []})
        raise AssertionError("no fetch expected")

    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped), base_url="https://www.pathofexile.com"
    )
    pricer = TradePricer(
        client=client,
        spacing_s=0,
        corrupted_uniques=(
            "Impossible Escape",
            "The Adorned",
            "Forbidden Flame",
            "Forbidden Flesh",
        ),
    )
    await pricer.fetch_unid_listings("Allflame", "Foil Mageblood")
    await pricer.fetch_unid_listings("Allflame", "Foil Impossible Escape")
    await pricer.fetch_unid_listings("Allflame", "Foil Forbidden Flame")

    def corrupted_of(q):
        return q["query"]["filters"]["misc_filters"]["filters"]["corrupted"]["option"]

    assert corrupted_of(queries[0]) == "false"  # normal unique: uncorrupted only
    assert corrupted_of(queries[1]) == "true"  # always-corrupted exception
    assert corrupted_of(queries[2]) == "true"


async def test_max_listings_defaults_to_cheapest_3():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/trade/search/"):
            return httpx.Response(
                200, json={"id": "q", "total": 9, "result": [f"h{i}" for i in range(9)]}
            )
        assert request.url.path == "/api/trade/fetch/h0,h1,h2"  # only 3 fetched
        return httpx.Response(200, json={"result": []})

    pricer, calls = pricer_with(handler)
    await pricer.fetch_unid_listings("Allflame", "Foil Mageblood")
    assert len(calls) == 2
