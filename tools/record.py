"""Record incoming userscript frames to a .jsonl file for later replay.

Runs INSTEAD of the sniper (it binds the same port). Every new_listing frame
is appended verbatim, one JSON object per line.

Run: python tools/record.py [outfile]   (default tests/fixtures/frames/recorded.jsonl)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import websockets

HOST, PORT = "127.0.0.1", 8765
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/frames/recorded.jsonl")


async def handle(ws: websockets.ServerConnection) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "new_listing":
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                f.flush()
                print(f"recorded {msg.get('listing_id')} {msg.get('item_name')}")


async def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    async with websockets.serve(handle, HOST, PORT):
        print(f"recording new_listing frames to {OUT} - Ctrl+C to stop")
        await asyncio.Future()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
