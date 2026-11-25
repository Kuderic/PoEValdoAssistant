"""Per-tab listing delay: spot a live-search tab that has gone slow.

A trade tab left open for hours gets slower as its results pile up: in the
logs a busy Mageblood tab went from ~1.6 s behind GGG's index time just after
loading to 4-6 s by evening, and dropped straight back to ~1.6 s on reload.
Quiet tabs barely moved. Nothing in the app can fix that page, but a reload
does, so the overlay names the tab that needs one.

A tab is SLOW when its recent median delay is
- at least SLOW_FLOOR_MS (small delays are not worth a reload), and
- at least SLOW_RATIO x what the same tab managed when its page was freshly
  loaded (a tab that was slow from the start will not be fixed by a
  reload), and
- at least SLOW_MARGIN_MS above the other tabs' recent medians (when GGG
  itself is slow, every tab is slow and reloading one changes nothing).
It stays slow until it drops back under the gentler RECOVER_* bars, so a
borderline tab does not flicker.

Only a real page reload starts a tab's history over. The app reconnecting
(an app restart reconnects every tab at once) leaves the page as slow as it
was, so the hello's page id and age decide (page_seen): a page first seen
already older than FRESH_PAGE_S has no fresh baseline, and is judged
against the other tabs alone.

Only network-captured listings have a delay (index_lag_ms); asyncio thread.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

WINDOW = 15  # listings per median; ~30 min on a busy tab
MIN_SAMPLES = 10
SLOW_FLOOR_MS = 2500.0
SLOW_RATIO = 1.6
SLOW_MARGIN_MS = 1000.0
RECOVER_FLOOR_MS = 2000.0
RECOVER_RATIO = 1.3
FRESH_PAGE_S = 900.0  # a page this young still shows its loaded-fresh delay


@dataclass
class _Tab:
    page_id: str | None = None
    # False when first seen on a page already open for a while: its early
    # delays are not a freshly-loaded baseline
    fresh: bool = True
    baseline: list[float] = field(default_factory=list)  # first WINDOW listings
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))
    slow: bool = False

    def median(self) -> float | None:
        return statistics.median(self.recent) if len(self.recent) >= MIN_SAMPLES else None

    def baseline_median(self) -> float | None:
        if not self.fresh or len(self.baseline) < MIN_SAMPLES:
            return None
        return statistics.median(self.baseline)


class TabLagMonitor:
    def __init__(self) -> None:
        self._tabs: dict[str, _Tab] = {}

    def page_seen(self, tab_id: str, page_id: str | None, page_age_s: float | None) -> None:
        """Every hello. A new page id is a reload: start over. Without one
        (a script older than 0.8.3) a new connection is the best guess at a
        reload, as before."""
        tab = self._tabs.get(tab_id)
        if tab is not None and (page_id is None or tab.page_id == page_id):
            return
        fresh = page_age_s is None or page_age_s < FRESH_PAGE_S
        self._tabs[tab_id] = _Tab(page_id=page_id, fresh=fresh)

    def forget(self, tab_id: str) -> None:
        """Tab disconnected. If the same page reconnects, its age says it is
        not fresh (see page_seen)."""
        self._tabs.pop(tab_id, None)

    def record(self, tab_id: str, lag_ms: float) -> bool | None:
        """Add one listing's delay. Returns the tab's new slow flag when it
        changed, else None."""
        tab = self._tabs.setdefault(tab_id, _Tab())
        if len(tab.baseline) < WINDOW:
            tab.baseline.append(lag_ms)
        tab.recent.append(lag_ms)
        slow = self._judge(tab_id, tab)
        if slow == tab.slow:
            return None
        tab.slow = slow
        return slow

    def _judge(self, tab_id: str, tab: _Tab) -> bool:
        recent = tab.median()
        if recent is None:
            return False
        if tab.slow:
            floor, ratio, margin = RECOVER_FLOOR_MS, RECOVER_RATIO, 0.0
        else:
            floor, ratio, margin = SLOW_FLOOR_MS, SLOW_RATIO, SLOW_MARGIN_MS
        if recent < floor:
            return False
        base = tab.baseline_median()
        if base is None and tab.fresh:
            return False  # still collecting the freshly-loaded baseline
        if base is not None and recent < ratio * base:
            return False
        others = [
            m for tid, t in self._tabs.items() if tid != tab_id and (m := t.median()) is not None
        ]
        if not others:
            # nothing to compare with: only a fresh baseline can say it slowed
            return base is not None
        return recent - statistics.median(others) >= margin

    def status(self, tab_id: str) -> tuple[float | None, float | None, bool]:
        """(recent median ms, freshly-loaded median ms, slow) for the overlay."""
        tab = self._tabs.get(tab_id)
        if tab is None:
            return None, None, False
        return tab.median(), tab.baseline_median(), tab.slow
