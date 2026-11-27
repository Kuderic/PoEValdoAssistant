"""Re-travel in the userscript: refresh a row already traveled to, then click
its Travel button ONCE - never more, never on timeout (one user input = one
travel, the ToS boundary).

The real retravel()/travelReady() source is cut out of the userscript and run
in node against stub rows, so the behaviour under test is the shipped code.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_reward_dropdown import Tree

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "userscript" / "valdo-sniper.user.js").read_text(encoding="utf-8")
RESULTS = ROOT / "tests" / "fixtures" / "results_snapshot.html"


def function_source(name: str) -> str:
    start = SCRIPT.index(f"  function {name}(")
    return SCRIPT[start : SCRIPT.index("\n  }\n", start) + 4]


def selector(name: str) -> str:
    return re.search(rf"^\s*{name}: '([^']+)'", SCRIPT, re.M).group(1)


def test_every_live_row_in_the_real_page_has_its_refresh_button():
    left_cls, button = selector("rowRefresh").split()
    assert left_cls == ".left" and button == "button.refresh"
    tree = Tree()
    tree.feed(RESULTS.read_text(encoding="utf-8"))
    rows = [
        n
        for n in tree.root.walk()
        if {"row"} <= n.classes and "data-id" in n.attrs and "gone" not in n.classes
    ]
    assert rows
    for row in rows:
        left = row.find("left")
        assert left is not None
        assert any(c.tag == "button" and "refresh" in c.classes for c in left.children)


def test_only_a_retravel_frame_takes_the_refresh_path():
    body = function_source("handleClickTravel")
    route = body.index("if (msg.retravel === true) return retravel(id, reply);")
    assert route < body.index("if (clickedIds.has(id)) return reply(false, 'already_clicked');")


HARNESS = r"""
const SELECTORS = { travelButton: 'T', rowRefresh: 'R', rowError: 'E' };
const CONFIRM_TEXT = /in demand|teleport anyway/i;
const RETRAVEL_WINDOW_MS = 300;
const clickedIds = new Set();
const replies = [];
let travelClicks = 0;
let observedContainer = null;
const observers = [];
class MutationObserver {
  constructor(cb) { this.cb = cb; this.on = false; observers.push(this); }
  observe() { this.on = true; }
  disconnect() { this.on = false; }
}
const mutate = () => observers.forEach((o) => o.on && o.cb());
const classList = (set) => ({ contains: (c) => set.has(c) });
function button(text, classes, disabled) {
  const set = new Set(classes);
  return { textContent: text, disabled, classList: classList(set), _set: set,
           click() { travelClicks += 1; } };
}
const MODE = process.argv[process.argv.length - 1];
const travel = MODE === 'enabled'
  ? button('Travel to Hideout', [], false)
  : button('Travel to Hideout', ['expired', 'disabled'], true);
const refresh = { click() {
  if (MODE === 'refreshes') setTimeout(() => {
    travel.disabled = false; travel._set.delete('disabled'); travel._set.delete('expired');
    mutate(); mutate(); setTimeout(mutate, 20);  // several mutations after it
  }, 30);
} };
const row = { isConnected: true, classList: classList(new Set()),
  querySelector: (s) => (s === 'T' ? travel : s === 'R' ? refresh : null) };
const resolveRow = () => row;
const unavailableError = () => null;
const watchAfterClick = () => {};
const retravelling = new Set();
__SOURCE__
const reply = (ok, reason) => replies.push([ok, reason]);
retravel('L1', reply);
retravel('L1', reply);  // a second request while the first is refreshing
setTimeout(() => {
  console.log(JSON.stringify({ travelClicks, replies, busy: retravelling.size }));
}, 600);
"""


def run(mode: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    source = function_source("travelReady") + function_source("retravel")
    out = subprocess.run(
        [node, "-e", HARNESS.replace("__SOURCE__", source), "--", mode],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(out.stdout)


def test_a_refreshed_row_is_traveled_to_exactly_once():
    result = run("refreshes")
    assert result["travelClicks"] == 1
    assert result["replies"] == [[False, "retravel_busy"], [True, "refreshed"]]
    assert result["busy"] == 0


def test_a_row_that_never_refreshes_is_not_clicked():
    result = run("stuck")
    assert result["travelClicks"] == 0
    assert result["replies"] == [[False, "retravel_busy"], [False, "refresh_timeout"]]
    assert result["busy"] == 0


def test_a_still_enabled_button_needs_no_refresh():
    result = run("enabled")
    # one click for the first request; the second sees the id clicked but
    # the button still enabled in this stub - in the page a used button is
    # disabled at once, which the refresh path covers
    assert result["travelClicks"] == 2
    assert result["replies"] == [[True, "retravel"], [True, "retravel"]]
