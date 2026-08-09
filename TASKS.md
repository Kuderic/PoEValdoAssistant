# Build order

Work strictly in order. A milestone is done when its acceptance criteria
pass and the result is demonstrated against real (or snapshot) data.

## Milestone 1 - Userscript: capture and forward

- [x] Ask the user for a saved HTML snapshot of a live search results page;
      derive selectors from it, keep them in one constants block.
- [x] MutationObserver on the results container; extract item name, price
      amount + currency, seller, and a stable/derived listing ID per new row.
- [x] Connect to `ws://127.0.0.1:8765`, send `hello` on connect + every 30s,
      send `new_listing` per row; auto-reconnect with backoff.
- [x] Listen for `click_travel`, click the matching row's Travel to Hideout
      button, reply with `click_result`.

Accept when: with a stub Python echo server, opening a live search tab
produces well-formed `new_listing` frames, and a manually sent
`click_travel` clicks the right row exactly once.

## Milestone 2 - Python core: server, prices, margin

- [x] Websocket server accepting multiple tabs; parse/validate messages.
- [x] poe.ninja client: verify current endpoint and foil Valdo coverage;
      implement 10-min cache + chaos/divine normalization; fall back to the
      manual price table in `config.yaml` when coverage is missing.
- [x] Margin engine with per-map and global thresholds from `config.yaml`.
- [x] Structured logging of every listing and decision to `logs/`.

Accept when: replaying a recorded stream of `new_listing` frames yields
correct margin decisions in the log, including a currency-normalized case.

## Milestone 3 - Alert surface

- [x] Always-on-top tkinter overlay: item, amount + currency in large text,
      margin %, seller, per-tab connection dots, alert countdown.
- [x] Distinct alert sound on above-threshold listings.
- [x] Currency MISMATCH banner per DESIGN.md scam guard.

Accept when: a simulated above-threshold listing produces sound + overlay
within 100ms of the frame arriving, and a mismatched-currency listing shows
the banner.

## Milestone 4 - Hotkey and return path

- [x] Global hotkey bound from `config.yaml`; inert with a soft warning when
      the PoE process is not running.
- [x] On press: send `click_travel` for the current (non-expired) alert
      only, then focus the PoE window. One press, one click, no retries.
- [x] Alert expiry so a stale listing can never be traveled to by reflex.
- [x] Surface `click_result` failures on the overlay.

Accept when: end-to-end dry run (stub row injected into a real tab) goes
frame -> alert -> single hotkey -> exactly one click + game focused, and a
second hotkey press without a new alert does nothing.

## Milestone 5 - Hardening

- [x] Reconnect/resilience pass on both ws sides; overlay shows degraded
      state honestly.
- [x] Config validation with clear startup errors.
- [x] Latency instrumentation: log detected_at -> alert-shown deltas;
      target < 150ms on the hot path.
- [x] README with setup steps (Tampermonkey install, config, run).