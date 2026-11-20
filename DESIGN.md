# Design

## Architecture

```
+---------------------------+        ws://127.0.0.1:8765
|  Browser (trade site)     |
|  ~5 live search tabs      |  new_listing -->
|  Tampermonkey userscript  | <-- click_travel      +--------------------+
|  - MutationObserver on    |                       |  Python program    |
|    results container      |                       |  - ws server       |
|  - clicks Travel button   |                       |  - price cache     |
|    for a given listing    |                       |    (poe.ninja)     |
+---------------------------+                       |  - margin engine   |
                                                    |  - overlay + sound |
+---------------------------+    focus window       |  - global hotkey   |
|  PoE game client          | <---------------------|  - game focus      |
|  (must be running)        |                       +--------------------+
+---------------------------+
```

Flow: listing appears on a live search tab -> userscript forwards it ->
margin engine scores it -> if above threshold, overlay + sound fire ->
user presses the hotkey **once** -> program sends `click_travel` for that
listing and focuses the game window -> user completes the purchase at
Faustus manually.

## Userscript install endpoint

The websocket port also answers two plain-HTTP GETs (`server.process_http`,
the `process_request` hook): `/valdo-sniper.user.js` returns the shipped
userscript and `/` a one-link landing page. Websocket upgrades pass straight
through. Tampermonkey intercepts any `*.user.js` URL, so installing is one
click, and the script's `@updateURL`/`@downloadURL` point back at this
endpoint, so Tampermonkey's own update check keeps every browser on the
version the running app expects (`EXPECTED_USERSCRIPT_VERSION`). Read-only,
served on the configured host (127.0.0.1); nothing on this path can act.

## Websocket message schemas

All messages are JSON, one object per frame.

Userscript -> Python:

```json
{
  "type": "new_listing",
  "search_id": "string, from the trade site URL",
  "tab_id": "string, per-tab UUID (sessionStorage) so two tabs on one search differ",
  "listing_id": "string, stable ID or generated hash for the result row",
  "item_name": "string",
  "price": { "amount": 123.0, "currency": "divine" },
  "seller": "string account name",
  "reward": "string or null, e.g. \"Foil Mageblood\" from the Reward property",
  "mods": ["string, raw mod line texts; may be empty"],
  "row_index": 0,
  "detected_at": "ISO-8601 timestamp, userscript clock",
  "indexed_at": "ISO-8601 or null: GGG's own index time (network capture only)",
  "capture": "\"net\" | \"dom\": which path produced this listing",
  "type_line": "string or null: the item's base type (\"Vermillion Ring\")",
  "generic": false
}
```

`generic: true` marks a listing from a live search that is **not** a Valdo
reward search — see "Generic live searches" below. Such a frame always
carries `reward: null`, even if the item happens to have a reward property:
the server registers rewards for trade-API pricing, and a stray one would
spend requests from an already-tight budget on something nobody asked to
price.

`capture` exists because the two paths are silently interchangeable: they
share the `seen` dedup, so whichever fires first wins and the other is a
no-op. Only `net` carries `indexed_at`, so if the DOM path starts winning
the race, index lag quietly disappears from the UI with nothing to point at.
Logging the source makes that visible: `grep capture logs/*.jsonl`.

`indexed_at` comes from `listing.indexed` in the `/api/trade/fetch` payload and
exists only on network-captured listings — the DOM shows nothing better than
"listed 4 minutes ago". `detected_at - indexed_at` is the **index lag**: how
long a listing was already live before it reached us, i.e. the head start every
other sniper had. It is logged per listing as `index_lag_ms` and shown beside
the travel button so a stale alert can be skipped rather than raced. The two
timestamps come from different clocks (GGG's server vs the browser) and
`indexed` is second-granular, so treat it as a multi-second signal, not a
precise measurement; negative values mean the browser clock trails GGG's and
are reported rather than clamped, so skew stays visible.

`currency` is the short trade id from the currency image's `alt` attribute
("divine", "chaos"), falling back to the visible text ("Divine Orb") only
if the image is missing. `reward` exists because a Valdo map's item name is
a random flavor name ("Twisted Sands") — the Reward property is what the
map is worth, so the price cache and thresholds key off `reward` (falling
back to `item_name` when reward is null).

`row_index` is diagnostics only — rows shift, so it is never used for click
targeting. `click_travel` is routed back over the exact connection
(`tab_id`) that reported the listing.

Listing ID: prefer the row's native `data-id` attribute if present;
otherwise `fnv1a32(seller + "|" + item_name + "|" + amount + "|" + currency
+ "|" + mods.join("~"))` as hex. FNV-1a is implemented identically in JS
and Python with shared test vectors. Python treats the ID as opaque; the
userscript maps ID -> DOM row.

### Dedup and re-listings

A seller who re-prices an item keeps the **same** trade listing id, so the
userscript dedups on `id + amount + currency` (`seenKey()`), not on the id
alone — id-only dedup silently swallowed every re-price, including price
*drops*, which are exactly the snipes worth having. Both capture paths
share the key, so DOM and network never double-send the same price.

Consequences on the Python side:

- `AlertStore.insert` treats a changed price for an id with an **active**
  alert as a replacement, so the overlay never shows a stale price for the
  rest of the expiry window; the countdown restarts and the sound replays,
  because it is a new listing state.
- A re-push at an unchanged price is still a duplicate and is ignored.
- A **consumed** listing never re-alerts, whatever its new price — single
  consumption stays a hard guarantee (see the ToS notes in `alerts.py`).
  A listing you already traveled to therefore stays silent after a
  re-price; that is deliberate, not a gap.

### Network capture (primary detection on live pages)

The live search page receives new item ids on its own websocket, fetches
details via `/api/trade/fetch/...?query=<search>`, and only then renders
the row. The userscript wraps `window.fetch` and `XMLHttpRequest` to read
those responses (the page's own traffic — zero additional requests, so the
no-polling constraint is untouched) and forwards the listing before Vue
renders it, saving ~100–500 ms over DOM observation. Verified against a
real response (2026-08-10, sanitized copy in
`tests/fixtures/trade_fetch_response.json`):

- `result[].id` equals the DOM row's `data-id`, so dedup (`seen`) and the
  click path work unchanged; network-captured listings send
  `row_index: -1` (no row yet).
- price is `listing.price.{amount,currency}` (currency already the short
  trade id), seller is `listing.account.name`, reward is
  `item.properties[type==76].values[0][0]`, mods are
  `item.explicitMods[].description` (plain strings also accepted).
- Responses arriving within 4 s of page init / SPA navigation are
  remembered silently (page-load fetches for already-listed rows — mirrors
  the DOM path's silent attach). Non-live search pages are excluded.
- The DOM MutationObserver + retry ladder + 2 s sweep stay on as the
  authority behind the hook; whichever path sees a listing first wins,
  `seen` dedupes the other.

Python -> Userscript:

```json
{ "type": "click_travel", "search_id": "...", "listing_id": "..." }
```

Userscript replies with `{"type": "click_result", "listing_id": "...",
"ok": true|false, "reason": "..."}` so failures (row gone, button missing,
item no longer available) surface in the overlay instead of failing
silently.

Heartbeat: userscript sends `{"type": "hello", "search_id": ..., "tab_id":
..., "search_reward": ..., "search_kind": ...}` on connect and every 30s;
the overlay shows a per-tab connection indicator so a dead tab is visible
before it costs a snipe. `search_reward` is taken from the tab's own query
state (`window.tradeOpts`), falling back to the majority reward among
visible rows — it tells the trade pricer what to price before the first
live push arrives. `search_kind` is `"valdo"` or `"any"`; absent means
`"valdo"`, so an older userscript keeps its scored behaviour.

## Generic live searches

A tab whose query has no `map_completion_reward` filter is watching
something other than a Valdo reward — any item the user built a search for.
Those listings take a bypass lane (`margin._generic_decision`): no
reference lookup, no mod scoring, no threshold, no warm-up hold. They alert
on arrival, because the user's own live search already encodes what they
want and what they will pay, and the only thing the app can usefully add is
speed.

Detection is deliberately asymmetric. A stale or unreadable `tradeOpts`
also looks like "no reward filter", so **unknown means Valdo**: the cost of
guessing wrong that way is a tab keeping its existing behaviour, whereas
the opposite mistake turns a Valdo tab into one that alerts on every
listing regardless of profit.

Generic alerts carry `difficulty = 0`, which makes `net_yield`
return `None` — they can never enter the Yield column or perturb the Valdo
ranking maths. Between the two kinds the top slot goes to whichever
produced the **most recent** alert (recency is the only honest cross-type
comparison when one side has no efficiency figure); within each kind the
natural order still applies, so a session with no generic tabs open ranks
exactly as it always did.

**Jackpot tier.** A Valdo alert whose net AND yield both clear
`alerts.jackpot_min_net` / `jackpot_min_yield` (`margin.is_jackpot`) is a
jackpot. It outranks every non-jackpot, generic hits included, because it
is the alert whose alarm is looping and a reflex press must take the thing
making the noise. Generic hits and currency mismatches are never jackpots
(the scam guard's case must not be made louder). The overlay loops its own
alarm (`SoundWorker` keeps play/stop ordered) until no live alert is a
jackpot, and pulls itself forward with no-activate Win32 calls
(`sniper/attention.py`) so keyboard focus never leaves the game. None of
this touches the click path: one press is still one travel.

## Scam guard

The MISMATCH banner fires when the listing's currency differs from the
currency of the map's reference price (e.g., reference in divine, listed
in exalted — the classic "20 exalted instead of 20 divine" bait). The
overlay shows both currencies prominently. Margin is still computed
correctly via chaos normalization; a mismatch warns, it never blocks.

## Mod warnings and difficulty scoring

The userscript scrapes the map's mod lines into `mods`. Two rule engines
consume them (both: case-insensitive substring by default, `regex: true`
for regex, `match_all` for combos where every pattern must hit):

**`mod_warnings`** — hard rules with severity:

- `warn` — chip on the overlay; the alert still fires.
- `block` — listing downgraded to log-only: no sound, no overlay alert,
  regardless of profit.

**`mod_scoring`** — difficulty scoring that raises the alert cutoff:

- Each rule can set `min_base` (raises the base score floor), `multiplier`
  (scales the final score), and/or `warning: red|yellow` (colored ‼ chip on
  the overlay: VOID red; BISMUTH and BLIGHT yellow).
- `score = max(base_default, min_bases of matched rules) x product of
  matched multipliers`. base_default is 25 (a clean Valdo map is assumed
  meaningfully hard); min-bases take the max, not the sum.
- `required_net_div = thresholds.min_yield (or per_map) x
  score / 100` — the base threshold is calibrated for a 100-difficulty
  map; a 200-difficulty map needs 2x the profit, a 50-difficulty map only
  half. Example: The Feared (base 100) + 100% Delirious (x1.8) = 180
  points -> 1.8x the base threshold.
- Alert ranking and the hotkey's best-pick use **yield** — net divine per
  100 difficulty (`margin.net_yield`) — not raw surplus. "Net" because the
  flat running cost is already subtracted, which is what the overlay's
  NET DIV column shows:
  the hotkey should take the map that pays best for what it costs to run.
  A clean 25-difficulty map at +20 div (80/100D) outranks a Feared+VOID
  map at +50 div (4/100D). This is the overlay's Yield feed column, and
  the same unit as the threshold — yield >= threshold is precisely the
  alert condition.
- Baseline rule values (config.yaml): Feared 100 / Twisted 50 / Einhar 80 /
  invitation bosses 60 / porcupines 20 (inert while base_default is 25);
  multipliers: ghosts 3-4 x2.2, void x2 (+warning), fatal x2, delirium
  x1.8, increasingly lethal x1.7, reflect x1.1; bismuth warning-only.
- Match texts are best-effort guesses at the live mod wording; every
  listing's full mod list lands in `logs/*.jsonl`, so verify and tune the
  patterns against real captures.

## Price cache and margin engine

**Primary reward pricing — official trade API unid averages (added
2026-08-09; poe.ninja's per-map medians proved inaccurate in practice):**

- For every *active* reward (a connected tab's `search_reward`, plus
  rewards seen in listings within the last 30 min), the trade pricer
  searches the **unidentified** version of the unique —
  `POST /api/trade/search/{league}` with `name` (verified: `name` matches
  unid uniques; base-type search is a 1c gamble bin), `identified: false`,
  `status: any`, no foil filter (foil doesn't matter; foils list at
  market parity) — then fetches the cheapest `max_listings` and stores
  the **average** in the PriceBook.
- Cadence: the loop wakes every 15s so a newly-opened live search is
  priced within seconds; each reward re-prices every
  `trade_pricing.refresh_minutes` (30 min default, floor 5). Every active
  reward re-prices each cycle, so the request rate scales with the number
  of open live searches — 10 min earned a 30-minute API backoff on a busy
  session. Requests are paced from the API's own `X-Rate-Limit-*` headers
  (a blind 3s only until the first response teaches us the real budget),
  and 429/Retry-After backs off exponentially. `POESESSID` from .env is
  attached when present.
- ToS stance: these are supplementary PRICING queries like poe.ninja was
  — a handful of requests per refresh cycle — not listing polling for
  snipes (which remains push-only via the live-search tabs).
- Reference precedence: session override (⚙ panel) -> manual `prices:` ->
  trade average (fresh, TTL 2x refresh) -> poe.ninja median (fallback until
  the first trade fetch, or when trade data goes stale). Currency rates
  still come from ninja.

### Session price overrides

A reference price is occasionally just wrong - a thin unid market, an
average that lands badly - and every profit figure downstream inherits the
error. The ⚙ panel's **Reward prices** section pins a corrected price, in
divine, per active reward; blank means automatic. Rules:

- The pin wins over every automatic source and counts as a *primary* price,
  so it also releases a reward the startup warm-up is holding (exactly the
  case of a unique the trade API cannot price at all).
- It applies to listings from then on, like the scoring edits; alerts
  already on the overlay keep the numbers they were judged with.
- The automatic price stays visible beside the field (`· auto 203 divine`)
  and keeps refreshing underneath, so clearing the pin returns to a current
  price rather than a frozen one.
- Anything priced from a pin is flagged `✋ YOUR PRICE` on the alert and
  `your price` in the Watching tooltip - the profit came from a typed
  number, not the market.
- **Never persisted.** Unlike the scoring panel, pins are not written to
  `scoring_overrides.yaml`: a correction that outlived the market that
  justified it would be worse than the error it fixed.


- Source: poe.ninja economy API. **Verified 2026-08-09** (docs at
  poe.ninja/docs/api; the old `/api/data/*` endpoints are dead):
  - `GET /poe1/api/economy/leagues` — first entry is the current challenge
    league (currently "Covens"); `league: auto` in config resolves this
    at startup.
  - `GET /poe1/api/economy/stash/current/item/overview?league=X&type=ValdoMap`
    — foil Valdo maps ARE tracked as a distinct type (~1500 lines). Each
    line's `variant` is the reward name ("Foil Mageblood"), matching our
    `reward` field; several map-name lines share one reward, so the
    reference price is the **median chaosValue per variant** (robust to
    single price-fixed listings).
  - `GET /poe1/api/economy/stash/current/currency/overview?league=X&type=Currency`
    — `chaosEquivalent` per currency name for chaos<->divine normalization.
  - Etiquette (their docs): descriptive User-Agent with contact, ETag
    conditional requests, don't poll faster than a few minutes. Responses
    are CDN-cached ~30 min.
- Manual `prices:` entries in `config.yaml` (keyed by reward) always
  override the API; the API fills everything else.
- Cache refresh every 10 minutes, off the hot path.
- Profit = reference price - listing price, normalized to **divine orbs**
  (chaos <-> divine via poe.ninja rates). Thresholds
  (`thresholds.min_yield`, `thresholds.per_map`) are absolute
  divine amounts, not percentages; listings below threshold are logged but
  do not alert. Margin % is still computed for display. Alert ranking (and
  the hotkey's best-pick) uses profit per 100 difficulty — see the scoring
  section above.
- **What the ValdoMap overview prices (verified 2026-08-09):** each line is
  the market price of the *map itself* (stash-listed, keyed map name +
  reward variant), NOT the price of the foil unique reward. E.g. Foil
  Mageblood maps: median ~203 div vs regular Mageblood unique ~230 div;
  foil uniques have no separate poe.ninja lines. The within-reward spread
  is wide (43-225 div for Foil Mageblood) because bad mods (void on death,
  unmodifiable, etc.) discount a map heavily - the median is a fair mid
  reference, and mod warnings exist precisely to catch the discounted-for-
  a-reason listings.

## Live tuning panel

The overlay's ⚙ button opens a panel editing the base threshold, hotkey
combo, `base_default`, every scoring rule's `min_base`/`multiplier` (match
patterns and warning colors stay in config.yaml), and each active reward's
reference price (session-only — see "Session price overrides"). "Apply"
swaps the scoring engine live (affects listings from then on); "Apply +
Save" also writes `scoring_overrides.yaml` next to config.yaml, which is
merged over `mod_scoring` at startup — so quick tweaks survive restarts
without touching the commented main config. Price pins are the one thing
deliberately left out of that file.

## Click-to-travel and the confirmation dialog

- Every alert row on the overlay (top pick and runners-up) is clickable;
  a click sends `click_travel` for exactly that listing. One user click =
  one server action, same PoE-process gate as the hotkey. Clicking
  consumes only the clicked alert (runners-up stay), while the hotkey
  keeps its configured consume mode and always targets the top pick.
- Hot items (verified from the in-demand-confirmation.html snapshot,
  2026-08-09): there is **no modal** — after the Travel click, the same
  `direct-btn` gains class `expired` and its text becomes
  "In demand. Teleport anyway?". The userscript watches that row's button
  for 3 seconds after its single travel click and clicks it **at most
  once** when the in-demand text appears, reporting
  `click_result {ok: true, reason: "auto_confirmed"}`. This completes the
  same single user-initiated action — never a second travel, never
  retried, inert outside the 3s window.
- Sold items (verified from the progenesis snapshot, 2026-08-09): the row
  swaps its buttons (`.btns` gets `display: none`) for
  `<span class="error">Item no longer available</span>`. The same 3s
  post-click watch reports `click_result {ok: false, reason:
  "item_no_longer_available"}`; if the error span is already present when
  `click_travel` arrives, the click is not attempted at all. The overlay
  shows TRAVEL FAILED and drops the traveled pin, and the AlertStore hands
  back the runners-up that press had cleared (see "Hotkey and focus path"),
  so the next-best alert takes the top slot and is targetable immediately.

## Hotkey and focus path

- `keyboard` registers one global hotkey (default in `config.yaml`).
- An AlertStore keeps the non-expired above-threshold alerts; the hotkey
  always targets the one with the **highest profit per 100 difficulty**
  (a jackpot first, see the Jackpot tier above). The
  overlay shows the top pick large plus up to 2 runners-up. Alerts expire
  after N seconds (config) to avoid traveling to a stale row.
- A press consumes: with `hotkey.consume: all` (default) the whole store is
  cleared, so a second press without a new alert does nothing; with
  `consume: top` only the best is popped and runners-up stay targetable.
  Either way one press = at most one click, and a consumed listing can
  never be clicked again (dedup against a consumed-ID LRU).
- **A travel that fails gives the runners-up back.** The listing that failed
  stays consumed forever (it is the one we just learned is unavailable), but
  the alerts the press cleared as collateral are restored with their
  original expiry, so the next-best immediately becomes the top pick and one
  further press can take it. This matters because the failure usually
  arrives *after* the userscript's optimistic `ok`: it replies the moment it
  clicks, then watches the row for ~3s and reports
  `item_no_longer_available` if the sale lands first. So `ok: true` settles
  nothing in the AlertStore - only a failure, or the next press, releases
  the held collateral. A send that never left (dead tab) settles the same
  way, since no `click_result` is coming for it.
- On press: send `click_travel`, then focus the PoE window via
  `pygetwindow`/`pywin32`. Focus switching is a local OS action and is the
  only thing automated besides the single click bound to the press.
- Hotkey is inert (no-op with a soft warning) if the PoE process is not
  detected.

## Known risks / open questions

- Mixed content: pathofexile.com is HTTPS; Chrome permits `ws://127.0.0.1`
  from secure pages, Firefox may not. Target Chrome first; if Firefox
  support is needed, route through `GM_xmlhttpRequest` or wss with a local
  self-signed cert.
- Trade site DOM is unversioned and can change; selectors live in one
  constants block at the top of the userscript, discovered from a real
  snapshot in Milestone 1.
- The site caps concurrent live searches per account; the tool assumes the
  user manages which searches are open.
- Listing rows may lack a stable ID; if so, hash (seller, item, price,
  position) and accept rare collisions.
- Multiple listings pushed quickly: addressed — the AlertStore ranks by
  divine profit and the hotkey targets the best non-expired alert (see
  "Hotkey and focus path"), so a burst can no longer bury a better listing.