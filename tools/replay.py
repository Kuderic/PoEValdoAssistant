"""Replay a recorded .jsonl frame stream into a running sniper server,
acting like a live-search tab (hello first, then each new_listing).

Run: python tools/replay.py [infile] [--delay 0.05]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import websockets

parser = argparse.ArgumentParser()
parser.add_argument("infile", nargs="?", default="tests/fixtures/frames/sample_stream.jsonl")
parser.add_argument("--delay", type=float, default=0.05, help="seconds between frames")
parser.add_argument("--url", default="ws://127.0.0.1:8765")


async def main() -> None:
    args = parser.parse_args()
    lines = [
        json.loads(line)
        for line in Path(args.infile).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]
    async with websockets.connect(args.url) as ws:
        tab_id = "replay-tab"
        await ws.send(json.dumps({"type": "hello", "search_id": "replay", "tab_id": tab_id}))
        sent = 0
        for msg in lines:
            if msg.get("type") != "new_listing":
                continue
            msg.setdefault("tab_id", tab_id)
            await ws.send(json.dumps(msg))
            sent += 1
            await asyncio.sleep(args.delay)
        print(f"replayed {sent} listings from {args.infile}")
        await asyncio.sleep(0.5)  # let the server finish processing


if __name__ == "__main__":
    asyncio.run(main())
