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
import time

import httpx

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

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self.in_backoff:
            raise TradeBackoff(f"backoff active for {self.backoff_remaining:.0f}s more")
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            self._enter_backoff(None)
            raise TradeBackoff(f"transport error: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raw = resp.headers.get("Retry-After")
            self._enter_backoff(float(raw) if raw and raw.isdigit() else None)
            raise TradeBackoff(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        self._fail_count = 0
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
        data = resp.json()
        return data["id"], (data.get("result") or [])[: self._max_listings], data.get("total", 0)

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
                await asyncio.sleep(self._spacing_s)
                query_id, hashes, total = await self._search(
                    league, name, identified=True, corrupted=False
                )
                mode = "identified"
                if total == 0:
                    await asyncio.sleep(self._spacing_s)
                    query_id, hashes, total = await self._search(
                        league, name, identified=True, corrupted=True
                    )
                    mode = "identified+corrupted"
        if not hashes:
            return [], mode
        await asyncio.sleep(self._spacing_s)
        resp = await self._request(
            "GET", f"/api/trade/fetch/{','.join(hashes)}", params={"query": query_id}
        )
        prices: list[tuple[float, str]] = []
        for item in resp.json().get("result") or []:
            price = ((item or {}).get("listing") or {}).get("price") or {}
            amount, currency = price.get("amount"), price.get("currency")
            if isinstance(amount, int | float) and amount > 0 and currency:
                prices.append((float(amount), str(currency).lower()))
        return prices, mode

    async def pause_between_rewards(self) -> None:
        await asyncio.sleep(self._spacing_s)

    async def aclose(self) -> None:
        await self._client.aclose()
