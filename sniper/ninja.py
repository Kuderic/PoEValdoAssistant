"""poe.ninja economy API client (endpoints verified 2026-08-09, see DESIGN.md).

Etiquette per poe.ninja/docs/api: descriptive User-Agent with a contact,
ETag conditional requests, no fast polling. All calls happen on the refresh
task, never on the detection hot path.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

BASE = "https://poe.ninja"
USER_AGENT = "valdo-map-sniper/0.1 (personal snipe-margin tool; contact: easymccarthy@gmail.com)"

BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 600.0


class NinjaBackoff(Exception):
    """Raised when a request is refused locally because backoff is active."""


class NinjaClient:
    def __init__(self, base_url: str = BASE, client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self._etags: dict[str, tuple[str, Any]] = {}  # url -> (etag, cached json)
        self._fail_count = 0
        self._next_allowed = 0.0  # monotonic

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

    async def get_json(self, path: str) -> Any:
        if self.in_backoff:
            raise NinjaBackoff(f"backoff active for {self.backoff_remaining:.0f}s more")
        url = f"{self._base}{path}"
        headers = {}
        cached = self._etags.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as e:
            self._enter_backoff(None)
            raise NinjaBackoff(f"transport error: {e}") from e

        if resp.status_code == 304 and cached:
            self._fail_count = 0
            return cached[1]
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = None
            raw = resp.headers.get("Retry-After")
            if raw and raw.isdigit():
                retry_after = float(raw)
            self._enter_backoff(retry_after)
            raise NinjaBackoff(f"HTTP {resp.status_code}")
        resp.raise_for_status()

        self._fail_count = 0
        # the ValdoMap overview is a large payload; parse it in the executor
        # so a listing arriving mid-refresh never waits behind json.loads
        data = await asyncio.get_running_loop().run_in_executor(None, resp.json)
        etag = resp.headers.get("ETag")
        if etag:
            self._etags[url] = (etag, data)
        return data

    async def leagues(self) -> list[dict]:
        return await self.get_json("/poe1/api/economy/leagues")

    async def current_league(self) -> str:
        """First entry is the current temporary challenge league (per docs)."""
        return str((await self.leagues())[0]["id"])

    async def valdo_overview(self, league: str) -> dict:
        return await self.get_json(
            f"/poe1/api/economy/stash/current/item/overview?league={league}&type=ValdoMap"
        )

    async def currency_overview(self, league: str) -> dict:
        return await self.get_json(
            f"/poe1/api/economy/stash/current/currency/overview?league={league}&type=Currency"
        )

    async def aclose(self) -> None:
        await self._client.aclose()
