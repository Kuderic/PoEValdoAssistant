"""Summarize hot-path latency from a session log.

Reads logs/sniper-*.jsonl (or a given file) and prints percentiles for:
- decide_ms:           frame received -> decision made (pure Python)
- frame_to_ui_ms:      frame received -> alert rendered + sound (trustworthy,
                       single monotonic clock; the <150ms M5 target)
- detected_to_decided_ms: browser detected_at -> decision (cross-clock,
                       approximate; useful for spotting browser-side delays)

Run: python tools/latency_report.py [logfile ...]
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

METRICS = ("decide_ms", "frame_to_ui_ms", "detected_to_decided_ms")


def collect(paths: list[Path]) -> dict[str, list[float]]:
    series: dict[str, list[float]] = {m: [] for m in METRICS}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            for metric in METRICS:
                value = entry.get(metric)
                if isinstance(value, int | float):
                    series[metric].append(float(value))
    return series


def report(series: dict[str, list[float]]) -> str:
    lines = [f"{'metric':<26} {'n':>5} {'p50':>8} {'p90':>8} {'p99':>8} {'max':>8}"]
    for metric, values in series.items():
        if not values:
            lines.append(f"{metric:<26} {0:>5}")
            continue
        values.sort()
        # inclusive: percentiles never extrapolate beyond observed min/max
        q = (
            statistics.quantiles(values, n=100, method="inclusive")
            if len(values) >= 2
            else values * 99
        )
        lines.append(
            f"{metric:<26} {len(values):>5} {q[49]:>8.1f} {q[89]:>8.1f} "
            f"{q[98]:>8.1f} {values[-1]:>8.1f}"
        )
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) > 1:
        paths = [Path(a) for a in sys.argv[1:]]
    else:
        paths = sorted(Path("logs").glob("sniper-*.jsonl"))
    if not paths:
        raise SystemExit("no log files found (logs/sniper-*.jsonl)")
    print(f"reading {len(paths)} file(s)")
    print(report(collect(paths)))


if __name__ == "__main__":
    main()
