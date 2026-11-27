"""A tab whose live search the trade site has stopped must be loud.

The site shows "Live Search retry limit exceeded / Please refresh the page and
try again" after 5 failed reconnects, and "Rate-limiting is active for your
account" when the server cuts the socket - then calls cancelLive(). The tab
and the userscript stay up and keep checking in, so before this the app
showed a healthy tab that would never push another listing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tkinter as tk
from pathlib import Path

import pytest
import websockets
from conftest import make_config
from test_server import running_server  # noqa: F401 - pytest fixture

import sniper.server as srv
from sniper.bus import Bus, TabsChanged
from sniper.models import Hello, parse_frame
from sniper.overlay import Overlay
from sniper.server import EXPECTED_USERSCRIPT_VERSION as CURRENT

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "userscript" / "valdo-sniper.user.js").read_text(encoding="utf-8")


def hello(**extra) -> dict:
    return {"type": "hello", "search_id": "s", "tab_id": "t1", **extra}


@pytest.mark.parametrize(
    ("sent", "parsed"),
    [
        ("retry_limit", "retry_limit"),
        ("rate_limited", "rate_limited"),
        (None, None),
        ("something else", None),
        (["retry_limit"], None),  # unhashable: must not raise
    ],
)
def test_hello_carries_the_live_error(sent, parsed):
    frame = parse_frame(hello(live_error=sent))
    assert isinstance(frame, Hello)
    assert frame.live_error == parsed


async def test_a_stopped_tab_stays_listed_and_is_logged(running_server, monkeypatch):  # noqa: F811
    server, config, decisions, clicks = running_server
    logged: list[dict] = []
    real_event = srv.event
    monkeypatch.setattr(
        srv,
        "event",
        lambda name, **f: (
            logged.append({"event": name, **f})
            if name == "live_search_stopped"
            else real_event(name, **f)
        ),
    )
    url = f"ws://127.0.0.1:{config.server.port}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(hello(search_reward="Foil Mageblood")))
        await asyncio.sleep(0.2)
        assert server.tab_snapshot()[0]["live_error"] is None
        # cancelLive may also move the page off its live URL: still listed
        stopped = hello(search_reward="Foil Mageblood", live=False, live_error="retry_limit")
        await ws.send(json.dumps(stopped))
        await ws.send(json.dumps(stopped))  # next heartbeat: logged once only
        await asyncio.sleep(0.2)
        assert [t["live_error"] for t in server.tab_snapshot()] == ["retry_limit"]
        assert len(logged) == 1
        assert logged[0]["reason"] == "retry_limit"
        assert logged[0]["search_reward"] == "Foil Mageblood"
        # a plain non-live page with no error is still dropped, as before
        await ws.send(json.dumps(hello(live=False)))
        await asyncio.sleep(0.2)
        assert server.tab_snapshot() == []


def tab(reward, live_error=None, tab_id=None):
    return {
        "tab_id": tab_id or reward,
        "search_reward": reward,
        "hello_age_s": 5,
        "version": CURRENT,
        "search_kind": "valdo",
        "live_error": live_error,
    }


@pytest.fixture
def overlay():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    bus = Bus()
    ov = Overlay(root, bus, make_config())
    sounds: list[str] = []
    ov._play_sound = lambda kind="valdo": sounds.append(kind)
    yield ov, bus, sounds
    root.destroy()


def test_the_overlay_bars_the_stopped_tab_and_sounds_once(overlay):
    ov, bus, sounds = overlay
    bus.put(TabsChanged((tab("Foil Mageblood", "retry_limit"), tab("Foil Nimis"))))
    ov._drain_once()
    assert ov._dead_bar.winfo_manager() == "pack"
    assert ov._dead_label.cget("text") == "⛔  LIVE SEARCH STOPPED  Mageblood"
    assert "F5" in ov._dead_hint.cget("text")
    assert "stopped: Mageblood" in ov._tabs_label._label.cget("text")
    assert sounds == ["error"]
    # heartbeats with the same dead tab: no repeat alarm
    bus.put(TabsChanged((tab("Foil Mageblood", "retry_limit"), tab("Foil Nimis"))))
    ov._drain_once()
    assert sounds == ["error"]
    # refreshed: the new page load reports no error and the bar clears
    bus.put(TabsChanged((tab("Foil Mageblood"), tab("Foil Nimis"))))
    ov._drain_once()
    assert ov._dead_bar.winfo_manager() == ""
    assert "stopped" not in ov._tabs_label._label.cget("text")


def test_a_rate_limit_says_to_wait(overlay):
    ov, bus, sounds = overlay
    bus.put(TabsChanged((tab("Foil Mageblood", "rate_limited"),)))
    ov._drain_once()
    assert "rate-limiting" in ov._dead_hint.cget("text")


def test_a_stopped_tab_is_not_counted_as_still_loading(overlay):
    ov, bus, sounds = overlay
    bus.put(TabsChanged((tab(None, "retry_limit", tab_id="t1"), tab("Foil Nimis"))))
    ov._drain_once()
    assert not any("still loading" in line for line in ov._watch_extra)


# --------------------------------------------------------------- userscript

HARNESS = r"""
const SELECTORS = { toastContainer: '.toast-container' };
__CONSTS__
const hellos = [];
const hello = () => hellos.push(liveError);
const console = { warn() {} };
function toast(title, msg) { return { textContent: `${title} ${msg}` }; }
const top = { children: [] };
const document = { querySelectorAll: () => [top, { children: [] }] };
__BODY__
const log = [];
top.children.push(toast('Disconnected from server', 'Retrying connection in 15 seconds...'));
checkLiveError(); log.push(liveError);
const dead = toast('Live Search retry limit exceeded', 'Please refresh the page and try again');
top.children.push(dead);
checkLiveError(); log.push(liveError);
checkLiveError();  // still on screen: no second hello
// the user opens a new search in place; the timeout-0 toast stays up
liveError = null;
checkLiveError(); log.push(liveError);
top.children.push(toast('Live Search error', 'Rate-limiting is active for your account'));
checkLiveError(); log.push(liveError);
process.stdout.write(JSON.stringify({ log, hellos }));
"""


def test_the_script_spots_both_messages_once_each():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    consts = SCRIPT[SCRIPT.index("const LIVE_ERRORS = [") :]
    consts = consts[: consts.index("];") + 2]
    body = SCRIPT[SCRIPT.index("  let liveError = null;") : SCRIPT.index("  /** React the moment")]
    code = HARNESS.replace("__CONSTS__", consts).replace("__BODY__", body)
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=20, check=True)
    result = json.loads(out.stdout)
    assert result["log"] == [None, "retry_limit", None, "rate_limited"]
    assert result["hellos"] == ["retry_limit", "rate_limited"]
