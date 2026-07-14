/* System tab interactivity (#245 M4 — Stories 2.2 + 3.1).
 *
 * Progressive enhancement: action cards in system.html.j2 render as plain
 * <form> elements that POST to /api/system/{action} with a hidden confirm
 * token. Without JS, tapping the button submits the form directly — the
 * destructive action still gates on the confirm-token TTL.
 *
 * With JS, we intercept the form submit, open the matching <dialog> sheet
 * with the locked DESIGN.md confirm-modal copy, and only fire the action
 * when the user taps the destructive primary button. Esc key + backdrop
 * tap both dismiss without firing the action.
 *
 * After a successful reboot POST (Story 3.1), the page swaps to a
 * "Restarting…" reconnect card and polls /api/health every 2s with a 1s
 * timeout. On the first successful response it waits one more second
 * (let services settle) and reloads. After 90s of failed polls it shows
 * a "Couldn't reconnect — Tap to retry" state.
 *
 * Power off goes to a terminal "LitClock is off — unplug and re-plug"
 * card with no polling (per locked failure-mode line 200 in the plan).
 */

(function () {
  'use strict';

  // #317 item 7 — Prepare-for-Gifting wiring (moved from settings.js).
  //
  // Two pieces of progressive enhancement for the gift card:
  //
  // (a) textarea → hidden `message` field mirror. The draft form posts to
  //     /settings (section=gift) and the destructive form posts to
  //     /api/system/prepare-for-gift; they're sibling <form>s, so the
  //     textarea is in a different DOM scope from the hidden field. Mirror
  //     on every input event so JS-enabled clients ship unsaved edits via
  //     the Prepare button. No-JS clients get the server-rendered persisted
  //     draft, which is the right fallback.
  //
  // (b) Live character counter — #319 fix. The textarea has maxlength=80 so
  //     the browser silently stops accepting input at the cap, but the
  //     user has no idea where they are until they hit it. The counter
  //     updates on `input`, turns warning at ≥80% (64), and treats the
  //     hard cap (80) as the error point. aria-live=polite on the element
  //     announces the count to screen readers without spamming.
  //
  //     #317 item 3 codex follow-up: the server-side validator caps
  //     GIFT_MODE_MESSAGE at 80 CODEPOINTS AND 80 BYTES (the byte cap
  //     mirrors reset-setup.sh's os.read(fd, 80) byte-bound consumer).
  //     A codepoint-only counter would stay green for 79 ASCII + 1 emoji
  //     (80 codepoints, 83 bytes) but the save would 422. Compute BOTH
  //     and trigger the warn/at-limit class on whichever cap is closer —
  //     the user gets honest feedback regardless of which limit bites.
  //     The visible "N / 80" count stays as codepoints so it matches the
  //     textarea's ``maxlength`` semantics and the "80 characters" mental
  //     model from the help copy.
  //
  // Both run regardless of <dialog> support — they're DOM-independent of
  // the confirm-sheet wiring below.
  var giftMessageSource = document.querySelector('[data-gift-message-source]');
  var giftMessageSync = document.querySelector('[data-gift-message-sync]');
  if (giftMessageSource && giftMessageSync) {
    giftMessageSource.addEventListener('input', function () {
      giftMessageSync.value = giftMessageSource.value;
    });
  }
  var giftCounter = document.querySelector('[data-gift-counter]');
  if (giftMessageSource && giftCounter) {
    var counterMax = parseInt(giftCounter.dataset.counterMax, 10) || 80;
    var counterWarnAt = Math.floor(counterMax * 0.8);
    // Use Array.from(str).length to count Unicode codepoints — String#length
    // returns UTF-16 code units, so a single emoji counts as 2 and would
    // make the counter show "is-at-limit" red at 40 emoji while Python's
    // validator (which uses len(value) = codepoints) still passes 80.
    // Adversarial /review caught this mirror of #317 item 3.
    var codepointLength = function (s) {
      return Array.from(s).length;
    };
    // UTF-8 byte length. TextEncoder is Pi-PWA-safe (iOS Safari 10.1+,
    // Chrome 38+ — well below the M2 PWA support floor). Fall back to a
    // simple codepoint-range estimator if TextEncoder is missing so the
    // counter still degrades safely on the rare ancient WebView.
    var utf8ByteLength = function (s) {
      if (typeof TextEncoder === 'function') {
        return new TextEncoder().encode(s).length;
      }
      // Fallback: estimate from codepoint ranges (UTF-8 byte width per cp).
      var bytes = 0;
      for (var i = 0; i < s.length; i++) {
        var cp = s.codePointAt(i);
        if (cp >= 0x10000) { i++; bytes += 4; }
        else if (cp >= 0x800) { bytes += 3; }
        else if (cp >= 0x80) { bytes += 2; }
        else { bytes += 1; }
      }
      return bytes;
    };
    var updateCounter = function () {
      var cpLen = codepointLength(giftMessageSource.value);
      var byteLen = utf8ByteLength(giftMessageSource.value);
      var effective = Math.max(cpLen, byteLen);
      giftCounter.textContent = cpLen + ' / ' + counterMax;
      giftCounter.classList.toggle('is-at-limit', effective >= counterMax);
      giftCounter.classList.toggle('is-near-limit', effective >= counterWarnAt && effective < counterMax);
    };
    giftMessageSource.addEventListener('input', updateCounter);
    updateCounter();
  }

  var POLL_INTERVAL_MS = 2000;
  var POLL_TIMEOUT_MS = 1000;
  var SETTLE_DELAY_MS = 1000;
  var RECONNECT_DEADLINE_MS = 90000;
  // Power off — after /api/health stops responding (network down ≈ services
  // stopped), wait this long to cover the post-network filesystem-sync
  // window before telling the user it's safe to unplug. Pi Zero 2W with our
  // service set typically syncs in 5–10s; 20s gives generous margin.
  // E-ink "powered off" frame lands earlier (SPI floor); the PWA countdown
  // is what protects against an enthusiastic unplug ahead of disk sync.
  var POWEROFF_SAFETY_COUNTDOWN_S = 20;

  // No-op on browsers without <dialog> support — form falls back to native
  // POST. iOS Safari 15.4+ + Chrome 37+ + Firefox 98+ all have it; the
  // floor matches the M2 PWA shell support matrix.
  var sample = document.querySelector('dialog.confirm-sheet');
  if (!sample || typeof sample.showModal !== 'function') {
    return;
  }

  // Map action → dialog element.
  var dialogs = {};
  document.querySelectorAll('dialog.confirm-sheet[data-action]').forEach(function (dialog) {
    dialogs[dialog.dataset.action] = dialog;
  });

  // #305: open the confirm sheet so the slide-up keyframe actually fires
  // on iOS Safari pre-17.5. Native <dialog>.showModal() flips
  // display:none → block AND promotes the element into the top layer in
  // the same paint, so a CSS keyframe gated on `[open]` never observes
  // its `from` state. Workaround: showModal first, wait two animation
  // frames (one for top-layer promotion, one for the paint), then add
  // `.is-opening` so the keyframe starts cleanly. confirm-sheet.css
  // animates `.is-opening` instead of `[open]`. The close listener
  // strips the class so a re-open re-triggers the animation.
  //
  // Defensive guards (codex /review on #305 PR):
  //   1. Strip `.is-opening` BEFORE showModal so a stale class from a
  //      prior interrupted open doesn't suppress the current animation.
  //   2. Check `dialog.open` inside the rAF callback so a user who taps
  //      Cancel within the ~33ms double-rAF window doesn't end up with
  //      `.is-opening` set on a closed dialog (which would suppress the
  //      animation on the NEXT open, defeating the fix).
  function openConfirmSheet(dialog) {
    dialog.classList.remove('is-opening');
    dialog.showModal();
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (dialog.open) {
          dialog.classList.add('is-opening');
        }
      });
    });
  }
  Object.keys(dialogs).forEach(function (action) {
    dialogs[action].addEventListener('close', function () {
      dialogs[action].classList.remove('is-opening');
    });
  });

  // Intercept each action-card form's submit.
  document.querySelectorAll('form[data-confirm-action]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      var action = form.dataset.confirmAction;
      var dialog = dialogs[action];
      if (!dialog) {
        // No matching dialog (template/JS drift) — fall back to native POST.
        return;
      }
      event.preventDefault();
      openConfirmSheet(dialog);
    });
  });

  // Wire each dialog's buttons + backdrop dismiss.
  Object.keys(dialogs).forEach(function (action) {
    var dialog = dialogs[action];
    var form = document.querySelector('form[data-confirm-action="' + action + '"]');

    var cancelBtn = dialog.querySelector('[data-modal-cancel]');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', function () {
        dialog.close('cancel');
      });
    }

    var confirmBtn = dialog.querySelector('[data-modal-confirm]');
    if (confirmBtn && form) {
      confirmBtn.addEventListener('click', function () {
        dialog.close('confirm');
        fireAction(form, action);
      });
    }

    // Backdrop-tap dismiss. The dialog itself receives the click when the
    // user taps the dimmed backdrop (outside .confirm-sheet__inner).
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) {
        dialog.close('cancel');
      }
    });
  });

  function fireAction(form, action) {
    var tokenInput = form.querySelector('input[name="token"]');
    if (!tokenInput || !tokenInput.value) {
      // No token — nothing useful to do; surface the issue.
      window.alert('Confirm token missing. Reload the page and try again.');
      return;
    }
    postAction(form, action, tokenInput, false);
  }

  // #317 item 1 (TTL-expiry-mid-typing half): on a 401 with the SPECIFIC
  // ``confirm_token_expired`` slug for prepare_for_gift, mint a fresh
  // token via /api/system/confirm-token and replay the action POST
  // exactly once. The slow-drafter path — open /system, type a message,
  // wait 5+ minutes past the 300s TTL, then tap "Prepare for Gifting" —
  // was alerting + losing session despite the textarea contents still
  // being valid. Scoped to prepare_for_gift because reboot/poweroff/
  // wifi_reset all confirm in seconds (no drafting surface), so an
  // expired token there is a real staleness signal worth surfacing
  // rather than silently papering over.
  //
  // #317 item 1 codex P2 follow-up — the server now distinguishes three
  // token failure modes (`confirm_token_expired` 401, `confirm_token_consumed`
  // 409, `confirm_token_invalid` 401). The refresh-and-retry path gates
  // on `confirm_token_expired` ONLY. A `confirm_token_consumed` response
  // (double-click, bfcached resubmit, page restore) must NOT trigger
  // a refresh-and-retry — that would silently double-fire the
  // destructive one-shot action by bypassing the single-use guard.
  // For `consumed` we surface a specific "already submitted" message
  // and stop. For the residual `invalid` case (genuinely malformed /
  // unknown token — buggy client or attack) we keep the existing alert
  // path.
  //
  // The `retried` flag enforces one-retry-only: if the fresh token also
  // 401s, fall through to the existing alert path. Refresh-endpoint
  // failures (network, 5xx, malformed body) likewise fall through —
  // never silently swallow.
  function postAction(form, action, tokenInput, retried) {
    // #316 /review CRITICAL fix — include every additional hidden field
    // beyond `token` in the JSON body so destructive forms can carry
    // action-specific state. Prepare-for-Gifting needs `message`; without
    // this loop the JS path always submitted an empty message, defeating
    // the entire customizable-welcome feature on JS-enabled clients (the
    // no-JS form-encoded path was working correctly). Sibling endpoints
    // (reboot/poweroff/wifi_reset) only ship `token`, so the loop is a
    // no-op there; this future-proofs any later action that needs more.
    //
    // #319 hardware-QA fix: the destructive Prepare form's `message` field
    // is a `<textarea hidden>` (not a hidden `<input>`) so newlines
    // round-trip on the no-JS path. The original `input[type="hidden"]`
    // selector missed it, so the JS path shipped an empty message and the
    // e-ink fell back to the default welcome regardless of what the user
    // typed. Caught by destructive-flow hardware QA. Now matches both
    // hidden inputs AND hidden textareas; the field-collection logic is
    // element-type-agnostic via the shared `name`/`value` interface.
    var payload = { token: tokenInput.value };
    var hiddenFields = form.querySelectorAll('input[type="hidden"], textarea[hidden]');
    for (var i = 0; i < hiddenFields.length; i++) {
      var field = hiddenFields[i];
      if (field.name && field.name !== 'token' && !(field.name in payload)) {
        payload[field.name] = field.value;
      }
    }

    fetch(form.action, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        if (response.ok) {
          enterReconnectState(action);
          return;
        }
        // 4xx/5xx — surface the message from the M4 error envelope.
        return response
          .json()
          .then(function (body) {
            var code = body && body.error && body.error.code;
            // #317 item 1: refresh-and-retry once on TTL expiry,
            // prepare_for_gift only. Gated on the SPECIFIC
            // `confirm_token_expired` slug (#317 item 1 codex P2)
            // so a `confirm_token_consumed` double-submit cannot
            // bypass the single-use guard via this branch.
            if (
              !retried &&
              response.status === 401 &&
              code === 'confirm_token_expired' &&
              action === 'prepare_for_gift'
            ) {
              refreshTokenAndRetry(form, action, tokenInput, body);
              return;
            }
            // #317 item 1 codex P2 — explicit `confirm_token_consumed`
            // branch. The destructive action was ALREADY submitted (in
            // flight elsewhere, or the form was resubmitted from a
            // bfcached page). Surface a specific message and STOP — do
            // NOT refresh-and-retry. A reload mints a fresh token if the
            // user genuinely needs to retry (e.g., the first run failed
            // server-side without the route detecting it).
            if (response.status === 409 && code === 'confirm_token_consumed') {
              var consumedMsg = (body && body.error && body.error.message)
                ? body.error.message
                : 'This action was already submitted. Reload the page if you need to retry.';
              window.alert(consumedMsg);
              return;
            }
            var msg = (body && body.error && body.error.message)
              ? body.error.message
              : 'Request failed (HTTP ' + response.status + ').';
            window.alert(msg);
          })
          .catch(function () {
            window.alert('Request failed (HTTP ' + response.status + ').');
          });
      })
      .catch(function () {
        // Network error — usually means systemd raced the response and the
        // box is already going down. Treat as success and start the
        // reconnect flow rather than alarming the user.
        enterReconnectState(action);
      });
  }

  // Mint a fresh confirm token via /api/system/confirm-token and replay the
  // action POST. On any failure (refresh request errors, non-200 response,
  // missing/empty token in body), fall through to the original error path
  // by alerting with the original-response message. Single shot — the
  // retried=true flag on the replay ensures we don't loop on a broken
  // server. Adversarial /review concern noted: the body is awaited
  // BEFORE the refresh call so we have something to fall back to if the
  // refresh itself fails.
  function refreshTokenAndRetry(form, action, tokenInput, originalErrorBody) {
    var fallbackMsg = (originalErrorBody && originalErrorBody.error && originalErrorBody.error.message)
      ? originalErrorBody.error.message
      : 'Confirm token expired. Reload the page and try again.';
    fetch('/api/system/confirm-token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify({ action: action })
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('refresh-failed');
        }
        return response.json();
      })
      .then(function (body) {
        if (!body || typeof body.token !== 'string' || !body.token) {
          throw new Error('refresh-no-token');
        }
        tokenInput.value = body.token;
        postAction(form, action, tokenInput, true);
      })
      .catch(function () {
        window.alert(fallbackMsg);
      });
  }

  // ─── Reconnect state machine ───────────────────────────────────────────

  var pollDeadline = null;
  var pollTimer = null;

  function enterReconnectState(action) {
    var main = document.querySelector('main');
    if (!main) return;

    // Tear down any existing dialog state — sheets shouldn't outlive the
    // page they decorate.
    Object.keys(dialogs).forEach(function (a) {
      try { dialogs[a].close(); } catch (_) { /* not open */ }
    });

    if (action === 'poweroff') {
      // Three-phase shutdown UX:
      //   1. "Shutting down…"  — while /api/health still responds
      //   2. "Almost done…N s" — countdown after first failed poll, covers
      //                          the post-network filesystem-sync window
      //   3. "Safe to unplug." — terminal state
      // Caught in hardware QA: e-ink "powered off" frame lands ~10s before
      // the kernel actually halts (SPI subsystem floor), and the original
      // immediate "safe to unplug" message let an enthusiastic user yank
      // the plug mid-fsync → SD card corruption risk.
      renderShuttingDownCard(main);
      pollDeadline = Date.now() + RECONNECT_DEADLINE_MS;
      schedulePoweroffPoll();
      return;
    }

    if (action === 'wifi_reset') {
      // Reset-WiFi handoff (#245 M5 D11). The litclock-wifi-reset.service
      // unit drops the LAN and restarts firstboot.service which brings up
      // the LitClock-Setup hotspot. There's no point polling /api/health
      // — control_server is being stopped. Just show terminal handoff
      // copy with the locked DESIGN.md text and let the user follow the
      // instruction to switch their phone WiFi.
      renderResetWifiCard(main);
      return;
    }

    if (action === 'factory_reset') {
      // #510 — Factory reset handoff. litclock-reset.service wipes config +
      // WiFi and reboots into first-boot (the LitClock-Setup hotspot). Like
      // wifi_reset, the LAN drops and control_server goes down, so show
      // terminal handoff copy rather than polling /api/health.
      renderFactoryResetCard(main);
      return;
    }

    if (action === 'prepare_for_gift') {
      // #316 /review CRITICAL fix — Prepare-for-Gifting handoff. The
      // litclock-prepare-for-gift.service unit wipes WiFi, paints the
      // welcome on the e-ink, then powers the device off. Like
      // wifi_reset, there's no point polling /api/health — the box is
      // going away. Show terminal handoff copy from the locked DESIGN.md
      // confirm-modal description ("Pack and ship the device") so the
      // user knows the action succeeded; falling through to
      // renderRestartingCard would mislead them with "Restarting…" + a
      // dead Retry button (device is OFF, never coming back without
      // physical re-plug + recipient first-boot).
      renderPrepareForGiftCard(main);
      return;
    }

    // Reboot path: show "Restarting…" + start health polling.
    renderRestartingCard(main);
    pollDeadline = Date.now() + RECONNECT_DEADLINE_MS;
    scheduleNextPoll();
  }

  function renderRestartingCard(main) {
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="restarting">' +
      '  <h2 class="reconnect-state__title"><em>Restarting…</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    Your quote will be back in about 30 seconds.' +
      '  </p>' +
      '</section>';
  }

  function renderResetWifiCard(main) {
    // #245 M5 D11 — terminal handoff copy. No health-poll: by the time
    // this renders, litclock-wifi-reset.service has either started OR is
    // about to start, and the LAN is going away regardless. The user has
    // to switch their phone over to the LitClock-Setup hotspot.
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="wifi-reset">' +
      '  <h2 class="reconnect-state__title"><em>Switching to setup mode…</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    Connect your phone to the <strong>LitClock-Setup</strong> hotspot, then enter your new WiFi.' +
      '  </p>' +
      '  <p class="reconnect-state__body">' +
      '    Your location, weather, and gift settings stay saved.' +
      '  </p>' +
      '</section>';
  }

  function renderFactoryResetCard(main) {
    // #510 — terminal handoff copy. No health-poll: litclock-reset.service is
    // wiping config + WiFi and rebooting into setup, so the LAN is going away.
    // Distinct from wifi-reset — this wiped EVERYTHING, so no "settings stay
    // saved" reassurance line.
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="factory-reset">' +
      '  <h2 class="reconnect-state__title"><em>Factory reset in progress…</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    The clock is erasing its settings and rebooting into setup.' +
      '  </p>' +
      '  <p class="reconnect-state__body">' +
      '    Connect your phone to the <strong>LitClock-Setup</strong> hotspot to set it up again.' +
      '  </p>' +
      '</section>';
  }

  function renderPrepareForGiftCard(main) {
    // #316 — terminal handoff copy. The device is paining the welcome
    // splash on the e-ink, wiping WiFi, and powering off. No health-poll
    // is meaningful here — the box is going down for good (until the
    // recipient unboxes + plugs back in).
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="prepare-for-gift">' +
      '  <h2 class="reconnect-state__title"><em>Preparing for gifting…</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    Your welcome message is being painted on the screen. The clock will power off shortly.' +
      '  </p>' +
      '  <p class="reconnect-state__body">' +
      '    <strong>Pack and ship the device.</strong> Plug it back in to test if you want a preview first.' +
      '  </p>' +
      '</section>';
  }

  function renderRetryCard(main) {
    main.innerHTML =
      '<section class="reconnect-state reconnect-state--error" role="status" aria-live="polite">' +
      '  <h2 class="reconnect-state__title">Couldn&rsquo;t reconnect to LitClock.</h2>' +
      '  <button type="button" class="reconnect-state__retry">Tap to retry</button>' +
      '</section>';
    var retry = main.querySelector('.reconnect-state__retry');
    if (retry) {
      retry.addEventListener('click', function () {
        renderRestartingCard(main);
        pollDeadline = Date.now() + RECONNECT_DEADLINE_MS;
        scheduleNextPoll();
      });
    }
  }

  function scheduleNextPoll() {
    pollTimer = window.setTimeout(pollHealth, POLL_INTERVAL_MS);
  }

  function pollHealth() {
    if (Date.now() > pollDeadline) {
      var main = document.querySelector('main');
      if (main) renderRetryCard(main);
      return;
    }

    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timeoutId = ctrl
      ? window.setTimeout(function () { ctrl.abort(); }, POLL_TIMEOUT_MS)
      : null;

    var fetchOpts = ctrl ? { signal: ctrl.signal } : {};

    fetch('/api/health', fetchOpts)
      .then(function (response) {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (!response.ok) throw new Error('not ok');
        // Service is back. Wait one more cycle for unrelated bits (timer,
        // tmpfiles, etc.) to finish coming up, then reload.
        window.setTimeout(function () { window.location.reload(); }, SETTLE_DELAY_MS);
      })
      .catch(function () {
        if (timeoutId) window.clearTimeout(timeoutId);
        scheduleNextPoll();
      });
  }

  // ─── Power off state machine ───────────────────────────────────────────
  //
  // Mirrors pollHealth() but inverted: while health responds, the system is
  // still shutting down. When health fails (network drop), the post-network
  // filesystem-sync window opens — start the safety countdown.

  function renderShuttingDownCard(main) {
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="shutting-down">' +
      '  <h2 class="reconnect-state__title"><em>Shutting down…</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    Don’t unplug yet — services are still stopping.' +
      '  </p>' +
      '</section>';
  }

  function renderSyncingCard(main, secondsLeft) {
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="syncing">' +
      '  <h2 class="reconnect-state__title"><em>Almost done… ' + secondsLeft + 's</em></h2>' +
      '  <p class="reconnect-state__body">' +
      '    Filesystems syncing. Don’t unplug yet.' +
      '  </p>' +
      '</section>';
  }

  function renderSafeToUnplugCard(main) {
    main.innerHTML =
      '<section class="reconnect-state" role="status" aria-live="polite" data-state="safe-to-unplug">' +
      '  <h2 class="reconnect-state__title">Safe to unplug.</h2>' +
      '  <p class="reconnect-state__body">' +
      '    Pull the power cable to turn off. Re-plug to start again.' +
      '  </p>' +
      '</section>';
  }

  function schedulePoweroffPoll() {
    pollTimer = window.setTimeout(pollHealthForPoweroff, POLL_INTERVAL_MS);
  }

  function pollHealthForPoweroff() {
    var main = document.querySelector('main');
    if (!main) return;

    // Belt: if 90s of polling never sees health drop (extremely unlikely
    // on a real shutdown), still open the safety window and proceed —
    // better than leaving the user staring at "Shutting down…" forever.
    if (Date.now() > pollDeadline) {
      startSafetyCountdown(POWEROFF_SAFETY_COUNTDOWN_S);
      return;
    }

    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timeoutId = ctrl
      ? window.setTimeout(function () { ctrl.abort(); }, POLL_TIMEOUT_MS)
      : null;
    var fetchOpts = ctrl ? { signal: ctrl.signal } : {};

    fetch('/api/health', fetchOpts)
      .then(function (response) {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (!response.ok) throw new Error('not ok');
        // Service still up — keep polling. We're still in phase 1.
        schedulePoweroffPoll();
      })
      .catch(function () {
        if (timeoutId) window.clearTimeout(timeoutId);
        // First failed poll = network gone = services stopped. Open the
        // post-network sync safety window and don't re-poll.
        startSafetyCountdown(POWEROFF_SAFETY_COUNTDOWN_S);
      });
  }

  function startSafetyCountdown(secondsLeft) {
    var main = document.querySelector('main');
    if (!main) return;
    if (secondsLeft <= 0) {
      renderSafeToUnplugCard(main);
      return;
    }
    renderSyncingCard(main, secondsLeft);
    window.setTimeout(function () { startSafetyCountdown(secondsLeft - 1); }, 1000);
  }
})();
