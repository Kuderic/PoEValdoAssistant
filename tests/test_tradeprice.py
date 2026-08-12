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
from sniper.tradeprice import TradeBackoff, TradePricer, rate_limit_delay

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
            hashes = [f"h{i}" for i in range(min(total, 10))]
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
    assert fetches == ["/api/trade/fetch/" + ",".join(f"h{i}" for i in range(10))]


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


def test_select_representative_drops_price_fixers():
    from sniper.tradeprice import select_representative

    # the real Mageblood case: 2c troll + low-roll + real listings
    prices = [0.01, 50.0, 209.0, 215.0, 219.0, 220.0, 225.0, 230.0, 230.0, 235.0]
    avg, dropped = select_representative(prices, max_listings=3, outlier_cutoff=0.5)
    assert dropped == 2  # 0.01 and 50 are below half the ~219.5 median
    assert avg == (209.0 + 215.0 + 219.0) / 3

    # the Headhunter case: single troll among honest listings
    avg, dropped = select_representative(
        [0.01, 35.0, 35.0, 35.0, 36.0, 37.0], max_listings=3, outlier_cutoff=0.5
    )
    assert dropped == 1
    assert avg == 35.0


def test_select_representative_edge_cases():
    from sniper.tradeprice import select_representative

    assert select_representative([], 3, 0.5) == (None, 0)
    assert select_representative([80.0], 3, 0.5) == (80.0, 0)
    # all-identical: nothing dropped
    assert select_representative([5.0, 5.0, 5.0], 3, 0.5) == (5.0, 0)


# ------------------------------------------------------------- rate limiting
# GGG publishes the live budget on every response; pacing off those headers
# is what keeps us from earning a 429 in the first place.

LIMIT_HEADERS = {
    "X-Rate-Limit-Rules": "Ip,Account",
    "X-Rate-Limit-Ip": "8:10:60,15:60:120",
    "X-Rate-Limit-Ip-State": "1:10:0,1:60:0",
    "X-Rate-Limit-Account": "5:10:60",
    "X-Rate-Limit-Account-State": "1:10:0",
}


def test_rate_limit_delay_uses_the_tightest_sustained_rate():
    """8/10s -> 1.25s, 15/60s -> 4s, 5/10s -> 2s; the worst one wins."""
    assert rate_limit_delay(LIMIT_HEADERS) == pytest.approx(4.0)


def test_rate_limit_delay_waits_out_a_full_bucket():
    """A filled bucket must wait its whole window, not just the sustained
    rate - that 10s beats the 4s the other buckets would allow."""
    headers = dict(LIMIT_HEADERS, **{"X-Rate-Limit-Ip-State": "8:10:0,1:60:0"})
    assert rate_limit_delay(headers) == pytest.approx(10.0)


def test_rate_limit_delay_serves_an_active_restriction():
    headers = {
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "8:10:60",
        "X-Rate-Limit-Ip-State": "9:10:47",  # restricted for 47s
    }
    assert rate_limit_delay(headers) == pytest.approx(47.0)


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no headers at all
        {"X-Rate-Limit-Rules": "Ip"},  # rule named but no data
        {"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "garbage"},
    ],
)
def test_rate_limit_delay_none_when_unparseable(headers):
    """Caller then falls back to its fixed spacing rather than crashing."""
    assert rate_limit_delay(headers) is None


@pytest.mark.asyncio
async def test_pricer_adopts_the_api_pacing():
    def handler(request):
        if "search" in request.url.path:
            return httpx.Response(
                200, json={"id": "q", "total": 30, "result": ["h1"]}, headers=LIMIT_HEADERS
            )
        return httpx.Response(200, json=FETCH_RESPONSE, headers=LIMIT_HEADERS)

    pricer = pricer_with(handler)
    assert pricer._api_spacing == 0.0  # nothing learned yet
    await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    assert pricer._api_spacing == pytest.approx(4.0)  # taught by the headers


@pytest.mark.asyncio
async def test_429_restriction_headers_drive_the_backoff():
    """No Retry-After, but the state header says how long we are locked out."""

    def handler(request):
        return httpx.Response(
            429,
            json={},
            headers={
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "8:10:60",
                "X-Rate-Limit-Ip-State": "9:10:300",
            },
        )

    pricer = pricer_with(handler)
    with pytest.raises(TradeBackoff):
        await pricer.fetch_reward_listings("Allflame", "Foil Mageblood")
    assert pricer.backoff_remaining == pytest.approx(300, abs=2)
