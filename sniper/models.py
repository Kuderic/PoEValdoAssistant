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
    # GGG's own index time (network capture only; None from the DOM path)
    indexed_at: str | None = None
    # which capture path produced this: "net" (the page's own fetch payload,
    # carries indexed_at) or "dom" (the rendered row). Logged so a silent
    # regression in either path is visible instead of guessed at.
    capture: str = ""

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
    # userscript @version. A browser tab keeps running the script it loaded
    # with until it is reloaded, so "which version is actually live" is not
    # otherwise answerable from the logs when diagnosing a missed snipe.
    version: str | None = None
    # network-capture counters (see the userscript's netStats): why the
    # network path did or did not forward listings on this tab
    net_stats: dict | None = None


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
        return Hello(
            search_id=search_id,
            tab_id=tab_id,
            search_reward=reward if isinstance(reward, str) and reward else None,
            version=version if isinstance(version, str) and version else None,
            net_stats=stats if isinstance(stats, dict) else None,
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
        return Listing(
            search_id=search_id,
            tab_id=tab_id,
            listing_id=listing_id,
            item_name=item_name,
            price=Price(amount=amount, currency=currency.lower()),
            seller=seller,
            reward=reward_raw or None,
            mods=tuple(mods_raw),
            row_index=row_index if isinstance(row_index, int) else -1,
            detected_at=detected_at,
            indexed_at=indexed_at if isinstance(indexed_at, str) and indexed_at else None,
            capture=msg.get("capture") if msg.get("capture") in ("net", "dom") else "",
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
    profit_div: float | None = None  # (reference - listing) in divine orbs
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
    required_profit_div: float | None = None  # threshold * difficulty / 100
