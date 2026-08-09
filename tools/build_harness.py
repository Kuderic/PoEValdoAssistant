"""Inject the trimmed snapshot rows into tests/harness/harness.html.

file:// pages cannot fetch local files, so the harness carries the snapshot
rows inline between marker comments. Re-run after refreshing
tests/fixtures/results_snapshot.html from a new Ctrl+S page save.

Run: python tools/build_harness.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "results_snapshot.html"
HARNESS = ROOT / "tests" / "harness" / "harness.html"
START = "<!-- SNAPSHOT-ROWS-START -->"
END = "<!-- SNAPSHOT-ROWS-END -->"


def main() -> None:
    snapshot = FIXTURE.read_text(encoding="utf-8")
    harness = HARNESS.read_text(encoding="utf-8")
    if START not in harness or END not in harness:
        raise SystemExit(f"markers not found in {HARNESS}")
    pattern = re.compile(re.escape(START) + ".*?" + re.escape(END), re.DOTALL)
    harness = pattern.sub(f"{START}\n{snapshot}\n{END}", harness, count=1)
    HARNESS.write_text(harness, encoding="utf-8")
    rows = len(re.findall(r'class="row(?: gone)?" data-id=', snapshot))
    print(f"injected {len(snapshot)} bytes ({rows} rows) into {HARNESS.name}")


if __name__ == "__main__":
    main()
