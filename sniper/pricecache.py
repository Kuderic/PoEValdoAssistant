"""Persisted reward prices, so a restart does not start blind.

Without this the app has no reward price until the trade API answers, which
means either holding every listing back (a blackout of tens of seconds, more
when rate limited) or judging them against poe.ninja's inaccurate median.
Neither is necessary: the prices from the previous session are usually
minutes old and good enough to trade on while a refresh runs.

Cached prices are deliberately marked `source="cached"` rather than
"trade" - they are provisional until the refresh lands, and the UI says so.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

CACHE_NAME = "price_cache.json"


def load(path: Path, max_age_s: float) -> dict[str, tuple[float, float]]:
    """reward -> (divine value, wall-clock time it was ORIGINALLY fetched),
    dropping anything older than max_age_s.

    The original timestamp travels with the price because the UI shows how
    long ago each price was calculated. Stamping a restored price with the
    load time instead would report a six-hour-old figure as "0m ago" -
    precisely the staleness the display exists to reveal.

    Never raises: a missing, unreadable or corrupt cache just means starting
    without one, which is the old behaviour.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("rewards") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    now = time.time()
    out: dict[str, tuple[float, float]] = {}
    for reward, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        try:
            divine = float(entry["divine"])
            saved_at = float(entry["at"])
        except (KeyError, TypeError, ValueError):
            continue
        # a clock that jumped backwards yields a negative age; treat the
        # entry as fresh rather than silently discarding a good price
        if divine > 0 and (now - saved_at) <= max_age_s:
            out[reward] = (divine, saved_at)
    return out


def save(path: Path, rewards: dict[str, tuple[float, float]]) -> None:
    """Write reward -> (divine value, when it was fetched). Wall clock, since
    monotonic does not survive a restart. Best effort: a failed write must
    never take the app down, it only costs the next start its head start."""
    data = {"rewards": {r: {"divine": v, "at": at} for r, (v, at) in rewards.items()}}
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash mid-write cannot corrupt the cache
    except OSError:
        pass
