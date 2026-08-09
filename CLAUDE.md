# Valdo Map Sniper

Tool that watches live searches on the official PoE trade site for Valdo's
Puzzle Box foil maps, add warnings for certain mod combinations, 
computes profit margin against market prices, and lets
the user travel to the seller's hideout with a single hotkey press.

Trade context: PoE 3.27+ asynchronous trading. There are no whispers. The
trade site's "Travel to Hideout" button teleports the logged-in player to the
seller's hideout, where the purchase is completed manually at Faustus.

## Hard constraints (never violate)

- **One user input = one server action.** The travel click fires only in
  direct response to a single global hotkey press by the user. Never
  auto-click "Travel to Hideout" on detection, never queue clicks, never
  retry a click without a new hotkey press. This is a GGG ToS boundary
- Data source for listings is the browser's live search pages (push-based).
  Do not poll the trade search API for sniping.
- Any supplementary HTTP requests (poe.ninja, static data) must parse and
  respect rate-limit headers and back off on 429.
- `POESESSID` or any auth material lives in `.env`, never in code or git.
- The hotkey is disabled unless the PoE game client process is detected
  running.
- The alert overlay must show price **and currency** prominently and flag
  currency mismatches (see Scam guard in DESIGN.md).

## Stack

- Python 3.12: `websockets` (localhost server), `httpx` (poe.ninja),
  `keyboard` (global hotkey), `pygetwindow` + `pywin32` (focus game window),
  `psutil` (game process detection), `tkinter` (always-on-top overlay).
  Target OS: Windows.
- Browser side: Tampermonkey userscript, vanilla JS, no build step.
- Config in `config.yaml`; secrets in `.env`.

## Commands

- Run: `python -m sniper`
- Tests: `pytest`
- Lint/format: `ruff check . && ruff format .`

## Working agreements

- Read `DESIGN.md` before implementing any component; it defines the
  websocket message schemas both sides must follow.
- `TASKS.md` is the build order. Work one milestone at a time; do not start
  the next until acceptance criteria pass.
- Latency matters on the detection -> alert path only. Keep that path free
  of blocking I/O; price cache refreshes happen off the hot path.
- When the DOM structure of the trade site is involved, verify selectors
  against the real page (ask me for a saved HTML snapshot) rather than
  guessing.
- Log every detected listing and every hotkey action to `logs/` with
  timestamps, so missed snipes can be diagnosed.