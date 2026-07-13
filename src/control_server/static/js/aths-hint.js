/* Add-to-Home-Screen first-run hint controller (M6 DD1/DD4/DD5/F2/F5).
 *
 * Lifecycle:
 *   1. On DOMContentLoaded — if the user is already in standalone mode
 *      (PWA pinned to home screen), or has previously dismissed the hint,
 *      do nothing. F2: detect via BOTH `(display-mode: standalone)` media
 *      query AND iOS-only `navigator.standalone` boolean — Safari iOS
 *      doesn't always honor the media query; either truthy = hide.
 *   2. Otherwise wait 600ms (DESIGN.md "First-run hint" spec) then add the
 *      .aths-hint--ready class — CSS opens it via the viewport-gate media
 *      query at max-width: 599px (DD4).
 *   3. Tap the X (DD5 — 44×44 tap area, aria-label "Dismiss hint") → fade
 *      out + write 'true' to localStorage so we don't show it again.
 *      F5: localStorage access wrapped in try/catch (Safari Private mode
 *      throws on setItem). Failed writes mean we just stay hidden in-
 *      memory for the rest of the session.
 *   4. If the user navigates within the PWA before tapping, auto-dismiss
 *      WITHOUT writing localStorage so the hint reappears on next return.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'litclock-aths-dismissed';

  function isStandalone() {
    // F2: belt-and-suspenders detection. Either signal truthy = hide.
    try {
      if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) {
        return true;
      }
    } catch (_e) { /* matchMedia missing on very old browsers — fall through */ }
    if (typeof navigator !== 'undefined' && navigator.standalone === true) {
      return true;
    }
    return false;
  }

  function getDismissed() {
    // F5: localStorage access can throw in Safari Private mode.
    try {
      return window.localStorage.getItem(STORAGE_KEY) === 'true';
    } catch (_e) {
      return false;
    }
  }

  function setDismissed() {
    try {
      window.localStorage.setItem(STORAGE_KEY, 'true');
    } catch (_e) {
      // Swallow — in-memory dismissal is the best we can do this session.
    }
  }

  function isAndroid() {
    // codex /review M6 — Android Chrome installs via Chrome's menu (⋮) →
    // "Install app", not via Safari's share toolbar. Branch the copy + icon
    // so the hint points users at the correct control. Default (anything
    // else, including iOS) keeps the iOS copy from the base template.
    return /Android/i.test(navigator.userAgent || '');
  }

  // /review feedback on #406 — coordination with status.js's mDNS probe.
  // status.js sets window.__litclockMdnsPending = true synchronously at
  // script eval when it WILL run the probe (on /status, HTTP origin, not
  // already on .local, not previously dismissed). After the probe settles,
  // it dispatches `litclock:mdns-result` with `{available: true|false}`.
  //
  //   available: true  → mDNS bookmark card is showing instead of AtHS;
  //                      suppress AtHS this load. AtHS will fire naturally
  //                      on the .local origin once the user taps Switch.
  //   available: false → No bookmark card; fall through to normal AtHS.
  //
  // Defensive 4s timeout in case the probe never signals (script error,
  // hung fetch with no AbortController). 4s = mDNS probe delay (2s) +
  // mDNS probe timeout (1.5s) + cushion (0.5s). Without the timeout,
  // an unexpected failure mode would suppress AtHS forever this session.
  var MDNS_RESULT_EVENT = 'litclock:mdns-result';
  var MDNS_DEFER_TIMEOUT_MS = 4000;

  function init() {
    // EPIC #383 PR2 (#388) — while the post-WiFi handoff banner is showing,
    // suppress this hint so the two cards don't compete for attention. The
    // banner sets `data-handoff-active` on <body> (server-side), and
    // handoff.js removes it + dispatches `litclock:handoff-complete` once the
    // user finishes — at which point we run the normal first-run logic (the
    // listener registered at the bottom of this file). Natural sequence:
    // handoff → AtHS hint.
    if (document.body && document.body.hasAttribute('data-handoff-active')) return;
    if (isStandalone()) return;
    if (getDismissed()) return;

    // /review #406 — defer while the mDNS probe is in flight. See the
    // MDNS_RESULT_EVENT block at the top of this file for the contract.
    if (window.__litclockMdnsPending === true) {
      var deferTimer = setTimeout(function () {
        // Probe never signaled — assume failure and proceed.
        proceedToShow();
      }, MDNS_DEFER_TIMEOUT_MS);
      document.addEventListener(MDNS_RESULT_EVENT, function (ev) {
        clearTimeout(deferTimer);
        if (ev && ev.detail && ev.detail.available === true) {
          // mDNS bookmark card is the bookmark-prompt this load. Don't
          // open AtHS — user will see AtHS on the .local origin after
          // tapping Switch (different localStorage scope, fresh state).
          return;
        }
        proceedToShow();
      }, { once: true });
      return;
    }

    proceedToShow();
  }

  function proceedToShow() {
    var card = document.querySelector('.aths-hint');
    if (!card) return;

    // Platform branch BEFORE opening so the user never sees the iOS copy
    // flash before the Android copy on a slow paint.
    if (isAndroid()) {
      card.classList.remove('aths-hint--ios');
      card.classList.add('aths-hint--android');
    }

    // 600ms delay per DESIGN.md "First-run hint" spec.
    var openTimer = setTimeout(function () {
      card.classList.add('aths-hint--ready');
    }, 600);

    // Auto-dismiss-without-persist on internal navigation (DESIGN.md spec).
    // Tab anchors in .tabbar each fire a navigation; cancel the open timer
    // and just hide the card. Don't write localStorage — the user didn't
    // actually engage with the hint, they just moved on.
    var navLinks = document.querySelectorAll('.tabbar a');
    Array.prototype.forEach.call(navLinks, function (a) {
      a.addEventListener('click', function () {
        clearTimeout(openTimer);
        card.classList.remove('aths-hint--ready');
      });
    });

    // Dismiss X.
    var dismissBtn = card.querySelector('.aths-hint__dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', function (event) {
        event.preventDefault();
        clearTimeout(openTimer);
        card.classList.remove('aths-hint--ready');
        setDismissed();
      });
    }
  }

  // EPIC #383 PR2 (#388) — run the first-run logic once the handoff banner
  // completes (handoff.js dispatches this after removing data-handoff-active).
  document.addEventListener('litclock:handoff-complete', init);

  // /review #406 — defer init() one tick so status.js (loaded after this
  // file in document order via `defer`) has a chance to set
  // `window.__litclockMdnsPending` synchronously. Without the yield,
  // aths-hint runs first under `defer`+`readyState==="interactive"`,
  // reads the flag as undefined, and misses the mDNS coordination
  // entirely (the 4s defensive timeout would never matter because
  // aths-hint would already have proceeded). setTimeout(0) enqueues
  // a macrotask that fires after both `defer` script bodies have run.
  function scheduleInit() { setTimeout(init, 0); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scheduleInit);
  } else {
    scheduleInit();
  }
})();
