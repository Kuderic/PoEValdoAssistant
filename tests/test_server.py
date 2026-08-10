import asyncio
import json
import socket
from pathlib import Path

import pytest
import websockets
from conftest import make_config

from sniper.config import ModScoringRule, ModWarningRule
from sniper.modrules import ModRules
from sniper.prices import PriceBook
from sniper.server import SniperServer

STREAM = Path(__file__).parent / "fixtures" / "frames" / "sample_stream.jsonl"

RULES = ModRules(
    [
        ModWarningRule(label="no regen", severity="warn", match="cannot regenerate"),
        ModWarningRule(label="no flasks", severity="block", match="cannot use flasks"),
    ]
)

SCORING_RULES = (
    ModScoringRule(label="The Feared", match="area contains the feared", min_base=100),
    ModScoringRule(label="100% Delirious", match="100% delirious", multiplier=1.8),
    ModScoringRule(label="VOID", match="sent to the void", multiplier=2.0, warning="red"),
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def running_server():
    config = make_config(port=free_port(), scoring_rules=SCORING_RULES)
    decisions = []
    clicks = []
    server = SniperServer(
        config,
        PriceBook(config),
        RULES,
        on_decision=decisions.append,
        on_click_result=clicks.append,
    )
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)  # let it bind
    try:
        yield server, config, decisions, clicks
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_full_stream_end_to_end(running_server):
    server, config, decisions, clicks = running_server
    url = f"ws://127.0.0.1:{config.server.port}"
    frames = [json.loads(line) for line in STREAM.read_text(encoding="utf-8").splitlines()]

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "search_id": "replay", "tab_id": "replay-tab"}))
        for frame in frames:
            await ws.send(json.dumps(frame))
        await ws.send("this is not json")
        await ws.send(json.dumps({"type": "new_listing", "listing_id": "malformed"}))
        await asyncio.sleep(0.3)

        assert [d.listing.listing_id for d in decisions] == [f["listing_id"] for f in frames]
        by_id = {d.listing.listing_id: d for d in decisions}
        assert by_id["case1-alert"].verdict == "alert"
        assert not by_id["case1-alert"].currency_mismatch
        assert by_id["case2-chaos-normalized"].verdict == "alert"
        assert by_id["case2-chaos-normalized"].currency_mismatch
        assert by_id["case2-chaos-normalized"].margin == (36000 - 9000) / 36000
        assert by_id["case3-blocked"].verdict == "blocked"
        # proportional scaling: clean map (diff 25) needs 20 * 0.25 = 5 div,
        # so case4's 10 div profit now alerts
        assert by_id["case4-below"].verdict == "alert"
        assert by_id["case5-noref"].verdict == "no_reference"
        assert by_id["case6-warn-mod"].verdict == "alert"
        assert [h.label for h in by_id["case6-warn-mod"].mod_hits] == ["no regen"]
        # difficulty scaling: Feared+Delirious -> diff 180 -> need 20*1.8 = 36 div
        assert by_id["case7-hard-but-cheap"].verdict == "alert"  # 120 div profit
        assert by_id["case7-hard-but-cheap"].required_profit_div == 36
        assert by_id["case8-hard-too-pricey"].verdict == "below_threshold"  # 25 div
        assert by_id["case9-void-warning"].verdict == "alert"
        assert by_id["case9-void-warning"].special_warnings == (("VOID", "red"),)

        # tab registry knows the connection
        assert [t["tab_id"] for t in server.tab_snapshot()] == ["replay-tab"]

        # click routing: command arrives on this exact connection, result flows back
        ok = await server.send_click_travel("case1-alert")
        assert ok
        cmd = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        assert cmd == {"type": "click_travel", "search_id": "replay", "listing_id": "case1-alert"}
        await ws.send(
            json.dumps(
                {"type": "click_result", "listing_id": "case1-alert", "ok": True, "reason": ""}
            )
        )
        await asyncio.sleep(0.2)
        assert clicks and clicks[0].ok

    await asyncio.sleep(0.2)
    assert server.tab_snapshot() == []  # disconnect cleaned up


async def test_click_travel_unknown_listing(running_server):
    server, config, decisions, clicks = running_server
    assert not await server.send_click_travel("never-seen")


async def test_listing_reward_identifies_its_tab(running_server):
    """A tab that couldn't scrape its reward (empty search page) gets named
    by its first listing."""
    server, config, decisions, clicks = running_server
    url = f"ws://127.0.0.1:{config.server.port}"
    async with websockets.connect(url) as ws:
        # hello with NO search_reward (page had no rows to scrape)
        await ws.send(json.dumps({"type": "hello", "search_id": "hh", "tab_id": "tab-hh"}))
        await asyncio.sleep(0.2)
        assert server.tabs["tab-hh"].search_reward is None
        assert server.active_rewards() == set()

        await ws.send(
            json.dumps(
                {
                    "type": "new_listing",
                    "search_id": "hh",
                    "tab_id": "tab-hh",
                    "listing_id": "hh1",
                    "item_name": "Twisted Sands",
                    "price": {"amount": 30, "currency": "divine"},
                    "seller": "S",
                    "reward": "Foil Headhunter",
                    "mods": [],
                    "row_index": 0,
                    "detected_at": "2026-08-10T12:00:00.000Z",
                }
            )
        )
        await asyncio.sleep(0.3)
        assert server.tabs["tab-hh"].search_reward == "Foil Headhunter"
        assert "Foil Headhunter" in server.active_rewards()
