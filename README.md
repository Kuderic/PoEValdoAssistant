# Valdo Map Sniper

Watches your live searches on the official PoE trade site for Valdo's Puzzle
Box foil maps, works out what each one is actually **worth to you** — profit
against the reward's real market price, divided by how nasty the map is to
run — alerts with sound and an always-on-top overlay, and travels to the
seller's hideout on a single hotkey press. PoE 3.27+ asynchronous trading:
no whispers, you complete the purchase manually at Faustus.

<img src="docs/screenshot.png" alt="Valdo Sniper overlay: a Mageblood map alerting at +107 div profit, with difficulty-scored mods highlighted, a runner-up alert, and the live feed of recent listings" width="440">

**Trade etiquette / ToS stance (non-negotiable, see CLAUDE.md):** one user
input = one server action. The Travel click fires only in direct response to
your hotkey press or overlay click — never on detection, never queued, never
retried. Listings come from the browser's live-search push, never from
polling the trade API.

---

## The profit model — read this first

Everything the app does comes down to one number.

```
P/100D  =  divine profit  ÷  difficulty  × 100
```

**Profit** is the reward's market price minus the asking price, minus a flat
toll (see below). **Difficulty** is a score built from the map's mods. The
ratio is *profit per unit of pain*, and an alert fires when it clears your
threshold:

```
alert  ⟺  P/100D  ≥  thresholds.global_profit_div      (default 2)
```

Why a ratio rather than raw profit: a clean 25-difficulty map at +20 div and
a Feared + VOID nightmare at +50 div are not the same trade. The second one
pays more and is worth less. P/100D says so, and it is what the hotkey ranks
on when several maps are alerting at once.

The threshold is expressed in those same units, so it reads directly: **"a
100-difficulty map must clear this much profit."** A 200-difficulty map needs
twice as much; a 50-difficulty map half.

### Difficulty

Each mod on the map contributes, via `mod_scoring` in `config.yaml`:

- **Base rules** set a floor — `The Feared: min_base 120` means any map with
  The Feared starts at 120 difficulty. The **highest** base wins; they don't
  add up.
- **Multipliers** scale the result — `100% Delirious: ×2.0`. These all
  multiply together.
- **Pairings** multiply on top again, for combinations that are worse than
  the sum of their parts (below).

So `difficulty = max(base_default, highest min_base) × every multiplier ×
every pairing`.

### The flat toll

`thresholds.flat_profit_reduction` (default **1** divine) comes off every
listing's profit before anything else looks at it. Running any map costs you
time, so an easy map with a 0.5 div margin is not actually worth doing. It is
subtracted at the source, so the profit you see on screen, the P/100D, the
alert cutoff and the hotkey's ranking all agree.

### Deadly pairings

Some mod combinations aren't "harder" — they're unrunnable. `mod_pairings`
catches these, multiplies the difficulty hard, and shows a red banner with a
note saying what actually goes wrong:

| Pairing | × | Why |
|---|---|---|
| No damage + fatal timer | 10 | Impossible unless DPS-check build |
| Delirium + fatal timer | 10 | Impossible unless DPS-check build |
| Ultimatum + delirium | 2 | Almost certain to fail Protect the Altar |
| Ultimatum + no damage | 2 | Almost certain to fail Protect the Altar |
| Blight + melee-range only | 2 | Bricks the CC tower strategy |

These stack multiplicatively, so a map with two 10× pairings lands in the
hundreds of thousands of difficulty and will simply never alert — which is
the point. Multipliers are editable in the ⚙ panel; the match patterns live
in `config.yaml`.

---

## Setup

1. **Python side** (Windows, Python 3.12):
   ```
   pip install websockets httpx keyboard pygetwindow pywin32 psutil PyYAML python-dotenv
   ```
   Dev extras: `pip install pytest pytest-asyncio ruff`

2. **Browser side** (Chrome): install
   [Tampermonkey](https://www.tampermonkey.net/), create a new userscript,
   paste in `userscript/valdo-sniper.user.js`. **Re-paste after every update
   and hard-reload your trade tabs** — a tab keeps running the script it
   loaded with. The version each tab is running is logged at `tab_connect`,
   so `grep userscript_version logs/*.jsonl` settles any doubt.

3. **Run**: `python -m sniper` (`--headless` for no overlay, `--config path`
   for an alternate config).

4. Open your Valdo live searches in Chrome tabs. The header should show
   `Tabs: N` in green, `Prices: trade`, `PoE: Running`.

Run PoE in **windowed-fullscreen**, not exclusive fullscreen, or the overlay
can't stay on top. Sound works either way.

---

## Reading an alert

```
Mageblood                                  ⚡ TELEPORT  [ ` ]   latency 2.2s
PRICE DIVINE   PROFIT DIV   │ P/100D │   DIFFICULTY
156            +46          │ +3.8   │   1214
☠ NO DAMAGE + FATAL TIMER  ×10        Impossible unless DPS-check build
Area contains The Feared                                          base 120
```

| Element | What it tells you |
|---|---|
| **PRICE** | What you pay, and in which currency. Turns red on a currency mismatch (the classic "20 exalted, not 20 divine" bait) — a full-width banner also appears |
| **PROFIT DIV** | Market price − asking price − the flat toll |
| **P/100D** | The decision number, boxed. Green means it clears your threshold |
| **DIFFICULTY** | Plain ≤100, amber >100, red >300 |
| **latency** | How long the listing was already live before it reached you — the head start every other sniper had. Green under 2s, red past 5s. Hover it for the full explanation |
| **☠ banners** | Deadly pairings, with the reason |
| **Mod list** | Each mod with its contribution (`base 120`, `×2.5`), red/amber by severity, modifiers right-aligned |

**Use latency to decide whether to bother.** From this app's own logs:
travels clicked within ~2s of detection still lost the item 26% of the time;
4–10s lost 52%. A red latency means someone almost certainly has it already.

Other markers you may see: `⚠️ ESTIMATED PRICE` (priced from poe.ninja's
median, which is inaccurate for foil Valdos), `⏳ CACHED PRICE` (last
session's trade price, refresh pending). Both disappear once a fresh trade
average lands.

**Acting on it:** press the hotkey (default `` ` ``) to travel to the best
alert by P/100D, click the **TELEPORT** button, or click any row — including
a grayed-out one in the history — to travel to that specific listing. All
require PoE to be running; the hotkey is deliberately inert otherwise. After
traveling, the map's mods stay pinned for ~10s so you can read them during
the loading screen, then you buy manually at Faustus.

---

## Tuning it to actually make money

The defaults are a starting point, not an answer. The app logs every listing
and every decision, so tune against your own data rather than guessing.

**Start loose, then tighten.** Set `global_profit_div` low (1–2), run a
session, then look at what you'd have bought:

```
grep '"event": "decision"' logs/sniper-*.jsonl
```

Every entry carries `profit_div`, `difficulty`, `required_profit_div` and the
matched `difficulty_mods`. If maps you would never run are alerting, their
difficulty is scored too low — raise those rules. If you're seeing nothing,
the threshold is too high or your mod rules are too harsh.

**Score difficulty for *your* build, not in the abstract.** This is where the
profit is. A map that's trivial for you and brutal for everyone else is
underpriced by the market and is exactly what you want to be buying. Drop the
multipliers on mods you don't care about and raise the ones that actually
stop you. The ⚙ panel edits every rule live and saves to
`scoring_overrides.yaml`, so you can tune mid-session without a restart.

**Add pairings as you learn them.** Any combination that wastes your time is
worth encoding once, with a note, so you never have to re-derive it at 2am
with a 16-second alert timer running.

**Raise the flat toll if you're churning low-value maps.** At 1 divine you'll
still see thin margins on cheap rewards; at 3–5 you only see trades worth
stopping for.

**Per-reward thresholds** (`thresholds.per_map`) let you demand more from
contested rewards. Nimis and Svalinn in this app's logs sold out from under a
travel click 51% and 40% of the time respectively; Sublime Vision only 4%. If
a reward is a bloodbath, either demand a much better price or don't search
for it.

**Manual price overrides** (`prices`) always win over any fetched price — use
them when you know better than the market average, e.g. right after a patch.

### Config quick reference

| Key | Does |
|---|---|
| `thresholds.global_profit_div` | Minimum P/100D to alert |
| `thresholds.flat_profit_reduction` | Divine toll off every listing |
| `thresholds.per_map` | Per-reward threshold overrides |
| `mod_scoring.base_default` | Difficulty of a clean, unmodded map |
| `mod_scoring.rules` | Per-mod `min_base` / `multiplier`, optional `warning` |
| `mod_pairings` | Deadly combinations: `multiplier` + `note` |
| `mod_warnings` | `block` rules — logged, never alerted, at any price |
| `hotkey.combo` / `.consume` | Travel key; `all` clears every alert per press, `top` keeps runners-up |
| `alerts.expiry_seconds` | How long an alert stays travel-able (16) |
| `alerts.volume` | 0.0–1.0, also in the ⚙ panel |
| `trade_pricing.cache_max_age_minutes` | How stale a restored price may be (720) |

---

## How pricing works

Reward prices come from the **official trade API**: the cheapest unidentified
copies of each reward's unique, outlier-filtered and averaged. poe.ninja's
per-map medians proved inaccurate for foil Valdos and are only a fallback;
poe.ninja is still used for currency rates and to resolve `league: auto`.

- **Startup is not blind.** Last session's prices are restored from
  `price_cache.json`, so the first listing is judged immediately while a
  refresh runs behind it. Anything older than `cache_max_age_minutes` is
  discarded as too stale to trade on.
- **Without a cache**, listings for an unpriced reward are *held back* rather
  than judged against a bad price — the overlay shows `Calculating prices…`
  with a per-reward panel of what's done, in flight, and queued.
- **Rate limits are respected from the API's own headers**, so pacing adapts
  instead of guessing. If you get limited anyway, the header pill turns red
  (`RATE LIMITED 47s`) and unpriced rewards show as `est. prices: 5/8`.
- Prices refresh every `refresh_minutes`; the panel shows `↻` on whichever
  reward is being recalculated, and both the panel and the Searching-line
  tooltip show how long ago each price was calculated.

**Opening many tabs has a cost.** Every live search makes its own detail
fetches against the same rate-limit budget the app uses for pricing. If your
latency figures climb as you add tabs, that's the trade-off — close the
searches you aren't actually racing for.

---

## Maintenance

- **Trade site DOM changed?** All selectors live in the `SELECTORS` block at
  the top of the userscript. Save the results page (Ctrl+S), extract the
  container into `tests/fixtures/results_snapshot.html`, run
  `python tools/build_harness.py`, then open `tests/harness/harness.html`
  (file://) — fix selectors until it's green.
- **Mod patterns**: every listing's full mod list is logged; tune
  `mod_scoring` match strings against real wording from `logs/*.jsonl`.
- **Capture health**: `python tools/capture_report.py` shows which capture
  path is winning (network vs DOM), the index-latency distribution, and the
  userscript's own counters explaining why. Run it if latency stops showing.
- **Latency**: `python tools/latency_report.py` prints p50/p90/p99 for the
  detection → UI hot path (target under 150 ms; typically ~16 ms).
- **Tests**: `pytest` · **Lint**: `ruff check . && ruff format .`
- **Record/replay**: `python tools/record.py` captures real frames;
  `python tools/replay.py [file]` feeds them back through the pipeline.
  `python tools/echo_server.py` is a bare frame viewer for userscript work.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot bind ws://127.0.0.1:8765` | Another sniper/echo server is running — close it |
| `could not register global hotkey` | Run the terminal elevated, or pick another combo |
| Overlay shows `Tabs: 0` | Tab not connected: check Tampermonkey is enabled on the trade page, look for `[valdo]` lines in the tab's DevTools console |
| `Tabs: N (M stale)` | A tab stopped heartbeating (crashed/asleep) — reload it |
| `RATE LIMITED` / `est. prices: N/M` | Trade API refused us; margins fall back to poe.ninja medians and every affected alert is flagged. Close some searches |
| Alert shows no latency | The DOM path captured it, or the tab is on an old userscript. `python tools/capture_report.py` says which |
| Alerts but hotkey does nothing | `PoE: NOT RUNNING` in the header — the hotkey is deliberately inert without the game |
| Travel says `item no longer available` | Someone beat you to it. Check the latency figure on the alerts you're losing |
| Config error on startup naming a rule | Two `mod_scoring` rules share a label; labels must be unique (the ⚙ panel keys on them) |
| Firefox | Not supported — Chrome only (`ws://127.0.0.1` from HTTPS pages) |
