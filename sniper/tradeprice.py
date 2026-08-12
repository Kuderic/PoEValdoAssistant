"""Official trade API pricing for Valdo rewards (poe.ninja's per-map medians
proved inaccurate).

For each active reward we search the *unidentified* version of the unique
(`name` matches unid uniques on the trade API - verified live 2026-08-09;
foil is deliberately not filtered, foils list at market parity) and average
the cheapest listings.

ToS note (DESIGN.md): these are supplementary PRICING queries - one search
+ one fetch per active reward every ~10 minutes - not listing polling for
snipes. Rate-limit headers are respected and 429/Retry-After backs off.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time

import httpx

# fetch this many cheapest listings (trade API max per fetch); the average
# uses max_listings of them after outlier rejection
FETCH_LIMIT = 10


def select_representative(
    div_prices: list[float], max_listings: int, outlier_cutoff: float
) -> tuple[float | None, int]:
    """Average the cheapest max_listings after dropping price-fixer lowballs
    (anything below outlier_cutoff x median of the fetched sample - e.g. the
    perennial 2-chaos Mageblood listing). Returns (avg, dropped_count)."""
    if not div_prices:
        return None, 0
    ordered = sorted(div_prices)
    median = statistics.median(ordered)
    kept = [p for p in ordered if p >= outlier_cutoff * median]
    if not kept:  # degenerate sample; keep everything rather than nothing
        kept = ordered
    sample = kept[:max_listings]
    return sum(sample) / len(sample), len(ordered) - len(kept)


def _buckets(spec: str) -> list[tuple[int, int, int]]:
    """Parse a rate-limit header value: comma-separated `a:b:c` triplets."""
    out: list[tuple[int, int, int]] = []
    for triplet in spec.split(","):
        parts = triplet.strip().split(":")
        if len(parts) != 3:
            continue
        try:
            out.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return out


def rate_limit_delay(headers) -> float | None:
    """Seconds to wait before the next request, from the API's OWN published
    budget rather than a hard-coded guess.

    GGG sends `X-Rate-Limit-Rules: Ip,Account`, then per rule a limit header
    (`max_hits:period:restrict`) and a state header
    (`current_hits:period:restricted_for`). We take the worst bucket:

    - already restricted -> serve exactly that penalty;
    - bucket full        -> wait out its window;
    - otherwise          -> period/max_hits, the sustained rate that can
                            never fill the bucket in the first place.

    None when the headers are missing or unparseable, so the caller falls
    back to its fixed spacing.
    """
    rules = headers.get("X-Rate-Limit-Rules")
    if not rules:
        return None
    worst: float | None = None
    for rule in (r.strip() for r in rules.split(",") if r.strip()):
        limits = _buckets(headers.get(f"X-Rate-Limit-{rule}", ""))
        state = _buckets(headers.get(f"X-Rate-Limit-{rule}-State", ""))
        for (max_hits, period, _restrict), (hits, _p, restricted_for) in zip(
            limits, state, strict=False
        ):
            if restricted_for > 0:
                delay = float(restricted_for)
            elif max_hits <= 0:
                continue
            elif hits >= max_hits:
                delay = float(period)
            else:
                delay = period / max_hits
            worst = delay if worst is None else max(worst, delay)
    return worst


USER_AGENT = "valdo-map-sniper/0.3 (personal snipe-margin tool; contact: easymccarthy@gmail.com)"

BACKOFF_BASE_S = 60.0
BACKOFF_CAP_S = 1800.0
REQUEST_SPACING_S = 3.0  # between any two trade API calls (limits: 5/10s)


class TradeBackoff(Exception):
    """Request refused locally or by the API; retry after backoff."""


class TradePricer:
    def __init__(
        self,
        base_url: str = "https://www.pathofexile.com",
        max_listings: int = 3,
        client: httpx.AsyncClient | None = None,
        spacing_s: float = REQUEST_SPACING_S,
        corrupted_uniques: tuple[str, ...] = (),
        min_unid_listings: int = 10,
    ):
        self._corrupted_uniques = {u.lower() for u in corrupted_uniques}
        self._min_unid_listings = min_unid_listings
        headers = {"User-Agent": USER_AGENT}
        cookies = {}
        if os.environ.get("POESESSID"):
            cookies["POESESSID"] = os.environ["POESESSID"]
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=30.0, headers=headers, cookies=cookies
        )
        self._max_listings = max_listings
        self._spacing_s = spacing_s
        # pacing the API asked for via its rate-limit headers; 0 until
        # the first response teaches us the real budget
        self._api_spacing = 0.0
        self._fail_count = 0
        self._next_allowed = 0.0

    @property
    def in_backoff(self) -> bool:
        return time.monotonic() < self._next_allowed

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self._next_allowed - time.monotonic())

    def _enter_backoff(self, retry_after: float | None) -> None:
        self._fail_count += 1
        delay = min(BACKOFF_BASE_S * (2 ** (self._fail_count - 1)), BACKOFF_CAP_S)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._next_allowed = time.monotonic() + delay

    def _observe_limits(self, resp: httpx.Response) -> None:
        """Adopt the pacing the API just told us it wants."""
        delay = rate_limit_delay(resp.headers)
        if delay is not None:
            self._api_spacing = delay

    async def _wait(self) -> None:
        """Gap before the next call: whichever is longer, our configured
        floor or what the API's own budget headers ask for."""
        await asyncio.sleep(max(self._spacing_s, self._api_spacing))

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self.in_backoff:
            raise TradeBackoff(f"backoff active for {self.backoff_remaining:.0f}s more")
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            self._enter_backoff(None)
            raise TradeBackoff(f"transport error: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            self._observe_limits(resp)  # the 429 itself carries the penalty
            raw = resp.headers.get("Retry-After")
            retry_after = float(raw) if raw and raw.isdigit() else None
            if retry_after is None:
                retry_after = rate_limit_delay(resp.headers)
            self._enter_backoff(retry_after)
            raise TradeBackoff(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        self._fail_count = 0
        self._observe_limits(resp)
        return resp

    @staticmethod
    def unique_name(reward: str) -> str:
        """Reward 'Foil Mageblood' -> trade-searchable unique name."""
        return reward.removeprefix("Foil ").strip()

    async def _search(
        self, league: str, name: str, identified: bool, corrupted: bool
    ) -> tuple[str, list[str], int]:
        """One search; returns (query_id, cheapest hashes, total listings)."""
        query = {
            "query": {
                "status": {"option": "any"},
                "name": name,
                "filters": {
                    "misc_filters": {
                        "filters": {
                            "identified": {"option": "true" if identified else "false"},
                            "corrupted": {"option": "true" if corrupted else "false"},
                        }
                    }
                },
            },
            "sort": {"price": "asc"},
        }
        resp = await self._request("POST", f"/api/trade/search/{league}", json=query)
        # parse off the loop: pricing must never stall a listing decision
        data = await asyncio.get_running_loop().run_in_executor(None, resp.json)
        return data["id"], (data.get("result") or [])[:FETCH_LIMIT], data.get("total", 0)

    async def fetch_reward_listings(
        self, league: str, reward: str
    ) -> tuple[list[tuple[float, str]], str]:
        """Cheapest listed prices for the reward's unique, as ((amount,
        currency) pairs ascending, filter mode used).

        Filter ladder: unid+uncorrupted by default; below min_unid_listings
        fall back to identified+uncorrupted (e.g. Headhunter has ~2 unid
        listings); if that has none, identified+corrupted (e.g. Fortress
        Covenant). corrupted_uniques (Forbidden Flame/Flesh, Impossible
        Escape, Rain of Splinters) skip the ladder: unid+corrupted."""
        name = self.unique_name(reward)
        if name.lower() in self._corrupted_uniques:
            query_id, hashes, _total = await self._search(
                league, name, identified=False, corrupted=True
            )
            mode = "unid+corrupted"
        else:
            query_id, hashes, total = await self._search(
                league, name, identified=False, corrupted=False
            )
            mode = "unid"
            if total < self._min_unid_listings:
                await self._wait()
                query_id, hashes, total = await self._search(
                    league, name, identified=True, corrupted=False
                )
                mode = "identified"
                if total == 0:
                    await self._wait()
                    query_id, hashes, total = await self._search(
                        league, name, identified=True, corrupted=True
                    )
                    mode = "identified+corrupted"
        if not hashes:
            return [], mode
        await self._wait()
        resp = await self._request(
            "GET", f"/api/trade/fetch/{','.join(hashes)}", params={"query": query_id}
        )
        fetched = await asyncio.get_running_loop().run_in_executor(None, resp.json)
        prices: list[tuple[float, str]] = []
        for item in fetched.get("result") or []:
            price = ((item or {}).get("listing") or {}).get("price") or {}
            amount, currency = price.get("amount"), price.get("currency")
            if isinstance(amount, int | float) and amount > 0 and currency:
                prices.append((float(amount), str(currency).lower()))
        return prices, mode

    async def pause_between_rewards(self) -> None:
        await self._wait()

    async def aclose(self) -> None:
        await self._client.aclose()
