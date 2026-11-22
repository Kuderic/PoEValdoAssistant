// ==UserScript==
// @name         Valdo Map Sniper - capture & forward
// @namespace    valdo-sniper
// @version      0.9.0
// @description  Forwards live-search listings to the local sniper program; clicks Travel to Hideout on command (one command, one click).
// @match        https://www.pathofexile.com/trade*
// @grant        none
// @sandbox      raw
// @run-at       document-start
// @updateURL    http://127.0.0.1:8765/valdo-sniper.user.js
// @downloadURL  http://127.0.0.1:8765/valdo-sniper.user.js
// ==/UserScript==

'use strict';

// ---------------------------------------------------------------------------
// SELECTORS - every DOM assumption lives in this block and nowhere else.
// Derived from a real Ctrl+S snapshot of a Valdo live search
// (chrome-html-examples/, 2026-08-09; trimmed copy in
// tests/fixtures/results_snapshot.html). Notes:
// - The results container is `div.results` inside #vue3-portal; the site
//   appends a fresh `div.resultset` per update, hence subtree observation.
//   (A second class="results" exists only inside an x-template script - not
//   real DOM, so document.querySelector is safe.)
// - Rows are `div.row[data-id]`; delisted rows get class "gone" (196 of 353
//   in the snapshot) and must be excluded from capture.
// - Mod text lives in span[data-field^="stat."]; the bracketed value
//   annotation ("[3 to 4]") is a sibling span and stays excluded.
// - The Reward property (span[type="76"]) is what prices a Valdo map; the
//   item name is a random flavor name.
// - Better Trading extension artifacts (bt-*) exist in the DOM; nothing
//   below matches them.
// - One page-structure assumption is deliberately NOT a selector: the
//   search's own query state, read from `window.tradeOpts` in
//   filterReward(). See that function for why and for the snapshot it was
//   verified against.
// ---------------------------------------------------------------------------
const SELECTORS = {
  resultsContainer: '.results',
  row: '.row[data-id]:not(.gone)',
  /* looser row match for the safety sweep: data-id can arrive late */
  rowLoose: '.row:not(.gone):not(.row-total)',
  rowIdAttr: 'data-id',
  itemName: '.middle .item-popup__header-line',
  priceAmount: '.details [data-field="price"] > span:nth-of-type(2)',
  priceCurrencyImg: '.details [data-field="price"] .currency-text img', // alt="divine"
  priceCurrencyText: '.details [data-field="price"] .currency-text span', // "Divine Orb" fallback
  seller: '.details .info .profile-link a',
  modLine: '.item-mod span[data-field^="stat."]',
  rewardValue: '.item-property span[type="76"] > span:last-of-type',
  travelButton: 'button.direct-btn',
  /* sold/delisted rows render `<span class="error">Item no longer
     available</span>` and hide the .btns group (verified in the
     progenesis snapshot, 2026-08-09) */
  rowError: 'span.error',
};

// Hot-item confirmation (verified from in-demand-confirmation.html snapshot,
// 2026-08-09): there is NO modal - the Travel button itself turns into
// "In demand. Teleport anyway?" (class gains "expired") and must be clicked
// a second time to complete the same travel action.
const CONFIRM_TEXT = /in demand|teleport anyway/i;
const UNAVAILABLE_TEXT = /no longer available/i;
const CONFIRM_WINDOW_MS = 3_000;

/* Sent in every hello and logged by the server: a tab keeps running the
   script it loaded with until it is reloaded, so this is the only way to
   tell from logs/ whether a live tab is on a stale userscript.
   KEEP IN SYNC with the @version header above. */
const SCRIPT_VERSION = '0.9.0';

const WS_URL = 'ws://127.0.0.1:8765';
const HEARTBEAT_MS = 30_000;
const SENTINEL_MS = 2_000;
/* network capture: listings taken from /api/trade/fetch responses within
   this window after page init / navigation are page-load fetches for rows
   that were already listed - remembered silently, never alerted (mirrors
   the DOM path's silent attach) */
const NETWORK_SILENT_MS = 4_000;
const TRADE_FETCH_RE = /\/api\/trade\/fetch\//;
/* the trade page's own live-search socket (PoE1 /trade/live/, PoE2
   /trade2/live/poe2/); frames announcing new listings are {"new": [ids]} */
const LIVE_WS_RE = /\/api\/trade2?\/live\//;
const PUSHED_CAP = 500;
/* rows whose price/seller render a tick after insertion retry on this
   schedule (first retry is a rAF) instead of waiting for the 2s sweep */
const RETRY_DELAYS_MS = [50, 100, 200, 400];
const SEEN_CAP = 500; // holds seenKey()s, so re-prices cost an extra slot each
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const PENDING_CAP = 50; // listings queued during ws reconnects
const PENDING_MAX_AGE_MS = 60_000;
/* Rows needed before their lack of a Reward property is treated as proof
   that a search is not a Valdo one (see isGenericSearch). Enough that a
   half-rendered page cannot momentarily look generic. */
const GENERIC_ROW_EVIDENCE = 5;

// ---------------------------------------------------------------------------
// Live-socket push hook. Installed at document-start (before the page opens
// its socket), separately from main(), which still waits for the DOM.
// ---------------------------------------------------------------------------

/* The browser's own WebSocket, captured BEFORE the patch below: with
   @grant none the page and this script share one window, so our own
   ws://127.0.0.1 connection must not go through the hook. */
const NativeWebSocket = window.WebSocket;

/* listing id -> performance.now() when the page's live socket announced it.
   Real evidence that a listing is a push (not a page-load seed), and the
   start of the push -> forward timing. One clock (the tab's), so the delta
   is exact, unlike detected_at vs indexed_at. */
const pushedAt = new Map();
let pushFrames = 0;

function notePushFrame(data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch {
    return; // not JSON: not ours to judge
  }
  if (!msg || !Array.isArray(msg.new)) return; // auth acks etc.
  const now = performance.now();
  pushFrames += 1;
  for (const id of msg.new) {
    if (typeof id !== 'string') continue;
    pushedAt.delete(id); // re-announced ids move to the back of the FIFO
    pushedAt.set(id, now);
    if (pushedAt.size > PUSHED_CAP) pushedAt.delete(pushedAt.keys().next().value);
  }
}

/** ms since the live socket announced `id`, or null if it never did. */
function msSincePush(id) {
  const t = pushedAt.get(id);
  return t === undefined ? null : Math.round(performance.now() - t);
}

function hookLiveSocket() {
  if (!NativeWebSocket || window.__VALDO_TEST__) return;
  // A Proxy with only a construct trap: instanceof, WebSocket.OPEN and the
  // prototype all forward to the native class untouched, and the instance
  // returned is a real native WebSocket.
  window.WebSocket = new Proxy(NativeWebSocket, {
    construct(target, args, newTarget) {
      const sock = Reflect.construct(target, args, newTarget);
      try {
        if (LIVE_WS_RE.test(String(args[0]))) {
          // registered before the page sets onmessage, so it runs first;
          // a tiny handler that can never throw into the page
          sock.addEventListener('message', (ev) => {
            try {
              notePushFrame(ev.data);
            } catch { /* never break the page */ }
          });
        }
      } catch { /* never break the page */ }
      return sock;
    },
  });
}

// ---------------------------------------------------------------------------
// Pure functions (no DOM mutation, no network) - exercised by the harness.
// ---------------------------------------------------------------------------

/** FNV-1a 32-bit over UTF-8 bytes, hex string. Must match sniper/models.py. */
function fnv1a32(str) {
  const bytes = new TextEncoder().encode(str);
  let h = 0x811c9dc5;
  for (const b of bytes) {
    h ^= b;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

function textOf(root, selector) {
  const el = root.querySelector(selector);
  return el ? el.textContent.trim() : '';
}

function extractMods(rowEl) {
  return Array.from(rowEl.querySelectorAll(SELECTORS.modLine))
    .map((el) => el.textContent.trim())
    .filter((t) => t.length > 0);
}

/**
 * Price fields only - the cheap half of parseRow. Split out so the 2s sweep
 * can skip unchanged rows with two querySelectors instead of a full parse
 * (dedup is price-aware, so the old id-only fast path no longer suffices).
 * Returns null when the price has not rendered yet.
 */
function parsePrice(rowEl) {
  const amountText = textOf(rowEl, SELECTORS.priceAmount).replace(/[,×x]/g, '');
  // Prefer the currency image's alt ("divine" - the short trade id used by
  // config and poe.ninja); fall back to the visible text ("Divine Orb").
  const currencyImg = rowEl.querySelector(SELECTORS.priceCurrencyImg);
  const currency = (currencyImg && currencyImg.getAttribute('alt')?.trim())
    || textOf(rowEl, SELECTORS.priceCurrencyText);
  const amount = parseFloat(amountText);
  if (!currency || !Number.isFinite(amount)) return null;
  return { amount, currency };
}

/**
 * Dedup key. A re-listed item KEEPS its trade id but gets a new price, so
 * keying on the id alone silently swallowed every re-price (a seller cutting
 * 190d -> 155d would never have been forwarded). Price is part of the key:
 * one alert per distinct price, re-pushes at an unchanged price stay quiet.
 * Both capture paths share this key, so DOM and network never double-send.
 */
function seenKey(id, price) {
  return `${id}|${price.amount}|${String(price.currency).toLowerCase()}`;
}

/**
 * Parse one result row into the new_listing payload fields.
 * Returns null when mandatory fields are missing (row not fully rendered yet).
 */
function parseRow(rowEl) {
  const itemName = textOf(rowEl, SELECTORS.itemName);
  const price = parsePrice(rowEl);
  const seller = textOf(rowEl, SELECTORS.seller);
  if (!itemName || !price || !seller) return null;
  return {
    item_name: itemName,
    price,
    seller,
    reward: textOf(rowEl, SELECTORS.rewardValue) || null,
    mods: extractMods(rowEl),
  };
}

/** Native data-id when present, else deterministic hash. Matches DESIGN.md. */
function listingId(rowEl, parsed) {
  const native = rowEl.getAttribute(SELECTORS.rowIdAttr);
  if (native) return native;
  const p = parsed || parseRow(rowEl);
  if (!p) return null;
  const key = [p.seller, p.item_name, p.price.amount, p.price.currency, p.mods.join('~')].join('|');
  return 'h' + fnv1a32(key);
}

/**
 * Parse one /api/trade/fetch result entry into the new_listing payload
 * fields (same shape as parseRow) plus the listing id. Field paths verified
 * against a real fetch response (2026-08-10; sanitized copy in
 * tests/fixtures/trade_fetch_response.json):
 * - entry.id is the same 64-hex id the DOM row carries as data-id, so the
 *   click path resolves network-captured listings unchanged;
 * - the Reward is properties[type==76].values[0][0];
 * - explicitMods entries are {description} objects on the live site (plain
 *   strings on the public API - both accepted);
 * - listing.indexed is GGG's own index time. Forwarding it is the only way
 *   to measure how long a listing was already live before we saw it, which
 *   is the window in which somebody else can take it. The DOM path cannot
 *   supply this (the row only says "listed 4 minutes ago").
 * Returns null when mandatory fields are missing.
 */
function parseFetchItem(entry) {
  const id = entry?.id;
  const listing = entry?.listing;
  const item = entry?.item;
  if (typeof id !== 'string' || !id || !listing || !item) return null;
  const amount = Number(listing.price?.amount);
  const currency = typeof listing.price?.currency === 'string' ? listing.price.currency : '';
  const seller = typeof listing.account?.name === 'string' ? listing.account.name : '';
  const itemName = typeof item.name === 'string' ? item.name : '';
  if (!itemName || !currency || !seller || !Number.isFinite(amount)) return null;
  let reward = null;
  for (const prop of item.properties || []) {
    if (prop && prop.type === 76) {
      reward = prop.values?.[0]?.[0] ?? null;
      break;
    }
  }
  const mods = (item.explicitMods || [])
    .map((m) => (typeof m === 'string' ? m : m?.description || ''))
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
  return {
    id,
    item_name: itemName,
    // the base type; for a rare "Vermillion Ring" is the half worth reading
    // while item_name is the random rare name. Valdo maps ignore it.
    type_line: typeof item.typeLine === 'string' ? item.typeLine : null,
    price: { amount, currency },
    seller,
    reward: typeof reward === 'string' && reward ? reward : null,
    mods,
    indexed: typeof listing.indexed === 'string' ? listing.indexed : null,
  };
}

/** Bounded set with FIFO eviction; holds seenKey()s (id + price), the dedup
 *  authority for forwarded rows across both capture paths. */
class SeenSet {
  constructor(cap) {
    this.cap = cap;
    this.set = new Set();
    this.queue = [];
  }
  has(id) {
    return this.set.has(id);
  }
  add(id) {
    if (this.set.has(id)) return;
    this.set.add(id);
    this.queue.push(id);
    if (this.queue.length > this.cap) this.set.delete(this.queue.shift());
  }
}

// ---------------------------------------------------------------------------
// Runtime wiring - skipped entirely under the test harness.
// ---------------------------------------------------------------------------

function main() {
  const tabId = (() => {
    let id = sessionStorage.getItem('valdo_tab_id');
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem('valdo_tab_id', id);
    }
    return id;
  })();

  let searchId = location.pathname.split('/').filter(Boolean).pop() || 'unknown';
  const seen = new SeenSet(SEEN_CAP);
  const rowRefs = new Map(); // listing_id -> WeakRef<Element>
  const clickedIds = new Set(); // one command = one click, forever per id

  // --- websocket -----------------------------------------------------------
  let ws = null;
  let reconnectDelay = RECONNECT_MIN_MS;
  const pendingListings = []; // queued while disconnected: {frame, queuedAt}

  function send(obj) {
    if (ws && ws.readyState === NativeWebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return;
    }
    // never lose listings to a reconnect blip: queue and flush on open
    if (obj.type === 'new_listing') {
      pendingListings.push({ frame: obj, queuedAt: Date.now() });
      if (pendingListings.length > PENDING_CAP) pendingListings.shift();
    }
  }

  function flushPending() {
    const cutoff = Date.now() - PENDING_MAX_AGE_MS;
    let sent = 0;
    while (pendingListings.length) {
      const { frame, queuedAt } = pendingListings.shift();
      if (queuedAt < cutoff) continue; // too old to be actionable
      ws.send(JSON.stringify(frame));
      sent += 1;
    }
    if (sent) console.info(`[valdo] flushed ${sent} listings queued while disconnected`);
  }

  /**
   * The reward this tab's search targets, taken as the majority reward among
   * currently visible rows (a live search shows recent results immediately,
   * so this resolves at page load). Lets the Python side price the reward
   * via the trade API before the first live push arrives.
   */
  function majorityReward() {
    if (!observedContainer) return null;
    const counts = new Map();
    for (const row of observedContainer.querySelectorAll(SELECTORS.row)) {
      const reward = textOf(row, SELECTORS.rewardValue);
      if (reward) counts.set(reward, (counts.get(reward) || 0) + 1);
    }
    let best = null;
    let n = 0;
    for (const [reward, count] of counts) if (count > n) { best = reward; n = count; }
    return best;
  }

  /**
   * The reward this tab's search targets, read from the page's own query
   * state instead of from rendered rows.
   *
   * The trade site embeds the search it loaded with in `window.tradeOpts`
   * (verified against the progenesis Ctrl+S snapshot, 2026-08-09):
   *
   *   window.tradeOpts = {..., "state": {"filters": {"map_filters":
   *     {"filters": {"map_completion_reward": {"option": "Progenesis"}}}}},
   *     "live": "d86aQa3oUJ"}
   *
   * This is what retires "+N unidentified" in the overlay: a live search
   * that has not pushed a result yet has no rows to take a majority of, but
   * it always knows what it is filtering for, so the Python side can price
   * the reward before the first listing arrives rather than after.
   * `@grant none` runs us in page context, so this is a plain read.
   *
   * `state` is a page-LOAD bootstrap. Editing filters in place mints a new
   * live search and changes the URL without reloading, which would leave
   * this describing the PREVIOUS search - so require `tradeOpts.live`, the
   * id of the search that state belongs to, to still be in the path. That
   * makes a stale bootstrap fall back to the rows on its own.
   */
  function queryState() {
    const opts = window.tradeOpts;
    const live = opts && opts.live;
    if (typeof live !== 'string' || !live || !location.pathname.includes(live)) return null;
    return (opts.state && typeof opts.state === 'object') ? opts.state : null;
  }

  /**
   * True when this tab is watching something OTHER than a Valdo reward -
   * any live search the user built for an item they want.
   *
   * Detected by the ABSENCE of the map-reward filter, but only when the
   * query state was positively readable: a stale or missing bootstrap also
   * yields "no reward filter", and treating that as generic would turn a
   * Valdo tab into an alert-on-everything tab. Unknown therefore means
   * Valdo, the conservative direction - the worst case is the tab keeps its
   * existing scored behaviour instead of gaining a new one.
   */
  function isGenericSearch() {
    const state = queryState();
    if (state) {
      const filters = state.filters && state.filters.map_filters;
      const reward = filters && filters.filters && filters.filters.map_completion_reward;
      return !(reward && typeof reward.option === 'string' && reward.option);
    }
    return rowsLookGeneric();
  }

  /**
   * Fallback when the bootstrap is unreadable: ask the rows instead.
   *
   * queryState() deliberately returns null once `tradeOpts.live` stops
   * matching the path - which happens on any in-place SPA navigation, since
   * `state` is a page-LOAD snapshot. That guard is right for reward
   * detection, but as the ONLY generic test it silently demoted a working
   * generic tab back to the Valdo lane: its listings then arrive
   * generic:false, get judged against a reward price that does not exist,
   * and land as no_reference - visible in the feed, never alerting, no
   * sound. Observed live: 18 Pearl of Tsoatha listings, 18 no_reference,
   * 0 flagged generic, on a tab already running 0.7.6.
   *
   * Every Valdo row carries the Reward property - it is how the map is
   * priced at all - so a screenful of rows with NO reward among them cannot
   * be a Valdo search. Requiring several rows keeps a half-rendered page
   * from momentarily looking generic, and rows are only consulted when the
   * authoritative query state is unavailable.
   */
  function rowsLookGeneric() {
    if (!observedContainer) return false;
    const rows = observedContainer.querySelectorAll(SELECTORS.row);
    if (rows.length < GENERIC_ROW_EVIDENCE) return false;
    for (const row of rows) {
      if (textOf(row, SELECTORS.rewardValue)) return false;
    }
    return true;
  }

  function filterReward() {
    const state = queryState();
    if (!state) return null;
    const filters = state.filters && state.filters.map_filters;
    const reward = filters && filters.filters && filters.filters.map_completion_reward;
    const option = reward && reward.option;
    if (typeof option !== 'string' || !option) return null;
    // Rows render this as "Foil Progenesis" while the filter stores the bare
    // unique name. Both paths feed the same reward key on the Python side
    // (which would otherwise price the two spellings as two rewards), and
    // this tool only ever watches foil boxes, so the prefix is unconditional.
    return option.startsWith('Foil ') ? option : `Foil ${option}`;
  }

  /**
   * Rows win when there are any: they carry the exact string the capture
   * path will send, so a search with results never depends on the filter's
   * spelling. The filter answers for a search that has not rendered a row.
   */
  function searchReward() {
    return majorityReward() || filterReward();
  }

  let lastSentReward = null;

  function hello() {
    lastSentReward = searchReward();
    send({
      type: 'hello',
      search_id: searchId,
      tab_id: tabId,
      search_reward: lastSentReward,
      search_kind: isGenericSearch() ? 'any' : 'valdo',
      version: SCRIPT_VERSION,
      net_stats: { ...netStats, pushFrames },
    });
  }

  /**
   * Re-hello immediately once the tab's reward becomes knowable (rows may
   * render after the first hello), instead of waiting for the heartbeat.
   * Also corrects a filter-derived name once rows confirm the real spelling.
   */
  function refreshRewardIdentity() {
    const reward = searchReward();
    if (reward && reward !== lastSentReward) hello();
  }

  function connect() {
    ws = new NativeWebSocket(WS_URL);
    ws.addEventListener('open', () => {
      reconnectDelay = RECONNECT_MIN_MS;
      console.info('[valdo] connected');
      hello();
      flushPending();
    });
    ws.addEventListener('message', (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === 'click_travel') handleClickTravel(msg);
    });
    ws.addEventListener('close', () => {
      const jitter = Math.random() * 0.3 + 0.85;
      const delay = Math.round(reconnectDelay * jitter);
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      console.info(`[valdo] disconnected, retrying in ${delay}ms`);
      setTimeout(connect, delay);
    });
    ws.addEventListener('error', () => ws.close());
  }

  setInterval(hello, HEARTBEAT_MS);

  // --- capture path --------------------------------------------------------

  /**
   * Returns true when the row was fully processed (seen or sent). False =
   * the row is not parseable yet (price/seller still rendering); it stays
   * unseen so the 2s sweep retries it.
   */
  function handleRow(rowEl, silent) {
    // cheap price-only check first so sweeps skip unchanged rows without a
    // full parse; a re-priced row falls through and is forwarded again
    const native = rowEl.getAttribute(SELECTORS.rowIdAttr);
    if (native) {
      const price = parsePrice(rowEl);
      if (price && seen.has(seenKey(native, price))) return true;
    }
    const parsed = parseRow(rowEl);
    if (!parsed) return false;
    const id = listingId(rowEl, parsed);
    if (!id) return false;
    const key = seenKey(id, parsed.price);
    if (seen.has(key)) return true;
    seen.add(key);
    rowRefs.set(id, new WeakRef(rowEl));
    if (silent) return true;
    const rows = Array.from(rowEl.parentElement?.children ?? []);
    const generic = isGenericSearch();
    send({
      type: 'new_listing',
      search_id: searchId,
      tab_id: tabId,
      listing_id: id,
      ...parsed,
      // A generic tab's listings must never carry a reward: the server
      // registers rewards for trade-API pricing, and a stray one would put
      // an unwanted name into that already-tight request budget.
      reward: generic ? null : parsed.reward,
      generic,
      capture: 'dom',
      row_index: rows.indexOf(rowEl),
      push_to_send_ms: msSincePush(id),
      detected_at: new Date().toISOString(),
    });
    return true;
  }

  function collectRows(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return [];
    const out = node.matches(SELECTORS.rowLoose) ? [node] : [];
    out.push(...node.querySelectorAll(SELECTORS.rowLoose));
    return out;
  }

  /**
   * handleRow with a fast retry ladder: a row that is not parseable yet
   * (price/seller still rendering) is re-tried on a zero timeout then short
   * timeouts (NOT requestAnimationFrame: rAF never fires in a hidden tab,
   * and most live-search tabs are hidden, so the ladder never started), so it forwards within ~50ms instead of waiting up to 2s for
   * the safety sweep (which remains the net for rows that outlast the
   * ladder). `silent` is sticky per attempt chain.
   */
  const retrying = new WeakSet();

  function processRow(rowEl, silent) {
    if (handleRow(rowEl, silent) || retrying.has(rowEl)) return;
    retrying.add(rowEl);
    let attempt = 0;
    const retry = () => {
      if (handleRow(rowEl, silent) || !rowEl.isConnected || attempt >= RETRY_DELAYS_MS.length) {
        retrying.delete(rowEl);
        return;
      }
      setTimeout(retry, RETRY_DELAYS_MS[attempt]);
      attempt += 1;
    };
    setTimeout(retry, 0);
  }

  let observedContainer = null;
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        for (const row of collectRows(node)) processRow(row, false);
      }
    }
  });

  function attach(silent) {
    const container = document.querySelector(SELECTORS.resultsContainer);
    if (!container) return false;
    observedContainer = container;
    // Rows already present at silent init were not pushed live in front of
    // us - never alert on them, only remember them (the retry ladder keeps
    // slow-rendering ones silent too, so they can't leak into the sweep as
    // fake live pushes).
    for (const row of container.querySelectorAll(SELECTORS.rowLoose)) processRow(row, silent);
    observer.observe(container, { childList: true, subtree: true });
    console.info(`[valdo] observing results container (silent init: ${silent})`);
    return true;
  }

  // --- network capture -----------------------------------------------------
  // The live page receives new item ids on its own websocket, then fetches
  // details via /api/trade/fetch and only THEN renders the row. Reading
  // those responses (zero extra requests - purely the page's own traffic,
  // so the no-polling constraint is untouched) forwards a listing
  // ~100-500ms before the DOM row exists. The DOM path stays on as the
  // authority for anything the hook misses; `seen` dedupes between the two.
  // The click path is unaffected: entry.id === the row's data-id.

  let networkSilentUntil = Date.now() + NETWORK_SILENT_MS;
  // main() now runs at DOMContentLoaded (0.9.0), earlier than the old
  // document-idle: restart the page-load window at `load` so slow seed
  // fetches still land inside it
  if (document.readyState !== 'complete') {
    window.addEventListener(
      'load',
      () => { networkSilentUntil = Date.now() + NETWORK_SILENT_MS; },
      { once: true },
    );
  }

  /* Why the network path did or did not forward a listing, reported in
     every hello and logged server-side. The two capture paths share the
     `seen` dedup, so a silent regression in either is otherwise invisible:
     "dedup" dominating means the DOM path is winning the race, "silent"
     means the page-load window ate it, "unparsed" means the payload shape
     moved. */
  const netStats = {
    payloads: 0, entries: 0, sent: 0, dedup: 0, silent: 0, unparsed: 0,
    // which wrapper delivered the payload: tells apart "the page uses fetch
    // and our extra clone().json() hop costs us the race" from "the page
    // uses XHR and its handler runs before ours"
    viaFetch: 0, viaXhr: 0,
    // of the deduped entries, how many already had a rendered DOM row when
    // we looked. High => the page really did render before our handler ran
    // (an ordering problem). Low => the key reached `seen` some other way
    // (silent init or the sweep), which is a different bug entirely.
    domAlready: 0,
  };

  function handleFetchEntry(entry) {
    netStats.entries += 1;
    const parsed = parseFetchItem(entry);
    if (!parsed) {
      netStats.unparsed += 1;
      return;
    }
    const key = seenKey(parsed.id, parsed.price);
    if (seen.has(key)) {
      netStats.dedup += 1;
      const sel = `.row[${SELECTORS.rowIdAttr}="${parsed.id}"]`;
      if (observedContainer && observedContainer.querySelector(sel)) netStats.domAlready += 1;
      return;
    }
    seen.add(key);
    const pushMs = msSincePush(parsed.id);
    // An id the live socket announced is a push, whatever the clock says:
    // that is evidence, the silent window only a heuristic. The window
    // still covers ids we never saw announced (socket opened before our
    // hook, or page-load seeds), exactly as before.
    if (pushMs === null && Date.now() < networkSilentUntil) {
      netStats.silent += 1;
      return; // page-load fetch, not a push
    }
    netStats.sent += 1;
    const { id, indexed, ...fields } = parsed;
    const generic = isGenericSearch();
    send({
      type: 'new_listing',
      search_id: searchId,
      tab_id: tabId,
      listing_id: id,
      ...fields,
      reward: generic ? null : fields.reward, // see the DOM path for why
      generic,
      indexed_at: indexed, // GGG's index time; null on the DOM path
      capture: 'net',
      row_index: -1, // no DOM row yet
      push_to_send_ms: pushMs, // live-socket push -> forwarded, one clock
      detected_at: new Date().toISOString(),
    });
  }

  function handleFetchPayload(url, data, via) {
    if (via === 'fetch') netStats.viaFetch += 1;
    else if (via === 'xhr') netStats.viaXhr += 1;
    // live search pages only: fetches on static search pages come from the
    // user browsing, not from pushes - leave those to the DOM path
    if (!location.pathname.endsWith('/live')) return;
    try {
      // the ?query= param is the real search id (the URL path ends in
      // "live", which is what the path-derived searchId degrades to)
      const q = new URL(url, location.origin).searchParams.get('query');
      if (q && q !== searchId) searchId = q;
    } catch { /* malformed url: keep the current searchId */ }
    if (!data || !Array.isArray(data.result)) return;
    netStats.payloads += 1;
    for (const entry of data.result) {
      try {
        handleFetchEntry(entry);
      } catch { /* one bad entry must not drop the rest */ }
    }
  }

  /** Wrap fetch + XHR; capture must never affect the page's own requests. */
  function hookNetwork() {
    const origFetch = window.fetch;
    if (origFetch) {
      window.fetch = function (input, init) {
        const out = origFetch.call(this, input, init);
        let url = '';
        try {
          url = typeof input === 'string' ? input : input?.url || '';
        } catch { /* exotic input: leave it to the DOM path */ }
        if (!url || !TRADE_FETCH_RE.test(url)) return out;

        // Parse BEFORE the page gets the response.
        //
        // Chaining off the response and parsing "alongside" the page lost
        // the dedup race 7 times out of 7 in measurement (domAlready ==
        // dedup): the site's own await-continuation parsed and rendered the
        // row while our clone was still buffering, so the DOM path always
        // forwarded first and the index time was never sent. Returning a
        // promise that settles only once WE have parsed makes us first by
        // construction rather than by luck.
        //
        // The page still receives the ORIGINAL Response object with every
        // property intact - we only delay it by our own JSON parse of a
        // clone (sub-millisecond on these payloads), never substitute it.
        return out.then((resp) => {
          try {
            return resp
              .clone()
              .json()
              .then((data) => {
                handleFetchPayload(url, data, 'fetch');
                return resp;
              })
              .catch(() => resp); // not JSON, or body unreadable
          } catch {
            return resp; // clone() refused; never withhold the response
          }
        });
      };
    }
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      try {
        if (typeof url === 'string' && TRADE_FETCH_RE.test(url)) {
          // Dormant in practice: the trade site fetches with `fetch`, not
          // XHR (measured viaXhr == 0), so this branch exists only in case
          // that changes. `readystatechange` at DONE fires before `load`
          // and before the page's own onreadystatechange property handler
          // (set after our open(), so it queues behind us); `load` is the
          // fallback and `handled` keeps it to a single parse.
          let handled = false;
          const grab = () => {
            if (handled || this.readyState !== 4) return;
            handled = true;
            try {
              const data =
                this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
              handleFetchPayload(url, data, 'xhr');
            } catch { /* non-JSON response; DOM path covers it */ }
          };
          this.addEventListener('readystatechange', grab);
          this.addEventListener('load', grab);
        }
      } catch { /* never break the page */ }
      return origOpen.call(this, method, url, ...rest);
    };
  }

  // The site replaces the whole `.results` container on some updates; the
  // sweep would catch that within 2s, but rows pushed during the swap
  // deserve better. #vue3-portal is the container's stable mount point
  // (verified in the progenesis snapshot) - watch it and re-attach the
  // moment the container is replaced. The callback is a cheap
  // connectivity check, so subtree noise costs nothing.
  function watchContainerSwap() {
    const portal = document.querySelector('#vue3-portal') || document.body;
    new MutationObserver(() => {
      if (observedContainer && !observedContainer.isConnected) {
        observer.disconnect();
        // same-search swap: rows that appeared during it are real pushes
        attach(false);
      }
    }).observe(portal, { childList: true, subtree: true });
  }

  /**
   * Safety sweep every 2s - the last-resort net behind the fast paths:
   * slow-rendering rows are normally caught by processRow's retry ladder
   * and container swaps by watchContainerSwap, but anything that outlives
   * both (data-id set very late, retry ladder exhausted) is picked up
   * here; unparseable rows stay unseen and retry on the next sweep.
   */
  let currentPath = location.pathname;
  setInterval(() => {
    if (location.pathname !== currentPath) {
      // real navigation: new search context - re-init silently
      currentPath = location.pathname;
      searchId = location.pathname.split('/').filter(Boolean).pop() || 'unknown';
      networkSilentUntil = Date.now() + NETWORK_SILENT_MS;
      observer.disconnect();
      attach(true);
      hello();
      return;
    }
    if (!observedContainer || !observedContainer.isConnected) {
      // container replaced in-place (same search): re-attach WITHOUT the
      // silent marking - rows that appeared during the swap are real
      observer.disconnect();
      if (!attach(false)) return;
    }
    let swept = 0;
    for (const row of observedContainer.querySelectorAll(SELECTORS.rowLoose)) {
      const native = row.getAttribute(SELECTORS.rowIdAttr);
      if (native && seen.has(native)) continue;
      if (handleRow(row, false)) swept += 1;
    }
    if (swept) console.info(`[valdo] sweep recovered ${swept} missed row(s)`);
    refreshRewardIdentity();
  }, SENTINEL_MS);

  // --- click path ----------------------------------------------------------

  function resolveRow(id) {
    const el = rowRefs.get(id)?.deref();
    if (el && el.isConnected) return el;
    if (!observedContainer) return null;
    for (const row of observedContainer.querySelectorAll(SELECTORS.row)) {
      if (listingId(row, null) === id) return row;
    }
    return null;
  }

  /** The row's "Item no longer available" error span, if shown. */
  function unavailableError(rowEl) {
    const err = rowEl.querySelector(SELECTORS.rowError);
    return err && UNAVAILABLE_TEXT.test(err.textContent) ? err : null;
  }

  /**
   * Watches the clicked row for the two known post-click outcomes:
   * - Hot items: the SAME button turns into "In demand. Teleport anyway?"
   *   and needs one more click. That second click completes the same single
   *   user-initiated travel action (one input -> one server action): at
   *   most one confirm click, only within this window, never retried.
   * - Sold items: the row swaps its buttons for an "Item no longer
   *   available" error span; report the failure so the overlay shows
   *   TRAVEL FAILED instead of a silent no-op teleport.
   */
  function watchAfterClick(id, clickedRow) {
    const deadline = Date.now() + CONFIRM_WINDOW_MS;
    let confirmed = false;
    let done = false;

    // A MutationObserver on the clicked row reacts the same frame the
    // button flips (the 100ms poll it replaces averaged +50ms on exactly
    // the most contested items); a slow poll remains as fallback for the
    // row being replaced wholesale, which would orphan the observer.
    const finish = () => {
      done = true;
      rowObserver.disconnect();
      clearInterval(fallback);
    };

    const check = () => {
      if (done) return;
      if (Date.now() > deadline) return finish();
      const rowEl = resolveRow(id) || clickedRow;
      if (!rowEl || !rowEl.isConnected) return;
      if (unavailableError(rowEl)) {
        finish();
        send({ type: 'click_result', listing_id: id, ok: false, reason: 'item_no_longer_available' });
        return;
      }
      const btn = rowEl.querySelector(SELECTORS.travelButton);
      if (confirmed || !btn || !CONFIRM_TEXT.test(btn.textContent.trim())) return;
      confirmed = true; // keep watching: the confirm click can still fail
      btn.click();
      send({ type: 'click_result', listing_id: id, ok: true, reason: 'auto_confirmed' });
    };

    const rowObserver = new MutationObserver(check);
    rowObserver.observe(clickedRow, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
    const fallback = setInterval(check, 250);
    setTimeout(finish, CONFIRM_WINDOW_MS + 100); // hard stop past the window
    check();
  }

  function handleClickTravel(msg) {
    const id = msg.listing_id;
    const reply = (ok, reason) =>
      send({ type: 'click_result', listing_id: id, ok, reason: reason || '' });
    if (clickedIds.has(id)) return reply(false, 'already_clicked');
    const rowEl = resolveRow(id);
    if (!rowEl) return reply(false, 'row_gone');
    if (unavailableError(rowEl)) return reply(false, 'item_no_longer_available');
    const btn = rowEl.querySelector(SELECTORS.travelButton);
    if (!btn) return reply(false, 'button_missing');
    clickedIds.add(id); // guard set before the click: duplicates can never fire
    btn.click(); // the single automated click, bound to one user press/click
    reply(true, '');
    watchAfterClick(id, rowEl);
  }

  attach(true);
  watchContainerSwap();
  hookNetwork();
  connect();
}

if (window.__VALDO_TEST__) {
  window.__valdo = {
    SELECTORS, fnv1a32, extractMods, parsePrice, seenKey,
    parseRow, parseFetchItem, listingId, SeenSet, notePushFrame, msSincePush,
  };
} else {
  hookLiveSocket(); // now, at document-start: the page has not opened its socket yet
  // everything else needs the DOM, and ran at document-idle before 0.9.0
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main, { once: true });
  } else {
    main();
  }
}
