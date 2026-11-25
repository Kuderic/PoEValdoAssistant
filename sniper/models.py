"""Shared data models and websocket frame validation (schemas in DESIGN.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def fnv1a32(text: str) -> str:
    """FNV-1a 32-bit over UTF-8 bytes, lowercase hex.

    Must stay byte-identical with fnv1a32() in userscript/valdo-sniper.user.js;
    parity is enforced by tests/test_hash_parity.py against shared vectors.
    """
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return format(h, "08x")


def _iso_delta_ms(start: str | None, end: str | None) -> float | None:
    """(end - start) in ms for two ISO-8601 stamps; None if either is
    missing or unparseable."""
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    if a.tzinfo is None or b.tzinfo is None:
        return None
    return (b - a).total_seconds() * 1000


@dataclass(frozen=True)
class Price:
    amount: float
    currency: str


@dataclass(frozen=True)
class StashSpot:
    """Where the item sits in the seller's shop: tab name and 0-based cell."""

    name: str
    x: int
    y: int


# Largest stash grid (a quad tab is 24x24); anything outside is not a cell
STASH_MAX_CELLS = 24


@dataclass(frozen=True)
class FetchTiming:
    """Where a network-captured listing's delay went inside the browser tab.

    push_to_fetch_ms: from the live socket's push to the page asking for the
    listing's details (None when no push was seen - an older script, or the
    socket opened before the script could hook it). fetch_ms: that request's
    round trip. hidden: whether the tab was in the background."""

    push_to_fetch_ms: float | None
    fetch_ms: float | None
    hidden: bool | None


def _ms(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value < 600_000 else None


def _parse_timing(raw) -> FetchTiming | None:
    if not isinstance(raw, dict):
        return None
    hidden = raw.get("hidden")
    return FetchTiming(
        push_to_fetch_ms=_ms(raw.get("push_to_fetch_ms")),
        fetch_ms=_ms(raw.get("fetch_ms")),
        hidden=hidden if isinstance(hidden, bool) else None,
    )


def _parse_stash(raw) -> StashSpot | None:
    if not isinstance(raw, dict):
        return None
    x, y, name = raw.get("x"), raw.get("y"), raw.get("name")
    for v in (x, y):
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < STASH_MAX_CELLS:
            return None
    return StashSpot(name=name if isinstance(name, str) else "", x=x, y=y)


@dataclass(frozen=True)
class Listing:
    search_id: str
    tab_id: str
    listing_id: str
    item_name: str
    price: Price
    seller: str
    reward: str | None
    mods: tuple[str, ...]
    row_index: int
    detected_at: str
    # A listing from a NON-Valdo live search: the user's own search is the
    # filter, so there is no reward to price, no map mods to score, and
    # nothing to hold for. It alerts on arrival (see margin.evaluate).
    generic: bool = False
    # the item's base type ("Vermillion Ring"); for a rare it is the half
    # worth reading, while item_name is the random rare name
    type_line: str | None = None
    # GGG's own index time (network capture only; None from the DOM path)
    indexed_at: str | None = None
    # which capture path produced this: "net" (the page's own fetch payload,
    # carries indexed_at) or "dom" (the rendered row). Logged so a silent
    # regression in either path is visible instead of guessed at.
    capture: str = ""
    # the seller's tab and the item's cell in it (network capture only)
    stash: StashSpot | None = None
    # where the delay went inside the tab (network capture, script 0.8.3+)
    timing: FetchTiming | None = None

    @property
    def index_to_push_ms(self) -> float | None:
        """The part of index_lag_ms spent before the live socket pushed the
        listing: GGG's side, plus clock skew and the up-to-1s truncation of
        the second-granular index time. None without push timing."""
        lag, t = self.index_lag_ms, self.timing
        if lag is None or t is None or t.push_to_fetch_ms is None or t.fetch_ms is None:
            return None
        return lag - t.push_to_fetch_ms - t.fetch_ms

    @property
    def index_lag_ms(self) -> float | None:
        """Milliseconds a listing was already live before the browser saw it
        - the head start every other sniper had on it.

        The two timestamps come from DIFFERENT clocks (GGG's server vs the
        browser), so this carries any skew between them, and `indexed` is
        only second-granular. Trustworthy for spotting multi-second lag,
        not for sub-second precision.
        """
        return _iso_delta_ms(self.indexed_at, self.detected_at)

    @property
    def price_key(self) -> str:
        """Reference prices key off the reward (the value driver); the random
        map name is only a fallback."""
        return self.reward or self.item_name


@dataclass(frozen=True)
class Hello:
    search_id: str
    tab_id: str
    search_reward: str | None = None  # majority reward of the tab's visible rows
    # "valdo" (a map-reward search, scored and priced) or "any" (any other
    # live search: alerts on arrival, see margin._generic_decision)
    search_kind: str = "valdo"
    # False when the tab is on a plain (non-live) search page: its rows were
    # not pushed, so it is no search of ours. Absent means True, so a
    # userscript older than 0.8.4 keeps working as before.
    live: bool = True
    # userscript @version. A browser tab keeps running the script it loaded
    # with until it is reloaded, so "which version is actually live" is not
    # otherwise answerable from the logs when diagnosing a missed snipe.
    version: str | None = None
    # network-capture counters (see the userscript's netStats): why the
    # network path did or did not forward listings on this tab
    net_stats: dict | None = None
    # one id per page load, and the page's age: tells a real reload (which
    # cures a slow tab) from the app reconnecting to a page that stayed open
    page_id: str | None = None
    page_age_s: float | None = None


@dataclass(frozen=True)
class ClickResult:
    listing_id: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class FrameError:
    reason: str
    raw: dict | None = None


Frame = Hello | Listing | ClickResult | FrameError


def _require_str(msg: dict, key: str, errors: list[str], allow_empty: bool = False) -> str:
    value = msg.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        errors.append(f"missing/invalid {key}")
        return ""
    return value


def parse_frame(msg: object) -> Frame:
    """Validate one decoded JSON object into a typed frame.

    Never raises: malformed input returns FrameError so a bad tab cannot
    crash the server.
    """
    if not isinstance(msg, dict):
        return FrameError(reason="frame is not an object")
    mtype = msg.get("type")
    errors: list[str] = []

    if mtype == "hello":
        search_id = _require_str(msg, "search_id", errors)
        tab_id = _require_str(msg, "tab_id", errors)
        if errors:
            return FrameError(reason="; ".join(errors), raw=msg)
        reward = msg.get("search_reward")
        version = msg.get("version")
        stats = msg.get("net_stats")
        page_id, page_age = msg.get("page_id"), msg.get("page_age_s")
        return Hello(
            search_id=search_id,
            tab_id=tab_id,
            search_reward=reward if isinstance(reward, str) and reward else None,
            search_kind="any" if msg.get("search_kind") == "any" else "valdo",
            live=msg.get("live") is not False,
            version=version if isinstance(version, str) and version else None,
            net_stats=stats if isinstance(stats, dict) else None,
            page_id=page_id if isinstance(page_id, str) and page_id else None,
            page_age_s=float(page_age)
            if isinstance(page_age, (int, float)) and not isinstance(page_age, bool)
            else None,
        )

    if mtype == "click_result":
        listing_id = _require_str(msg, "listing_id", errors)
        ok = msg.get("ok")
        if not isinstance(ok, bool):
            errors.append("missing/invalid ok")
        if errors:
            return FrameError(reason="; ".join(errors), raw=msg)
        return ClickResult(listing_id=listing_id, ok=bool(ok), reason=str(msg.get("reason", "")))

    if mtype == "new_listing":
        search_id = _require_str(msg, "search_id", errors)
        tab_id = _require_str(msg, "tab_id", errors)
        listing_id = _require_str(msg, "listing_id", errors)
        item_name = _require_str(msg, "item_name", errors)
        seller = _require_str(msg, "seller", errors)
        detected_at = _require_str(msg, "detected_at", errors)

        price_raw = msg.get("price")
        amount, currency = 0.0, ""
        if not isinstance(price_raw, dict):
            errors.append("missing/invalid price")
        else:
            currency = str(price_raw.get("currency") or "")
            if not currency:
                errors.append("missing price.currency")
            try:
                amount = float(price_raw.get("amount"))
            except (TypeError, ValueError):
                errors.append("missing/invalid price.amount")
            else:
                if amount <= 0:
                    errors.append("price.amount must be > 0")

        mods_raw = msg.get("mods", [])
        if not isinstance(mods_raw, list) or any(not isinstance(m, str) for m in mods_raw):
            errors.append("invalid mods")
            mods_raw = []

        reward_raw = msg.get("reward")
        if reward_raw is not None and not isinstance(reward_raw, str):
            errors.append("invalid reward")
            reward_raw = None

        if errors:
            return FrameError(reason="; ".join(errors), raw=msg)
        row_index = msg.get("row_index")
        indexed_at = msg.get("indexed_at")
        type_line = msg.get("type_line")
        reward = reward_raw or None
        return Listing(
            search_id=search_id,
            tab_id=tab_id,
            listing_id=listing_id,
            item_name=item_name,
            price=Price(amount=amount, currency=currency.lower()),
            seller=seller,
            reward=reward,
            mods=tuple(mods_raw),
            row_index=row_index if isinstance(row_index, int) else -1,
            detected_at=detected_at,
            indexed_at=indexed_at if isinstance(indexed_at, str) and indexed_at else None,
            capture=msg.get("capture") if msg.get("capture") in ("net", "dom") else "",
            # Every Valdo map carries the Reward property (it is what gets
            # priced), so a listing without one is a non-Valdo hit whatever
            # the tab claims. The tab's own flag can be wrong: after an
            # in-place search edit its page-load query state goes stale, and
            # with fewer than 5 rows on screen the row fallback cannot tell
            # either, so a real hit (an Ephemeral Edge) arrived generic:false
            # and sat silently as no_reference. In ~37k logged listings a
            # reward-less one was never a Valdo map.
            generic=msg.get("generic") is True or reward is None,
            type_line=type_line if isinstance(type_line, str) and type_line else None,
            stash=_parse_stash(msg.get("stash")),
            timing=_parse_timing(msg.get("timing")),
        )

    return FrameError(reason=f"unknown frame type {mtype!r}", raw=msg)


@dataclass(frozen=True)
class ModHit:
    label: str
    severity: str  # "warn" | "block"


@dataclass(frozen=True)
class Reference:
    key: str
    display_amount: float
    display_currency: str
    chaos_value: float
    source: str  # "manual" | "live" | "stale"


@dataclass(frozen=True)
class Decision:
    listing: Listing
    key: str
    verdict: str  # "alert" | "below_threshold" | "blocked" | "no_reference" | "no_rate"
    threshold: float  # base divine-profit threshold (before difficulty)
    reference: Reference | None = None
    listing_chaos: float | None = None
    net_div: float | None = None  # (reference - listing) in divine orbs
    margin: float | None = None  # same as fraction of reference, for display
    currency_mismatch: bool = False
    mod_hits: tuple[ModHit, ...] = field(default_factory=tuple)
    difficulty: float = 0.0  # mod-based difficulty score (modrules.ModScoring)
    difficulty_mods: tuple[str, ...] = field(default_factory=tuple)
    # (mod text, scoring note like "×1.8"/"base 100"/"", severity level
    # "none"|"yellow"|"red") per mod line
    mods_annotated: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    special_warnings: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (label, color)
    # deadly mod pairings present: (label, note, multiplier)
    pairings: tuple[tuple[str, str, float], ...] = field(default_factory=tuple)
    required_net_div: float | None = None  # threshold * difficulty / 100
    # failure risks present: (label, note, fail_chance 0-1), their combined
    # chance, and what that chance costs in divines (fail_chance x market
    # price) - already taken off net_div, which is an expected value
    risks: tuple[tuple[str, str, float], ...] = field(default_factory=tuple)
    fail_chance: float = 0.0
    risk_cost_div: float | None = None
