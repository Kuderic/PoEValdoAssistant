"""Milestone 1 stub server: pretty-prints every frame from the userscript and
offers a stdin REPL to send click_travel manually.

Run:  python tools/echo_server.py
REPL: travel <listing_id>   -> send click_travel to the tab that reported it
      tabs                  -> list connected tabs
      quit
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import datetime

import websockets

HOST, PORT = "127.0.0.1", 8765

tabs: dict[str, websockets.ServerConnection] = {}  # tab_id -> connection
listings: dict[str, dict] = {}  # listing_id -> {"tab_id", "search_id", "frame"}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def handle(ws: websockets.ServerConnection) -> None:
    peer_tab = None
    print(f"[{ts()}] + connection from {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[{ts()}] ! non-JSON frame: {raw[:200]!r}")
                continue
            mtype = msg.get("type")
            if mtype == "hello":
                peer_tab = msg.get("tab_id")
                tabs[peer_tab] = ws
                print(f"[{ts()}] hello  tab={peer_tab} search={msg.get('search_id')}")
            elif mtype == "new_listing":
                lid = msg.get("listing_id")
                listings[lid] = {"tab_id": msg.get("tab_id"), "frame": msg}
                print(f"[{ts()}] new_listing:\n{json.dumps(msg, indent=2, ensure_ascii=False)}")
            elif mtype == "click_result":
                print(
                    f"[{ts()}] click_result listing={msg.get('listing_id')} "
                    f"ok={msg.get('ok')} reason={msg.get('reason')!r}"
                )
            else:
                print(f"[{ts()}] ? unknown frame: {msg}")
    finally:
        if peer_tab and tabs.get(peer_tab) is ws:
            del tabs[peer_tab]
        print(f"[{ts()}] - connection closed ({peer_tab or 'no hello'})")


async def repl() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if not line:
            continue
        cmd, *rest = line.split()
        if cmd == "quit":
            raise SystemExit
        if cmd == "tabs":
            for tab_id in tabs:
                print(f"  tab {tab_id}")
            print(f"  ({len(tabs)} connected, {len(listings)} listings seen)")
        elif cmd == "travel" and rest:
            lid = rest[0]
            info = listings.get(lid)
            ws = tabs.get(info["tab_id"]) if info else None
            if ws is None:
                print(f"  no connected tab for listing {lid!r}")
                continue
            await ws.send(
                json.dumps(
                    {
                        "type": "click_travel",
                        "search_id": info["frame"].get("search_id"),
                        "listing_id": lid,
                    }
                )
            )
            print(f"  click_travel sent for {lid}")
        else:
            print("  commands: travel <listing_id> | tabs | quit")


async def main() -> None:
    async with websockets.serve(handle, HOST, PORT):
        print(f"echo server on ws://{HOST}:{PORT} - open a live search tab now")
        await repl()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
