"""Which capture path is producing listings, and is index lag arriving?

The DOM and network capture paths share the userscript's `seen` dedup, so
whichever fires first wins and the other silently no-ops. Only the network
path carries GGG's index time, so when the DOM path starts winning, index
lag quietly disappears from the UI with nothing to point at. This report
reads logs/ and says which path is actually winning, plus the userscript's
own counters (`capture_stats`) explaining why.

    python tools/capture_report.py
"""

from __future__ import annotations

import glob
import json
from collections import Counter


def load(event: str) -> list[dict]:
    out = []
    for path in sorted(glob.glob("logs/*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == event:
                    out.append(rec)
    return out


def main() -> None:
    listings = load("listing_received")[-300:]
    print(f"last {len(listings)} listings by capture path:")
    for src, n in Counter(e.get("capture") or "(pre-0.7.1 userscript)" for e in listings).items():
        print(f"   {n:>5}  {src}")

    lags = sorted(e["index_lag_ms"] for e in listings if e.get("index_lag_ms") is not None)
    if lags:
        print(f"\nindex lag from {len(lags)} listings:")
        for label, value in (
            ("fastest", lags[0]),
            ("p25", lags[len(lags) // 4]),
            ("median", lags[len(lags) // 2]),
            ("p90", lags[min(len(lags) - 1, len(lags) * 9 // 10)]),
            ("slowest", lags[-1]),
        ):
            print(f"   {label:>8}: {value:>7.0f} ms")
    else:
        print("\nno index lag captured yet (nothing came from the network path)")

    stats = load("capture_stats")
    if stats:
        print("\nuserscript capture counters (latest per tab):")
        latest: dict[str, dict] = {}
        for s in stats:
            latest[s["tab_id"]] = s
        totals: Counter = Counter()
        for s in latest.values():
            for k in (
                "payloads",
                "entries",
                "sent",
                "dedup",
                "silent",
                "unparsed",
                "viaFetch",
                "viaXhr",
                "domAlready",
            ):
                totals[k] += s.get(k, 0)
        for k, v in totals.items():
            print(f"   {k:>9}: {v}")
        print("\n   payloads=0        -> the hook never sees a response (wrapper bypassed)")
        print("   dedup high        -> the DOM path is winning the race")
        print("   silent high       -> the page-load quiet window is eating them")
        print("   unparsed>0        -> the fetch payload shape changed")
        print("   domAlready~=dedup -> the page rendered BEFORE our handler ran")
        print("   domAlready~=0     -> the key reached `seen` some other way")
        print("   viaFetch/viaXhr   -> which wrapper the site actually uses")
    else:
        print("\nno capture_stats yet: reload the trade tabs on userscript 0.7.2+")


if __name__ == "__main__":
    main()
