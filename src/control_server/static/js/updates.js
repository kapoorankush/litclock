/* Updates tab interactivity (#245 M5 D3, D4, D9, D10).
 *
 * Progressive enhancement: the Apply form posts to /api/update/apply with
 * a hidden confirm token — without JS, the no-JS path goes straight
 * through. With JS, we intercept submit, open the confirm modal, then on
 * confirm POST and overlay the phase reading-list, polling
 * /api/update/status every 2s until it reaches a terminal state.
 *
 * Terminal states (D9):
 *   complete             — version-mismatch reload via A8 health probe
 *   failed_reverted      — render "rolled back, clock is fine" copy
 *   failed_unrecovered   — render "manual recovery needed" copy
 *
 * On `complete`, /api/health returns the new version → window.location.reload().
 * On either failure, the reading-list stops polling and shows terminal copy.
 */

(function () {
  'use strict';

  var STATUS_POLL_INTERVAL_MS = 2000;
  var STATUS_POLL_TIMEOUT_MS = 1000;
  var HEALTH_POLL_INTERVAL_MS = 3000;
  var HEALTH_POLL_TIMEOUT_MS = 1000;
  var SETTLE_DELAY_MS = 1000;
  var RECONNECT_DEADLINE_MS = 90000;

  var card = document.getElementById('updates-card');
  var readingList = document.getElementById('phase-reading-list');
  var terminalMsg = document.getElementById('phase-terminal-message');
  var dialog = document.querySelector('dialog.confirm-sheet[data-action="update_apply"]');
  var form = document.querySelector('form[data-confirm-action="update_apply"]');

  // #329: track whether we've ever seen state=running in this session. When a
  // single /api/update/status poll fails after we've seen running, Phase 7's
  // litclock-control restart is the most likely cause — optimistically tick
  // all 7 phases visually before entering reconnect mode, instead of leaving
  // the user staring at a Phase 4 spinner for 30-60s while the page reloads.
  // Page-load probes (no update in flight) keep their existing behavior so
  // a fresh /updates load doesn't phantom-tick.
  var seenRunning = false;

  // No-op on browsers without <dialog>. Mirrors system.js policy.
  var dialogSupported = dialog && typeof dialog.showModal === 'function';

  // Wire the confirm modal (only if all parts are present).
  if (dialogSupported && form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      openConfirmSheet(dialog);
    });

    var cancelBtn = dialog.querySelector('[data-modal-cancel]');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', function () { dialog.close('cancel'); });
    }
    var confirmBtn = dialog.querySelector('[data-modal-confirm]');
    if (confirmBtn) {
      confirmBtn.addEventListener('click', function () {
        dialog.close('confirm');
        fireApply();
      });
    }
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) dialog.close('cancel');
    });
    // #305: strip the `.is-opening` class on close so re-opens re-trigger
    // the slide-up keyframe. Without this the keyframe only fires once.
    //
    // #354 codex P2 follow-up — if the page-load probe arrived while the
    // confirm sheet was open, its payload was deferred (the modal guard
    // dropped the only cold-load sample). On CANCEL, replay through
    // handleProbePayload so the page transitions to the running / failed
    // reading-list it should have rendered.
    //
    // On CONFIRM, leave the stash alone — fireApply handles it: on 2xx
    // (apply succeeded) fireApply clears the stash because it owns the
    // new running state; on non-OK (e.g., 409 concurrent update — another
    // tab or auto-update is already running the very state the deferred
    // probe captured) fireApply REPLAYS the stash before alerting, so
    // the user gets the in-flight reading-list instead of a stale card.
    // Without that path the codex-found edge fires: user sees an alert,
    // page sits on stale card, no follow-up polling, manual reload only.
    dialog.addEventListener('close', function () {
      dialog.classList.remove('is-opening');
      if (deferredProbePayload && dialog.returnValue !== 'confirm') {
        var stashed = deferredProbePayload;
        deferredProbePayload = null;
        handleProbePayload(stashed);
      }
    });
  }

  // #354 codex P2 — stash for a probe payload that landed while the
  // confirm sheet was open. Replayed on modal close (cancel path); cleared
  // without replay on confirm (fireApply takes over). null when no probe
  // is pending.
  var deferredProbePayload = null;

  // #305: see system.js openConfirmSheet for the full doc — iOS Safari
  // pre-17.5 doesn't fire keyframes gated on `[open]` because top-layer
  // promotion + display flip happen in the same paint as the attribute
  // toggle. Adding `.is-opening` after two rAFs lets the keyframe observe
  // its `from` state. Mirrored verbatim across system.js + updates.js so
  // each tab's bundle is self-contained. Same defensive guards: strip
  // stale `.is-opening` before showModal, check `d.open` before adding.
  function openConfirmSheet(d) {
    d.classList.remove('is-opening');
    d.showModal();
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (d.open) {
          d.classList.add('is-opening');
        }
      });
    });
  }

  // Auto-refresh the cached check on page load — the server-rendered card
  // shows whatever the cache had at render time, which can be stale up to
  // 6h. /api/update/check returns a fresh value if the cache TTL has
  // expired, otherwise it serves the same cached payload (cheap).
  refreshCheck();

  // If the user navigates back to /updates while an update is mid-flight
  // (left the tab during apply), bring the reading-list up immediately.
  //
  // #342 #345 — the probe must NOT clobber a reading-list that fireApply
  // has already entered. The fetch can sit in flight 3-5s on a slow LAN;
  // if it lands AFTER the user tapped Apply and the first scheduled poll
  // already advanced the visual to phase 2+, replaying enterReadingList
  // with the stale snapshot regresses the display backward one tick.
  // Guard on readingList.hidden — true only when no reading list is
  // showing yet, so the probe can safely take over on a true cold load.
  //
  // #342 I10 — when the probe DOES legitimately enter the reading-list
  // mid-update (cold load while an update is in flight), it must also
  // arm `seenRunning` AND start `schedulePoll`. Pre-fix the probe only
  // called enterReadingList — no follow-up polls fired, so the reading
  // list froze at whatever phase the snapshot showed and the user
  // watched a stuck spinner until they reloaded.
  pollStatusOnce(handleProbePayload);

  function handleProbePayload(payload) {
    if (!payload) return;
    if (!readingList || !readingList.hidden) return;  // #345 — don't race fireApply
    // #354 Race 2 + Race 3 — if the user has the confirm modal open when this
    // probe lands (3-5s slow-LAN window), DEFER for ANY state transition.
    // The guard must live ABOVE the state switch so it covers BOTH the
    // failed_* path (Race 2: stale failed_* snapshot yanks card surface to
    // terminal copy mid-confirm) AND the running path (Race 3: an auto-update
    // weekly timer fire or sibling-tab apply lands `running` mid-confirm,
    // enterReadingList yanks the card surface to the reading list, and the
    // user confirms against a UI for a DIFFERENT in-flight update — their
    // POST then returns 409). Both paths break the modal's "stage and
    // confirm against THIS card" contract identically.
    //
    // #354 codex P2 follow-up — DEFER the payload instead of dropping it.
    // The probe is one-shot; if we discard the only cold-load sample and
    // the user cancels the modal, the page stays on the stale card until
    // manual reload. Stash now; the dialog close listener replays through
    // this same function on cancel.
    if (dialog && dialog.open) {
      deferredProbePayload = payload;
      return;
    }
    if (payload.state === 'running') {
      seenRunning = true;                              // #342 I10 — arm phantom-tick + reconnect-mode
      enterReadingList(payload);
      schedulePoll();                                  // #342 I10 — keep advancing
    } else if (payload.state === 'failed_reverted' || payload.state === 'failed_unrecovered') {
      enterReadingList(payload);
      // #352 — also render the terminal copy on cold load. Without this,
      // a user who navigates to /updates AFTER a failed update sees a
      // frozen phase reading-list with no banner — looks like a stuck
      // in-flight update instead of a finished failure. Mirror the exact
      // strings + tones used by handleStatusPayload's terminal branches.
      updateRowStates(payload.phase_index || (payload.state === 'failed_reverted' ? 5 : 0), true);
      if (payload.state === 'failed_unrecovered') {
        showTerminal(
          payload.error || 'Update did not finish. Try again in a few minutes; if it still fails, restart from the System tab.',
          'error'
        );
      } else {
        showTerminal(
          payload.error || 'Update failed verification — rolled back. Your clock is running normally.',
          'reverted'
        );
      }
    }
  }

  function fireApply() {
    if (!form) return;
    var tokenInput = form.querySelector('input[name="token"]');
    if (!tokenInput || !tokenInput.value) {
      window.alert('Confirm token missing. Reload the page and try again.');
      return;
    }
    fetch(form.action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ token: tokenInput.value })
    })
      .then(function (response) {
        if (response.ok) {
          // 202 Accepted — kick the reading list. First /api/update/status
          // poll fires after the standard interval; the server-side
          // status file is already populated by Phase 1's update_status_set_phase 1.
          // #329 (review C2): arm seenRunning at user-action time so the
          // optimistic-tick branch fires even if the very first scheduled
          // poll times out (e.g. Phase 7 restart racing the 2s interval).
          seenRunning = true;
          // #354 codex P2 follow-up — fireApply owns the new running
          // state on success; the deferred probe (if any) is now stale.
          deferredProbePayload = null;
          enterReadingList({ state: 'running', phase_index: 1 });
          schedulePoll();
          return;
        }
        // #354 codex P2 follow-up — non-OK (typically 409 concurrent
        // update) means an update is ALREADY running from another tab or
        // an auto-update timer. If we deferred a probe payload while the
        // modal was open, it captured exactly that state — replay it so
        // the page transitions to the in-flight reading-list instead of
        // sitting on a stale card after the alert.
        if (deferredProbePayload) {
          var stashed = deferredProbePayload;
          deferredProbePayload = null;
          handleProbePayload(stashed);
        }
        return response.json().then(function (body) {
          var msg = (body && body.error && body.error.message)
            ? body.error.message
            : 'Request failed (HTTP ' + response.status + ').';
          window.alert(msg);
        }).catch(function () {
          window.alert('Request failed (HTTP ' + response.status + ').');
        });
      })
      .catch(function () {
        // Network glitch (rare on LAN). Try once more by entering the
        // reading-list optimistically; if no update is actually running,
        // the first poll will report state=idle and we exit cleanly.
        // #329 (review C2): arm seenRunning here too — the user clicked
        // Apply, so we're in a "running" intent for the duration regardless
        // of whether the POST round-tripped cleanly.
        seenRunning = true;
        // #354 codex P2 follow-up — optimistic enterReadingList here
        // claims the running state; drop any deferred probe so it can't
        // overwrite the optimistic phase_index when the close listener
        // would otherwise have replayed.
        deferredProbePayload = null;
        enterReadingList({ state: 'running', phase_index: 1 });
        schedulePoll();
      });
  }

  function refreshCheck() {
    fetch('/api/update/check', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body || !body.ok) return;
        // If the freshly-fetched check disagrees with the server-rendered
        // card, reload so the no-JS path-rendered HTML stays the source
        // of truth (cheap on LAN, avoids any DOM-rebuild logic here).
        var renderedState = card && card.dataset.state;
        var freshState = body.available ? 'available'
                       : (body.available === null ? 'unknown' : 'up_to_date');
        // #381 codex post-review fix: when we rendered as 'unknown' (i.e.,
        // initial-load "checking…" with no cache) AND the API confirms
        // available === null (terminal unknown — typically private repo +
        // no PAT), the data-state values match so the equality check
        // below skips the reload. The substate changed, but reloading
        // here would create an infinite loop: after the reload, the
        // server reads the now-populated cache, finds available=null,
        // and renders data-state="unknown" again → JS re-enters this
        // branch → reload → etc. Hardware QA on 2026-05-20 surfaced this
        // as scroll jank + un-tappable tabs on mobile (pull-to-refresh
        // triggers the reload loop). In-place DOM update instead — change
        // the pill text from "checking…" to "couldn't check". Idempotent:
        // on subsequent visits where the server already rendered
        // "couldn't check" from cache, the textContent check is false
        // and this is a no-op.
        if (renderedState === 'unknown' && freshState === 'unknown') {
          var pill = card && card.querySelector('.updates-pill--unknown');
          if (pill) {
            var currentLabel = (pill.textContent || '').trim().toLowerCase();
            if (currentLabel.indexOf('checking') === 0) {
              pill.textContent = "couldn't check";
            }
          }
          return;
        }
        if (renderedState && renderedState !== freshState) {
          window.location.reload();
        }
      })
      .catch(function () { /* swallow — non-fatal */ });
  }

  // ─── Status polling ────────────────────────────────────────────────

  // #348 + codex adversarial review (findings 1+2) — the polling state machine
  // has TWO scheduled units that can each re-arm the cycle:
  //
  //   1. A pending setTimeout (pollTimer)
  //   2. An in-flight pollStatusOnce fetch whose .then/.catch lands later
  //      and calls handleStatusPayload → schedulePoll (or enterReconnectMode)
  //
  // The original #348 sentinel only covered (1). A delayed `fireApply.catch()`
  // that lands while a poll is in flight slips past `if (pollTimer)` (it's
  // null mid-fetch) and arms a NEW timer. The original in-flight fetch then
  // resolves and may transition to a terminal/reconnect state, which doesn't
  // clear the new timer — that timer fires AFTER terminal state, re-entering
  // handleStatusPayload → re-entering reconnect mode → two parallel pollHealth
  // loops competing for /api/health.
  //
  // Fix: a monotonically-incrementing `pollGeneration` counter. Every armed
  // unit (timer callback, fetch callback) captures the generation at arming
  // time; cancelPolling() bumps the counter so any captured-stale callback
  // checks `gen !== pollGeneration` and fails closed. Terminal branches and
  // reconnect-mode entry call cancelPolling() to invalidate every in-flight
  // unit at once, and enterReconnectMode is idempotence-guarded so a
  // late-arriving callback that survives the generation check (impossible by
  // construction, but defense-in-depth) still can't fork pollHealth.
  var pollTimer = null;
  var pollGeneration = 0;
  var reconnectArmed = false;

  function cancelPolling() {
    // Bump the generation so any captured-by-closure callback (pending
    // timer, in-flight fetch) sees `gen !== pollGeneration` and short-
    // circuits. Also clear the pending timer outright so it never fires.
    pollGeneration++;
    if (pollTimer) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function pollStatusOnce(cb) {
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timeoutId = ctrl ? window.setTimeout(function () { ctrl.abort(); }, STATUS_POLL_TIMEOUT_MS) : null;
    var fetchOpts = ctrl ? { signal: ctrl.signal } : {};
    fetch('/api/update/status', fetchOpts)
      .then(function (r) {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (!r.ok) throw new Error('not ok');
        return r.json();
      })
      .then(function (body) {
        if (cb) cb(body);
      })
      .catch(function () {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (cb) cb(null);
      });
  }

  function schedulePoll() {
    // #348 + codex finding 1 — guard against double-arming. The pollTimer
    // check coalesces two callers in the same 2s window (page-load probe +
    // fireApply.catch on a transient network error). The generation-counter
    // mechanism (see cancelPolling) handles the harder case where a stale
    // callback lands after terminal/reconnect and tries to re-arm.
    if (pollTimer) return;
    var gen = pollGeneration;
    pollTimer = window.setTimeout(function () {
      if (gen !== pollGeneration) return;  // generation expired — terminal/reconnect entered
      pollTimer = null;
      pollStatusOnce(function (payload) {
        if (gen !== pollGeneration) return;  // generation expired mid-fetch
        handleStatusPayload(payload);
      });
    }, STATUS_POLL_INTERVAL_MS);
  }

  function handleStatusPayload(payload) {
    if (!payload) {
      // Network failure during apply — control_server is probably mid-
      // restart in Phase 7. Switch to health-poll mode.
      // #329: if we've previously seen state=running this session, the
      // failed poll is almost certainly the Phase 7 systemctl restart of
      // litclock-control killing waitress mid-fetch. Tick all 7 phases
      // visually before reconnecting so the user doesn't stare at the
      // mid-update spinner while /api/health races to detect the new
      // version. The reload follows naturally within ~3-5s.
      // Page-load probes (seenRunning=false) keep existing behavior — no
      // phantom tick when there's no update in flight.
      if (seenRunning) {
        updateRowStates(7, false);
      }
      // #348 codex finding 1+2 — invalidate any pending/in-flight poll
      // before transitioning out of the status-polling loop, so a stale
      // callback can't fork a competing pollHealth cycle.
      cancelPolling();
      enterReconnectMode();
      return;
    }
    if (payload.state === 'idle') {
      // Either no update has run, or one finished and the file was
      // wiped. Restore the card view.
      cancelPolling();
      exitReadingList();
      return;
    }
    if (payload.state === 'running') {
      seenRunning = true;
      updateRowStates(payload.phase_index || 0, false);
      schedulePoll();
      return;
    }
    if (payload.state === 'complete') {
      updateRowStates(7, false);
      // A8 — version-mismatch reload. Wait for /api/health to report
      // a different version than the card's data-current-version.
      // #348 codex finding 1+2 — terminal state must cancel any in-flight
      // poll so it can't re-enter handleStatusPayload after reconnect arms.
      cancelPolling();
      enterReconnectMode();
      return;
    }
    if (payload.state === 'failed_reverted') {
      updateRowStates(payload.phase_index || 5, true);
      // #348 codex finding 1+2 — terminal state cancels pending work.
      cancelPolling();
      showTerminal(
        payload.error || 'Update failed verification — rolled back. Your clock is running normally.',
        'reverted'
      );
      return;
    }
    if (payload.state === 'failed_unrecovered') {
      updateRowStates(payload.phase_index || 0, true);
      // #348 codex finding 1+2 — terminal state cancels pending work.
      cancelPolling();
      showTerminal(
        payload.error || 'Update did not finish. Try again in a few minutes; if it still fails, restart from the System tab.',
        'error'
      );
      return;
    }
    if (payload.state === 'stale') {
      // Status file existed but couldn't be parsed. Treat as transient
      // and keep polling — atomic mv-tmp writes should make this
      // self-correct within one tick.
      schedulePoll();
      return;
    }
    // Unknown state — keep polling.
    schedulePoll();
  }

  function enterReadingList(payload) {
    if (!readingList) return;
    // #354 Race 1 — clear any stale terminal banner from a prior failed run
    // when starting fresh. Sequence: prior run left `failed_*` copy in
    // terminalMsg (hidden underneath the hidden reading list — the DOM
    // node persists; only the visible card surface is restored on idle).
    // User taps Apply → fireApply.then calls
    // enterReadingList({state:'running', phase_index:1}). Without this
    // clear, the OLD failure banner sits inside the freshly-revealed
    // reading list (#345's existing `readingList.hidden` probe guard
    // blocks the cold-load probe from clobbering this entry path, so
    // the banner can only come from in-DOM residue, not a racing probe).
    // exitReadingList already clears the banner on idle transitions;
    // mirror that clear here for the running-entry path so the new
    // reading list paints clean.
    if (payload && payload.state === 'running' && terminalMsg) {
      terminalMsg.hidden = true;
      terminalMsg.textContent = '';
      delete terminalMsg.dataset.tone;
    }
    if (card) card.hidden = true;
    readingList.hidden = false;
    if (payload && typeof payload.phase_index === 'number') {
      updateRowStates(payload.phase_index, false);
    }
  }

  function exitReadingList() {
    if (readingList) readingList.hidden = true;
    if (terminalMsg) {
      terminalMsg.hidden = true;
      terminalMsg.textContent = '';
      delete terminalMsg.dataset.tone;
    }
    if (card) card.hidden = false;
  }

  function updateRowStates(activeIndex, failed) {
    var rows = document.querySelectorAll('.phase-row');
    Array.prototype.forEach.call(rows, function (row) {
      var idx = parseInt(row.getAttribute('data-phase-index'), 10);
      if (failed && idx === activeIndex) {
        row.setAttribute('data-state', 'failed');
      } else if (idx < activeIndex) {
        row.setAttribute('data-state', 'completed');
      } else if (idx === activeIndex && !failed) {
        row.setAttribute('data-state', 'active');
      } else {
        row.setAttribute('data-state', 'upcoming');
      }
    });
  }

  function showTerminal(message, tone) {
    if (!terminalMsg) return;
    terminalMsg.textContent = message;
    if (tone) {
      terminalMsg.dataset.tone = tone;
    } else {
      delete terminalMsg.dataset.tone;
    }
    terminalMsg.hidden = false;
  }

  // ─── Reconnect mode (mirrors system.js A8 reconnect probe) ─────────

  function enterReconnectMode() {
    // #348 codex finding 2 — idempotence guard. Without this, a stray late
    // status-poll callback that survives the generation check (defense-in-
    // depth: shouldn't happen by construction, but cheap belt-and-suspenders)
    // could call enterReconnectMode a second time → fork a parallel
    // pollHealth loop racing the first one against /api/health.
    if (reconnectArmed) return;
    reconnectArmed = true;
    // Also bump generation so any in-flight status fetch that lands AFTER
    // reconnect arms (and slipped past the gen check at fetch dispatch)
    // can't re-enter handleStatusPayload. cancelPolling is idempotent —
    // callers (terminal branches in handleStatusPayload) already invoked
    // it, but calling again is harmless and pins the invariant locally.
    cancelPolling();
    var deadline = Date.now() + RECONNECT_DEADLINE_MS;
    var currentVersion = card ? card.dataset.currentVersion : null;
    pollHealth(deadline, currentVersion);
  }

  function pollHealth(deadline, currentVersion) {
    if (Date.now() > deadline) {
      showTerminal(
        'Couldn’t reconnect. Check your WiFi and refresh this page.',
        'error'
      );
      return;
    }
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timeoutId = ctrl ? window.setTimeout(function () { ctrl.abort(); }, HEALTH_POLL_TIMEOUT_MS) : null;
    var fetchOpts = ctrl ? { signal: ctrl.signal } : {};
    fetch('/api/health', fetchOpts)
      .then(function (r) {
        if (timeoutId) window.clearTimeout(timeoutId);
        if (!r.ok) throw new Error('not ok');
        return r.json();
      })
      .then(function (body) {
        // Version mismatch (or first response post-restart) → reload.
        if (body && body.version && body.version !== currentVersion) {
          window.setTimeout(function () { window.location.reload(); }, SETTLE_DELAY_MS);
          return;
        }
        // Same version still — service hadn't restarted yet OR the update
        // went through with no version change (same SHA timer fire).
        // Either way, give it another beat.
        window.setTimeout(function () { pollHealth(deadline, currentVersion); }, HEALTH_POLL_INTERVAL_MS);
      })
      .catch(function () {
        if (timeoutId) window.clearTimeout(timeoutId);
        window.setTimeout(function () { pollHealth(deadline, currentVersion); }, HEALTH_POLL_INTERVAL_MS);
      });
  }
})();
