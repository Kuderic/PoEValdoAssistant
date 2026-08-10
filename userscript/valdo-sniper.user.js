// ==UserScript==
// @name         Valdo Map Sniper - capture & forward
// @namespace    valdo-sniper
// @version      0.3.0
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
  rowIdAttr: 'data-id',
  itemName: '.middle .item-popup__header-line',
  priceAmount: '.details [data-field="price"] > span:nth-of-type(2)',
  priceCurrencyImg: '.details [data-field="price"] .currency-text img', // alt="divine"
  priceCurrencyText: '.details [data-field="price"] .currency-text span', // "Divine Orb" fallback
  seller: '.details .info .profile-link a',
  modLine: '.item-mod span[data-field^="stat."]',
  rewardValue: '.item-property span[type="76"] > span:last-of-type',
  travelButton: 'button.direct-btn',
};

// Hot-item confirmation (verified from in-demand-confirmation.html snapshot,
// 2026-08-09): there is NO modal - the Travel button itself turns into
// "In demand. Teleport anyway?" (class gains "expired") and must be clicked
// a second time to complete the same travel action.
const CONFIRM_TEXT = /in demand|teleport anyway/i;
const CONFIRM_WINDOW_MS = 3_000;

const WS_URL = 'ws://127.0.0.1:8765';
const HEARTBEAT_MS = 30_000;
const SENTINEL_MS = 2_000;
const SEEN_CAP = 500;
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

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

  const searchId = location.pathname.split('/').filter(Boolean).pop() || 'unknown';
  const seen = new SeenSet(SEEN_CAP);
  const rowRefs = new Map(); // listing_id -> WeakRef<Element>
  const clickedIds = new Set(); // one command = one click, forever per id

  // --- websocket -----------------------------------------------------------
  let ws = null;
  let reconnectDelay = RECONNECT_MIN_MS;

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
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

  function hello() {
    send({ type: 'hello', search_id: searchId, tab_id: tabId, search_reward: majorityReward() });
  }

  function connect() {
    ws = new WebSocket(WS_URL);
    ws.addEventListener('open', () => {
      reconnectDelay = RECONNECT_MIN_MS;
      console.info('[valdo] connected');
      hello();
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

  function handleRow(rowEl, silent) {
    const parsed = parseRow(rowEl);
    if (!parsed) return;
    const id = listingId(rowEl, parsed);
    if (!id || seen.has(id)) return;
    seen.add(id);
    rowRefs.set(id, new WeakRef(rowEl));
    if (silent) return;
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
  }

  function collectRows(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return [];
    const out = node.matches(SELECTORS.row) ? [node] : [];
    out.push(...node.querySelectorAll(SELECTORS.row));
    return out;
  }

  let observedContainer = null;
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        for (const row of collectRows(node)) handleRow(row, false);
      }
    }
  });

  function attach(silent) {
    const container = document.querySelector(SELECTORS.resultsContainer);
    if (!container) return false;
    observedContainer = container;
    // Rows already present were not pushed live in front of us - never alert
    // on them, only remember them.
    for (const row of container.querySelectorAll(SELECTORS.row)) handleRow(row, true);
    observer.observe(container, { childList: true, subtree: true });
    console.info(`[valdo] observing results container (silent init: ${silent})`);
    return true;
  }

  // The site can replace the container on navigation; re-attach when it dies.
  setInterval(() => {
    if (!observedContainer || !observedContainer.isConnected) {
      observer.disconnect();
      attach(true);
    }
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

  /**
   * Hot items: after the Travel click, the SAME button turns into
   * "In demand. Teleport anyway?" and needs one more click. That second
   * click completes the same single user-initiated travel action (one
   * input -> one server action): at most one confirm click, only within a
   * short window after the travel click, never retried.
   */
  function watchForConfirm(id) {
    const deadline = Date.now() + CONFIRM_WINDOW_MS;
    const timer = setInterval(() => {
      if (Date.now() > deadline) return clearInterval(timer);
      const rowEl = resolveRow(id);
      const btn = rowEl && rowEl.querySelector(SELECTORS.travelButton);
      if (!btn || !CONFIRM_TEXT.test(btn.textContent.trim())) return;
      clearInterval(timer);
      btn.click();
      send({ type: 'click_result', listing_id: id, ok: true, reason: 'auto_confirmed' });
    }, 100);
  }

  function handleClickTravel(msg) {
    const id = msg.listing_id;
    const reply = (ok, reason) =>
      send({ type: 'click_result', listing_id: id, ok, reason: reason || '' });
    if (clickedIds.has(id)) return reply(false, 'already_clicked');
    const rowEl = resolveRow(id);
    if (!rowEl) return reply(false, 'row_gone');
    const btn = rowEl.querySelector(SELECTORS.travelButton);
    if (!btn) return reply(false, 'button_missing');
    clickedIds.add(id); // guard set before the click: duplicates can never fire
    btn.click(); // the single automated click, bound to one user press/click
    reply(true, '');
    watchForConfirm(id);
  }

  attach(true);
  connect();
}

if (window.__VALDO_TEST__) {
  window.__valdo = { SELECTORS, fnv1a32, extractMods, parseRow, listingId, SeenSet };
} else {
  main();
}
