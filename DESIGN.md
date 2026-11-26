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

## Search presets

`sniper.presets` holds ready-made sets of live searches. **High profit** is
Headhunter, Mageblood, Nimis, Progenesis, Sublime Vision and Wine of the
Prophet: any instant-buyout ("securable") foil map with that reward. A click
opens each one's live page,
`/trade/search/<league>/<id>/live`, as a Chrome tab, `TAB_GAP_MS` (1 s)
apart. The userscript then connects each tab like any other. There are two
buttons: step 2 of the first-run guide, and Settings → Live searches.

- Trade search ids are the query itself, gzipped and base64url-encoded, with
  no league in them, so one id works in every league. The league comes from
  poe.ninja's resolution (`PriceStatus.league`), or an explicit `league:` in
  config. While neither is known, the button explains why and opens nothing.
- The ids are the exact ones the trade site issued. Its compressor's output
  differs from zlib's, so rebuilding an id from a reward name gives
  different bytes, and the site's acceptance of those is untested.
  `tests/test_presets.py` decodes each id and checks its query.
- Rewards that a connected tab already watches are skipped. A second click
  within 30 s is ignored, because the first click's tabs have not connected
  yet and would be opened twice.
- The app sends no request of its own: a user click opens pages in the
  user's browser, as typing the URLs would. `preset_open` is logged.

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
  "stash": { "name": "7", "x": 8, "y": 8 },
  "timing": { "push_to_fetch_ms": 12, "fetch_ms": 180, "hidden": true },
  "generic": false
}
```

`generic: true` marks a listing from a live search that is **not** a Valdo
reward search — see "Generic live searches" below. Such a frame always
carries `reward: null`, even if the item happens to have a reward property:
the server registers rewards for trade-API pricing, and a stray one would
spend requests from an already-tight budget on something nobody asked to
price.

The converse is enforced server-side: a frame with `reward: null` is
treated as generic **whatever its `generic` flag says** (`parse_frame`),
and its tab's `search_kind` is promoted to `"any"`. Every Valdo map carries
the Reward property, so no reward means not Valdo. The tab's own flag can
be wrong: after an in-place search edit the page-load query state is stale,
and with fewer than 5 rows on screen the row fallback cannot decide either,
so a real hit used to arrive `generic: false` and sit silently as
`no_reference`.

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

`stash` (or null) is `listing.stash` from the fetch payload: the seller's
tab name and the item's 0-based cell. Network capture only; the DOM row does
not show it. The overlay draws it as a mini grid under the alert's name
(`_set_stash`) so the item can be found in Faustus' shop the moment the
hideout loads. The payload does not say how big the tab is, so the grid is
12×12 unless a coordinate is 12 or more, then 24×24 (a quad tab). A quad
tab with the item in its top-left quarter is therefore drawn as 12×12; the
exact "col, row" beside the grid stays correct either way. Each listing's
stash is logged in `listing_received` so that assumption can be checked.

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
  `seen` dedupes the other. `seen` is FIFO-capped (500), so the sweep's
  "already handled" test is a WeakMap on the row element (`handledRows`),
  not `seen`: on a tab holding more than 500 live rows the cap would
  otherwise evict old keys every sweep and re-send those rows as new
  listings (reproduced: 1,200 rows → 27,400 stale re-sends in 40 s).
- The 2 s sweep reads only rows the observer saw arrive in the last 60 s;
  the whole page is re-read once a minute, and the reward check (which
  also reads every row) runs on those full sweeps once rows have named the
  reward. Reading every row every 2 s made the sweep's cost grow with the
  hours a tab stayed open, in a background tab whose timers Chrome rations.
  `net_stats` reports `rows` (at the last full sweep) and `sweepMsMax`
  (slowest sweep since the last hello) so that cost stays visible.

**Where a listing's delay goes (`timing`).** `index_lag_ms` is GGG's index
time to the listing reaching us. Since 0.8.3 the script runs at
`document-start` and wraps the `WebSocket` constructor, adding one passive
`message` listener to the trade site's live socket (`/api/trade/live/...`;
every other socket, ours included, passes straight through). The rest of
the script still starts at DOMContentLoaded, as it did under
`document-idle`. Each `/api/trade/fetch` call is attributed to the latest
push before it, which splits the delay:

- `index_to_push_ms` (derived server-side) = `index_lag_ms` −
  `push_to_fetch_ms` − `fetch_ms`: GGG's side, plus clock skew and the up
  to 1 s truncation of the second-granular index time;
- `push_to_fetch_ms`: the page reacting to the push. A page bogged down
  by hours of accumulated rows shows up here;
- `fetch_ms`: the details request itself;
- `hidden`: whether the tab was in the background.

All four are logged on `listing_received`. `push_to_fetch_ms` is null when
no push was seen (an older script, or a socket opened before the hook);
`net_stats.liveSockets` says whether the hook found the socket at all.

**Slow tabs (`sniper.tablag`).** A busy tab left open slows down: Mageblood
went from ~1.6 s to 4-6 s over a day and back to ~1.6 s on reload, while
quiet tabs barely moved. Per tab, the server keeps the median delay of the
first 15 network-captured listings after a page load and of the latest 15.
A tab is slow when its recent median is ≥ 2.5 s, ≥ 1.6 × its freshly loaded
median, and ≥ 1 s above the other tabs' (when every tab is slow it is GGG,
and a reload changes nothing). It clears under 2 s or 1.3 ×. The tabs
pill turns amber and an amber bar under the status strip reads
`⟳ RELOAD Mageblood (+2.7s lag)`: the recent median minus the freshly loaded
median, or minus the healthy tabs' median when the page has no fresh
figure (`overlay._reload_notes`). `tab_slow` and
`tab_recovered` are logged. Only a real reload resets a tab: the hello's
`page_id` (one per page load) and `page_age_s` tell it apart from the app
reconnecting, which every app restart does to every tab. A page first seen
already older than 15 minutes has no fresh baseline and is judged against
the other tabs only. Replayed over the logs, the rule flags Mageblood about
4 hours after a reload, at 2.6 s against 1.55 s fresh.

Python -> Userscript:

```json
{ "type": "click_travel", "search_id": "...", "listing_id": "..." }
```

Userscript replies with `{"type": "click_result", "listing_id": "...",
"ok": true|false, "reason": "..."}` so failures (row gone, button missing,
item no longer available) surface in the overlay instead of failing
silently.

Heartbeat: userscript sends `{"type": "hello", "search_id": ..., "tab_id":
..., "search_reward": ..., "search_kind": ..., "live": ..., "version": ...,
"net_stats": {...}, "page_id": ..., "page_age_s": ...}` on connect and every 30s;
the overlay shows a per-tab connection indicator so a dead tab is visible
before it costs a snipe. `search_reward` is taken from the tab's own query
state (`window.tradeOpts`), falling back to the majority reward among
visible rows — it tells the trade pricer what to price before the first
live push arrives. `search_kind` is `"valdo"` or `"any"`; absent means
`"valdo"`, so an older userscript keeps its scored behaviour.

`live` is `false` when the tab is on a plain (non-live) search page — its
URL does not end in `/live`. Those results were never pushed, so the
userscript forwards none of its rows (it still marks them seen, so they
cannot resurface as pushes once live search is activated) and the server
leaves the tab out of the searches and drops any listing it sends
(`listing_ignored`, reason `not_live`). Absent means `true` (scripts older
than 0.8.4). The check runs at send time, because the site switches between
a plain and a live search in place, without a reload.

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
ranking maths. A generic alert holds the top slot above every ordinary
Valdo alert (only a jackpot or super jackpot outranks it): it comes from a
search the user built by hand for one item, so it is the rare one worth
interrupting for.
Generic alerts rank newest first among themselves (they have no efficiency
figure); Valdo alerts keep their natural order below, so a session with no
generic tabs open ranks exactly as it always did.

**Jackpot tier.** A Valdo alert whose net AND yield both clear
`alerts.jackpot_min_net` / `jackpot_min_yield` (`margin.is_jackpot`) is a
jackpot. It outranks every other alert, generic hits included: it clears
both bars, so it is what the hotkey takes.
Generic hits and currency mismatches are never jackpots (the scam guard's
case must not be pushed to the top). The one exception is a chaos price: a
normal way to list, and the unit every price normalizes to, so its net is
trustworthy. It keeps its MISMATCH banner but may be a jackpot. A jackpot
gets a magenta banner, frame and feed row, and plays an alarm once (a super
jackpot its own alarm, once; `alerts.alert_sound` picks a
batch's one sound: super, then jackpot, then generic, then Valdo). The
play count (`sound.JACKPOT_PLAYS` / `SUPER_JACKPOT_PLAYS`, both 1) is baked
into the WAV (`sound.repeat_wav`, cached per volume), so
it is still one async PlaySound that a mute can cut off. Until the alarm has
finished (its WAV's length), a routine alert sound is skipped so it cannot
cut it off. A travel that fails because the listing is gone
(`click_result` reason `row_gone` or `item_no_longer_available`) stops the
alarm early once every jackpot in the batch that set it off is gone
(`jackpot_sound_stopped` is logged); any other failure leaves it ringing,
since the listing may still be there. The banner is not clickable, and the
window is never pulled forward.

A **super jackpot** is a jackpot that also clears
`alerts.super_jackpot_min_net` / `super_jackpot_min_yield`
(`AlertStore.is_super_jackpot`). Requiring the jackpot first keeps every
jackpot exclusion and means super bars set below the jackpot bars cannot
skip a tier. `AlertView.super_jackpot` implies `jackpot`, so code that only
knows jackpots still treats one correctly. The rank key is (generic, super,
jackpot, yield or recency). The overlay styles both tiers through one `_Tier`
palette (gold for super), and a super jackpot plays its own alarm three
times.
None of this touches the click path: one press is still one travel.

## Scam guard

The MISMATCH banner fires when the listing's currency differs from the
currency of the map's reference price (e.g., reference in divine, listed
in exalted — the classic "20 exalted instead of 20 divine" bait). The
overlay shows both currencies prominently. Margin is still computed
correctly via chaos normalization; a mismatch warns, it never blocks.

## Failure risks

`mod_risks` (`config.ModRisk`, matched like a pairing but one pattern is
allowed) model mods that can make the map FAIL rather than be harder:
Bismuth bugging out, Protect the Altar under less damage/delirium, an
unclearable Breach. A failed map pays nothing and the asking price is spent,
so `margin.evaluate` computes an expected value:

    net = (1 - p) x reference - listing price - running_cost
    p   = 1 - prod(1 - fail_chance_i)   (independent risks)

`risk_cost_div = p x reference` is carried on the Decision for display only;
it is already inside `net_div`. Everything downstream (yield, the cutoff,
jackpot, hotkey ranking) reads `net_div`, so none of it needs to know about
risks. A risk at 0 is skipped at compile time. Chances are tuned in the ⚙
panel (stored under `risks:` in scoring_overrides.yaml); patterns stay in
config.yaml. Difficulty multipliers and failure risks must not describe the
same thing twice: the old Ultimatum pairings were removed when the risk
replaced them.

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
  NET column shows:
  the hotkey should take the map that pays best for what it costs to run.
  A clean 25-difficulty map at +20 div (80/100D) outranks a Feared+VOID
  map at +50 div (4/100D). This is the overlay's Yield feed column, and
  the same unit as the threshold — yield >= threshold is the alert
  condition, together with a flat floor `net >= thresholds.min_net`: an
  easy map can reach a high yield on a profit too small to travel for.
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
  `status: securable` (instant buyout only - `trade_pricing.listing_status`;
  `any` let offline sellers' stale prices and negotiable `~price` listings
  undercut the market: Headhunter read ~161d against a real 184d),
  `mutated: false` (Foulborn: No - Foulborn uniques trade at their own
  prices and a Valdo reward is never Foulborn), no foil filter (foil doesn't matter; foils list at
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
  `your price` (amber) on its Watching row - the profit came from a typed
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

Layout: a pinned header (search box + the save/error note, so feedback is
always in view) over a scrolling body ordered by how often each part is
touched: Alerts (yield, running cost, jackpot bars), Hotkey & sound, Reward
prices, Failure risks, Deadly pairings, Base difficulty, Difficulty
multipliers (both rule lists alphabetical). The search filters rows by
label and hover hint; a query matching a section title shows the whole
section; rows are hidden with `grid_remove()` so they return in place.

## Hover help

Every part of the overlay explains itself on hover (`overlay.HELP`, bound
with `Overlay._bind_help`):
- **Instant.** The popup opens the moment the pointer arrives. An earlier
  350 ms delay read as a sluggish UI. It is ONE window, built at startup
  (`_build_help_win`) and only re-filled, moved, shown and withdrawn, so a
  hover never pays for creating a window (~50 ms from `<Enter>` to visible).
  It is bound with `add="+"` so it never displaces a widget's own hover
  handler (the travel button's tint), and any click closes it. Help is
  display-only: it never acts on a listing.
- **One target, many widgets.** A stat cell is a frame, a caption and a
  value. Moving between them neither hides nor re-places the popup.
  `member_of` widens a target to everything inside a container, for rows
  rebuilt every render inside a frame that persists (the price panel). The
  frame is bound once and each new row per render, so handlers never stack.
- **Never over what it explains.** Stat help opens below the whole stats
  row, and price-panel help below the whole table. Popups stay inside the
  overlay's width and flip above when they would leave the screen.
- **Worked examples must match.** NET, YIELD and DIFFICULTY add a live line
  that works the sum through for the listing on screen
  (`_net_breakdown`, `_yield_breakdown`, `_difficulty_breakdown`). Each
  builder returns nothing unless its parts reproduce the figure shown, for
  example when a conversion from another currency is not on screen, or when
  the running cost was changed after the alert was scored.

Reward prices show in the idle card's Watching panel (below) and under a
Valdo alert's name (`Reward worth 101d · price checked 2m ago`; prices
normally come from trade, so only another source - last session, poe.ninja,
a pinned price - is named). There is no separate Watching line at the top of
the overlay any more: it repeated the panel and pushed the alert card down. `RewardPrice.age_s` is the age at push time. The
overlay counts on from when the push arrived, and re-renders both lines every
30 s, so the age keeps ticking between pushes.

**Watching panel.** With no alert on screen, the card drops its four
dashes and the space goes to a table of every searched reward - including
one whose tab just opened and has no price yet ("pricing…") - then a line
for any tab that is not a Valdo reward search (`⚡ 1 other live search`,
`1 tab still loading its search…`). Each row: a trust dot, the reward, a
note (age or why to double-check), the price, and ↻. Dot, note and price
are coloured by how far to trust it (`_price_status`):
- green: a trade price no older than 1.5 × `trade_pricing.refresh_minutes`;
- amber: overdue (older than that, e.g. rate limited), last session's
  price, a poe.ninja estimate, or a pinned price;
- red: no buyable listings;
- ↻: being re-fetched (state from `WarmupStatus`).

Each row's ↻ is a **recheck** button (`App.request_price_recheck`). One click
queues one fetch for that reward: it jumps the scheduled queue, wakes the
price loop's nap (`_price_nap`), and goes through the same `TradePricer`,
so rate-limit headers and the 429 backoff still apply; a request that hits
a backoff is kept and runs after it lifts. While queued the row reads
`recheck queued` (state "queued") and the ↻ is blank; a repeat click while
queued or fetching is ignored.

This lets a price be judged before an alert arrives. The panel replaces the
old warm-up panel, which showed only while pricing was busy. It hides while
an alert is up, because the alert's own reward-value line covers that.
Hovering a history row adds that reward's current price and age, so a
listing can still be judged after its alert has gone.

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
  shows TRAVEL FAILED in a red bar at the very top of the window (plain
  words, e.g. "item already sold"; it clears after 10s, on the next press,
  on a travel that works, or on a click) and drops the traveled pin, and the AlertStore hands
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
- `hotkey.raise_overlay` (⚙ panel toggle, on by default): every press also
  brings the overlay to the front, restoring it if minimized, via
  `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)` / `SW_SHOWNOACTIVATE`
  (`gamewindow.raise_without_focus`) - never activating it, so the game
  keeps the keyboard. The press publishes `HotkeyPressed` on the bus first.
- Hotkey is inert (no-op with a soft warning) if the PoE process is not
  detected.

## Packaging and self-update

**One-file exe.** `tools/build_exe.py` runs PyInstaller (`--onefile
--windowed`) on `tools/exe_entry.py`, a two-line shim over
`sniper.__main__._run`, so the exe and a source checkout run the same code.
`config.yaml` and the browser script are bundled inside; `sniper.paths`
resolves them (`resource()`) from the bundle when frozen and from the repo
otherwise. The build ends with `ValdoSniper.exe --self-check`, which loads
the config, reads the bundled browser script and imports every native
module, exiting non-zero if anything is missing. The window icon is
generated from `sniper/icon.py`'s pixel map, so there is no binary in git.

**Where state lives.** Frozen, per-user files live in
`%LOCALAPPDATA%/ValdoSniper` (`paths.state_dir`), not beside the exe:
people run the exe straight from Downloads. The process `chdir`s there (so
`logs/` and `.env` resolve there) and `Config.state_dir` is that folder:
the user's `config.yaml`, `scoring_overrides.yaml` and `price_cache.json`
are written there and survive updates. Before 1.3.4 they sat beside the
exe; `paths.migrate_state` moves them on first start (never overwriting a
file already in the new folder; an untouched config template is deleted)
and the moved names are logged as `state_dir`. Only the updater's
transient `.new` / `.part` / `.old` stay beside the exe, since its swap is a
rename. Without `LOCALAPPDATA`, or if the folder cannot be created, the
exe's folder is used as before. ⚙ Settings → General → Open shows the folder. `load_config(path, defaults=bundled, state_dir=...)`
layers the user's optional `config.yaml` over the bundled defaults
(`merge_config`: mappings merge key by key, lists are replaced), so a user
file holding one key keeps every other default current as releases change
them. A commented template is written on first start. A source checkout is
unchanged: its `config.yaml` is the whole config and state sits beside it.

**Version.** `sniper.__version__` is the single source (pyproject reads it
dynamically). A release is a `vX.Y.Z` tag; the workflow refuses to build if
the tag disagrees with `__version__`, because an exe that under-reports its
version would update itself in a loop.

**Self-update (`sniper.updater`).** An asyncio task, off the hot path,
20 s after start and then every `updates.check_interval_hours` (1):
`GET /repos/{repo}/releases/latest` with `If-None-Match` (a 304 does not
count against GitHub's 60/hour unauthenticated limit). `Retry-After` and
an exhausted `X-RateLimit-Remaining` + `X-RateLimit-Reset` are honoured;
403/429/5xx and transport errors back off exponentially (5 min doubling,
6 h cap). The User-Agent is `valdo-sniper-updater/<version>`, with no
personal details. Drafts and prereleases are ignored.

When the release is newer and the app is frozen with `auto_download` on,
the `ValdoSniper.exe` asset is streamed to `ValdoSniper.exe.part`, checked
against the asset's `size` and the `sha256:` `digest` GitHub publishes,
and only then renamed to `.new`, with `.new.json` (version, size) written
last. A crash mid-way therefore never leaves anything that counts as
staged. `UpdateStatus` events drive the overlay's update pill: available /
downloading / ready / failed / installed. Every state but installed also
shows a bar at the very top of the window (`overlay._update_banner`; green,
amber for failed), above the TRAVEL FAILED and RELOAD bars
(`_pack_top_bar` keeps that order whichever appears first). Clicking it does
what the pill does; its ✕ hides it until the state or version changes.

**Install.** Never automatic mid-session. Clicking the pill when an update
is `ready` closes the overlay cleanly (hotkey unhooked, loop stopped, logs
flushed), then `apply_staged` renames the running exe to `.old` (Windows
allows renaming a running exe, not deleting it), renames `.new` into place
and starts it with `--wait-pid <old pid>` and `--updated-from <old
version>`. The new process waits for the old one to exit before binding
the websocket port, deletes `.old`, and shows "updated to vX" briefly. The
relaunch sets `PYINSTALLER_RESET_ENVIRONMENT=1` and strips `_PYI*`
variables, or the child would reuse the parent's unpacked (old) bundle. A
staged update not installed by click installs at the next launch, before
anything binds. A staged build that is not newer than the running one
(e.g. the user installed a newer exe by hand) is discarded, never
installed. A source checkout only announces (`available`): git or a ZIP
owns those files.

None of this touches the click path or the hotkey: one press is still one
travel.

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