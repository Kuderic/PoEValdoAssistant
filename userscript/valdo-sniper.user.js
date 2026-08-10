// ==UserScript==
// @name         Valdo Map Sniper - capture & forward
// @namespace    valdo-sniper
// @version      0.5.0
// @description  Forwards live-search listings to the local sniper program; clicks Travel to Hideout on command (one command, one click).
// @match        https://www.pathofexile.com/trade*
// @grant        none
// @run-at       document-idle
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

const WS_URL = 'ws://127.0.0.1:8765';
const HEARTBEAT_MS = 30_000;
const SENTINEL_MS = 2_000;
/* network capture: listings taken from /api/trade/fetch responses within
   this window after page init / navigation are page-load fetches for rows
   that were already listed - remembered silently, never alerted (mirrors
   the DOM path's silent attach) */
const NETWORK_SILENT_MS = 4_000;
const TRADE_FETCH_RE = /\/api\/trade\/fetch\//;
/* rows whose price/seller render a tick after insertion retry on this
   schedule (first retry is a rAF) instead of waiting for the 2s sweep */
const RETRY_DELAYS_MS = [50, 100, 200, 400];
const SEEN_CAP = 500;
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const PENDING_CAP = 50; // listings queued during ws reconnects
const PENDING_MAX_AGE_MS = 60_000;

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
 * Parse one result row into the new_listing payload fields.
 * Returns null when mandatory fields are missing (row not fully rendered yet).
 */
function parseRow(rowEl) {
  const itemName = textOf(rowEl, SELECTORS.itemName);
  const amountText = textOf(rowEl, SELECTORS.priceAmount).replace(/[,×x]/g, '');
  // Prefer the currency image's alt ("divine" - the short trade id used by
  // config and poe.ninja); fall back to the visible text ("Divine Orb").
  const currencyImg = rowEl.querySelector(SELECTORS.priceCurrencyImg);
  const currency = (currencyImg && currencyImg.getAttribute('alt')?.trim())
    || textOf(rowEl, SELECTORS.priceCurrencyText);
  const seller = textOf(rowEl, SELECTORS.seller);
  const amount = parseFloat(amountText);
  if (!itemName || !currency || !seller || !Number.isFinite(amount)) return null;
  return {
    item_name: itemName,
    price: { amount, currency },
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
 *   strings on the public API - both accepted).
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
    price: { amount, currency },
    seller,
    reward: typeof reward === 'string' && reward ? reward : null,
    mods,
  };
}

/** Bounded set with FIFO eviction; the dedup authority for forwarded rows. */
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
    if (ws && ws.readyState === WebSocket.OPEN) {
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

  let lastSentReward = null;

  function hello() {
    lastSentReward = majorityReward();
    send({ type: 'hello', search_id: searchId, tab_id: tabId, search_reward: lastSentReward });
  }

  /**
   * Re-hello immediately once the tab's reward becomes knowable (rows may
   * render after the first hello), instead of waiting for the heartbeat.
   */
  function refreshRewardIdentity() {
    const reward = majorityReward();
    if (reward && reward !== lastSentReward) hello();
  }

  function connect() {
    ws = new WebSocket(WS_URL);
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
    // cheap native-id check first so sweeps skip seen rows without parsing
    const native = rowEl.getAttribute(SELECTORS.rowIdAttr);
    if (native && seen.has(native)) return true;
    const parsed = parseRow(rowEl);
    if (!parsed) return false;
    const id = listingId(rowEl, parsed);
    if (!id) return false;
    if (seen.has(id)) return true;
    seen.add(id);
    rowRefs.set(id, new WeakRef(rowEl));
    if (silent) return true;
    const rows = Array.from(rowEl.parentElement?.children ?? []);
    send({
      type: 'new_listing',
      search_id: searchId,
      tab_id: tabId,
      listing_id: id,
      ...parsed,
      row_index: rows.indexOf(rowEl),
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
   * (price/seller still rendering) is re-tried on a rAF then short
   * timeouts, so it forwards within ~50ms instead of waiting up to 2s for
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
    requestAnimationFrame(retry);
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

  function handleFetchEntry(entry) {
    const parsed = parseFetchItem(entry);
    if (!parsed || seen.has(parsed.id)) return;
    seen.add(parsed.id);
    if (Date.now() < networkSilentUntil) return; // page-load fetch, not a push
    const { id, ...fields } = parsed;
    send({
      type: 'new_listing',
      search_id: searchId,
      tab_id: tabId,
      listing_id: id,
      ...fields,
      row_index: -1, // no DOM row yet
      detected_at: new Date().toISOString(),
    });
  }

  function handleFetchPayload(url, data) {
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
        try {
          const url = typeof input === 'string' ? input : input?.url || '';
          if (TRADE_FETCH_RE.test(url)) {
            out
              .then((resp) => resp.clone().json())
              .then((data) => handleFetchPayload(url, data))
              .catch(() => {});
          }
        } catch { /* never break the page */ }
        return out;
      };
    }
    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      try {
        if (typeof url === 'string' && TRADE_FETCH_RE.test(url)) {
          this.addEventListener('load', () => {
            try {
              const data =
                this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
              handleFetchPayload(url, data);
            } catch { /* non-JSON response; DOM path covers it */ }
          });
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
  window.__valdo = { SELECTORS, fnv1a32, extractMods, parseRow, parseFetchItem, listingId, SeenSet };
} else {
  main();
}
