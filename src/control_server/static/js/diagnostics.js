/* Diagnostics page (#416 PR3a) — auto-refresh + Reveal toggle + copy.
 *
 * Design decisions: see issue #416 /plan-design-review (D1–D33 IDs).
 *
 * Server-side rendering carries the full first paint (status banner +
 * sections + anomalies + copy_payload) so the page is usable without JS.
 * This module is the progressive-enhancement layer:
 *
 *   D12 — 30s poll: refetch /api/diagnostics, patch values + section
 *         pills + anomaly open-state. <details> open/closed state is
 *         preserved across polls so the user's manual interaction wins
 *         over server-side default-open. Scroll position preserved by
 *         not removing/recreating DOM nodes. Pauses on document.hidden.
 *
 *   D16 — Reveal pill is session-scoped (sessionStorage). On toggle:
 *         re-fetch /api/diagnostics with ?reveal=location, patch values
 *         dict + copy_payload + aria-pressed.
 *
 *   D10 — Per-row .diag-log-entry Copy — focusable button copies the
 *         formatted line to the clipboard; polite live-region "Copied"
 *         announcement.
 *
 *   D18 — Bottom copy-payload card: full markdown block to clipboard;
 *         same announce mechanism.
 *
 *   D29 — Banner anomaly state uses the locked copy "Clock isn't running"
 *         + --error oxblood triangle when a services/recent-log anomaly
 *         fires; softer "Something needs attention" + --warning for the
 *         remaining anomaly classes. (Per /review design specialist:
 *         locked copy from /plan-design-review.)
 *
 * Patch strategy: textContent / [hidden] / [open] / aria-pressed on
 * data-diag-* hooks. Never innerHTML — XSS defense for any future field
 * that might leak HTML. The services + recent-log section rebuilders use
 * createElement + textContent for the same reason.
 *
 * /review fixes folded in (multi-source CRITICAL):
 *   F-OPEN-RACE   — JS-driven sectionEl.open=X fires the toggle event,
 *                   which my pre-fix listener marked as user-touched and
 *                   broke D3 default-open after the first poll-driven
 *                   transition. Fixed via _suppressToggleTracking flag.
 *   F-REVEAL-RACE — Reveal-toggle-mid-fetch let the stale revealed
 *                   response patch sensitive data while UI said
 *                   "redacted". Fixed via generation token + reveal-
 *                   state guard at response time.
 *   F-SERVICES-STALE — Services + recent_log_entries weren't patched on
 *                   poll. Banner flipped but rows stayed at SSR. Fixed
 *                   via dedicated rebuilders.
 *   F-FAILURE-MASKED — Pre-fix the "failed" attribute was cleared in
 *                   the always-runs .then(), masking persistent failures.
 *                   Fixed via consecutiveFailures counter.
 *   F-LISTENER-STACK — Production code now exports a teardown hook on
 *                   window.__litclockDiag for vitest to call between
 *                   tests, preventing stacked document listeners.
 */

(function () {
  'use strict';

  var POLL_INTERVAL_MS = 30000;
  /* #432 D8 — debounce window for ok → uncollected (reverse) transitions.
     A real-world flapping case: tmpfs marker briefly missing during a
     dispatcher edit, or weather_location_name briefly cleared mid-edit
     through Settings. Within 60s of a forward transition for the same
     section, the visual update is suppressed for one poll cycle so the
     pill doesn't flicker grey on a transient. */
  var UNCOLLECTED_REVERSE_DEBOUNCE_MS = 60000;
  /* #432 D11 — N consecutive poll failures trip the page-level "stale"
     state (60% pill opacity). Match the existing meta-failed UX (which
     fires after 1 failure) but only one notch later so a single transient
     failure doesn't dim every pill. */
  var POLL_STALE_FAILURES = 2;
  /* #429: client fetch budget. Bumped from 5 s to 10 s to give the
     server's DIAG_JOURNAL_TIMEOUT_S (8 s) full headroom under Pi Zero 2W
     IO contention. The pre-bump 5 s client < 8 s server gap meant a
     genuinely-slow journalctl call that the server WAS going to satisfy
     (just slowly) showed as "couldn't refresh" to the user, even though
     the cache populated with the success result. 10 s puts the client's
     abort budget above the server's worst case + ~2 s slack for waitress
     scheduling. The trade is: a wedged endpoint now hangs the spinner
     for up to 10 s before the user sees the failure UI (vs 5 s pre-PR),
     but PR1b's failure-TTL cap (5 s) means a second poll comes through
     within the abort window anyway. */
  var FETCH_TIMEOUT_MS = 10000;
  var REVEAL_STORAGE_KEY = 'litclock.diag.reveal-location';
  var REFRESH_HINT_MS = 2000;
  var COPY_FLASH_MS = 1500;
  var ANNOUNCE_RESET_MS = 16;
  var META_TICKER_MS = 5000;

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }
  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  /* ----- Reveal pill (D16) ---------------------------------------------- */

  function readReveal() {
    try {
      return window.sessionStorage.getItem(REVEAL_STORAGE_KEY) === '1';
    } catch (e) {
      return false;
    }
  }
  function writeReveal(on) {
    try {
      if (on) {
        window.sessionStorage.setItem(REVEAL_STORAGE_KEY, '1');
      } else {
        window.sessionStorage.removeItem(REVEAL_STORAGE_KEY);
      }
    } catch (e) {
      /* sessionStorage unavailable — Reveal stays in-memory only. */
    }
  }

  function applyRevealUI(on) {
    var btn = $('[data-diag-reveal]');
    if (btn) {
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      var off = btn.querySelector('.reveal-pill__icon--off');
      var onIcon = btn.querySelector('.reveal-pill__icon--on');
      if (off) off.hidden = on;
      if (onIcon) onIcon.hidden = !on;
      var label = $('[data-diag-reveal-label]');
      if (label) label.textContent = on ? 'Hide' : 'Reveal';
    }
    var copyState = $('[data-diag-copy-reveal-state]');
    if (copyState) copyState.textContent = on ? 'visible' : 'redacted';
  }

  /* ----- Fetch with timeout --------------------------------------------- */

  /* #435: ``externalController`` lets the caller (a refresh handle)
     attach a controller it can also call ``.abort()`` on. When provided,
     this function still arms the FETCH_TIMEOUT_MS auto-abort against
     the SAME controller — so an external abort and a timeout abort
     share signal state. When omitted, behaviour matches pre-#435: a
     fresh per-call controller bounded by FETCH_TIMEOUT_MS.

     /review on PR #440 found a critical bug: the catch handler in
     makeRefresher swallows AbortError indiscriminately, treating both
     user-cancels and OUR-OWN timeouts as "no failure." Without
     distinguishing, a wedged endpoint hitting the 10 s timeout would
     silently show stale data with NO "couldn't refresh" UI — strictly
     worse than pre-PR. The ``timedOut`` flag lets us re-throw the
     timeout as a TimeoutError so the caller's catch hits the failure
     path correctly. User-intended aborts (caller's abort() method)
     keep their AbortError shape and the caller skips the failure. */
  function fetchDiagnostics(reveal, externalController) {
    var url = '/api/diagnostics' + (reveal ? '?reveal=location' : '');
    var controller = externalController
      || ((typeof AbortController !== 'undefined') ? new AbortController() : null);
    var timedOut = false;
    var t = controller ? setTimeout(function () {
      timedOut = true;
      controller.abort();
    }, FETCH_TIMEOUT_MS) : null;
    return fetch(url, { credentials: 'same-origin', signal: controller ? controller.signal : undefined })
      .then(function (r) {
        if (t) clearTimeout(t);
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .catch(function (err) {
        if (t) clearTimeout(t);
        if (timedOut && err && err.name === 'AbortError') {
          var te = new Error('fetch timeout');
          te.name = 'TimeoutError';
          throw te;
        }
        throw err;
      });
  }

  /* ----- DOM patchers --------------------------------------------------- */

  function renderValue(value) {
    /* Mirror Jinja's render rules: None / "" → em-dash; bool → Yes/No;
       float → trim trailing .0 for integer-valued floats (so cpu_temp_c
       50.0 matches the SSR "50.0" rendering); anything else → str(value). */
    if (value === null || value === undefined || value === '') return '—';
    if (value === true) return 'Yes';
    if (value === false) return 'No';
    if (typeof value === 'number') {
      /* Match Jinja's str() on floats: 50.0 → "50.0" (not "50"). */
      if (Number.isFinite(value) && !Number.isInteger(value)) {
        return String(value);
      }
      /* Integer-valued floats land here too; preserve int shape. */
      return String(value);
    }
    return String(value);
  }

  /* F-OPEN-RACE: every JS-driven `sectionEl.open = X` queues a toggle
     event (sync in jsdom, async in real browsers via "queue a task" per
     HTML spec). Without distinguishing, the toggle listener can't tell a
     poll-driven transition from a user click, and `userInteractedSections`
     gets set on the first JS flip — silently breaking D3 default-open
     for the rest of the session.

     The fix: maintain a per-section "JS-driven flip in flight" marker.
     The toggle handler clears the marker and returns early when it sees
     one; only un-marked toggles count as user interaction. This works
     whether the toggle event fires sync (jsdom) or async (browser): the
     marker outlives both paths. */
  var _jsOpenInFlight = Object.create(null);

  /* #432 — pill labels for the tri-state. Anomaly labels live in the
     macro caller (not JS-rebuilt) because each section's anomaly_label
     is distinct ("Resource alert" / "Connection issue" / "Location
     stale" / etc.); the OK + Not yet collected labels are constant
     across sections so JS can write them directly. */
  var PILL_LABEL_OK = 'OK';
  var PILL_LABEL_UNCOLLECTED = 'Not yet collected';

  /* #432 D6 — section-aware "settling" banner body lookup. Shared between
     SSR Jinja (in diagnostics.html.j2) and the 30s poll handler so the
     two never disagree. Order-independent: caller normalises by sort()
     so ["time-location","network"] hashes to the same key as the reverse.
     Hand-rolled if/else (not a literal object) because IE-class WebViews
     in the wild still trip on computed property keys with hyphens — and
     the three-line ladder is no less maintainable. */
  function _settlingBody(uncollected) {
    if (!uncollected || uncollected.length === 0) return '';
    var key = uncollected.slice().sort().join('+');
    if (key === 'network') return 'Your clock is finishing its first network check.';
    if (key === 'time-location') return 'Your clock is finishing its first location check.';
    if (key === 'network+time-location') {
      return 'Your clock is finishing its first network and location checks.';
    }
    /* Unknown section ID (forward-compat) — return empty so the banner
       renders the headline alone rather than wrong copy. Graceful per
       the plan's failure-modes table. */
    return '';
  }

  /* #432 — last-forward-transition timestamps per section ID for D8
     debounce. Resets on teardown via the lifecycle so vitest re-entry
     between tests doesn't leak between cases.

     Adversarial-review F4: timestamps come from _monotonicNow() so an
     NTP step adjustment (common on fresh-flash Pis right after first
     WiFi join) doesn't blow up the debounce window. performance.now()
     is monotonic; falls back to Date.now() in jsdom/old WebViews. */
  var _uncollectedForwardAt = Object.create(null);

  function _monotonicNow() {
    if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
      return performance.now();
    }
    return Date.now();
  }

  /* #432 D11 — page-level poll-stale flag on the sections container.
     Driven by the consecutive-failure counter in the refresh loop. */
  function _setPollStale(on) {
    var container = $('[data-diag-sections]');
    if (!container) return;
    if (on) container.setAttribute('data-poll-stale', 'true');
    else container.removeAttribute('data-poll-stale');
  }

  /* #432 D2 — helper consumed by the poll handler. Defaults `uncollected`
     to [] so a stale cached diagnostics.js reading an old payload doesn't
     crash on a missing field (graceful per Codex finding 7). */
  function getSectionStates(payload) {
    return {
      anomalies: (payload && payload.anomalies) || [],
      uncollected: (payload && payload.uncollected) || []
    };
  }

  /* #432 — apply tri-state pill class + label per the truth table.
     Server already applied uncollected-wins precedence in
     _compute_section_states, so a section ID never appears in BOTH
     `anomalies` and `uncollected` at this point — the checks below are
     mutually exclusive. Returns the section's logical state, consumed
     by the SR announcer to fire "Network details available." on a
     uncollected → ok transition. */
  function _stateOf(sectionId, anomalies, uncollected) {
    if (anomalies.indexOf(sectionId) !== -1) return 'anomaly';
    if (uncollected.indexOf(sectionId) !== -1) return 'uncollected';
    return 'ok';
  }

  /* F2 fix — when both network AND time-location flip uncollected → ok in
     the same poll cycle, announce ONE batched message ("Network and
     location details available.") instead of two announce() calls in
     quick succession. The naive per-section announce() races: each call
     does node.textContent='' then setTimeout(16ms) which means the second
     announce overwrites the first before any AT picks it up. The poll
     handler collects forward-transition section IDs and flushes once at
     the end. */
  function _announceForwardTransitions(ids) {
    if (!ids || ids.length === 0) return;
    var net = ids.indexOf('network') !== -1;
    var loc = ids.indexOf('time-location') !== -1;
    if (net && loc) announce('Network and location details available.');
    else if (net) announce('Network details available.');
    else if (loc) announce('Location details available.');
  }

  /* Fix B — patchSection now returns the section's EFFECTIVE post-debounce
     state ('ok' | 'uncollected' | 'anomaly') so the caller can rebuild
     a debounce-aware `uncollected` list for patchBanner. Pre-fix the
     banner consumed raw `state.uncollected` straight from the server,
     so during a debounce-suppressed flap the pill stayed green while
     the banner flipped to "Just settling in." — internally contradictory
     UI on the exact tmpfs-marker flap window the debounce was meant
     to smooth. */
  function patchSection(sectionEl, sectionId, anomalies, uncollected, valuesDict, userInteractedSections, priorStates, forwardTransitions) {
    var newState = _stateOf(sectionId, anomalies, uncollected);
    var oldState = priorStates[sectionId];

    /* D8 reverse-transition debounce: ok → uncollected within 60s of the
       last forward transition (uncollected → ok) is suppressed for one
       poll cycle. The store key is the section ID; the timestamp is
       overwritten on each forward transition. A real reverse-transition
       after the 60s window passes the time-window check below and lands
       normally.

       Adversarial-review F4 (#432): the timestamps use performance.now()
       (monotonic) instead of Date.now() because fresh-flash Pis routinely
       step the wall clock by minutes-to-hours on first NTP sync — exactly
       the timing window the grey tier was designed for. A wall-clock
       forward jump would prematurely expire every active debounce window
       (visible flicker); a backward jump would stretch it. performance.now()
       isn't affected by clock adjustments. */
    if (oldState === 'ok' && newState === 'uncollected') {
      var lastForward = _uncollectedForwardAt[sectionId];
      if (lastForward && (_monotonicNow() - lastForward) < UNCOLLECTED_REVERSE_DEBOUNCE_MS) {
        /* Suppress this poll — paint the prior state again. */
        newState = oldState;
      }
    }

    /* D8 forward transition emit: record the timestamp + queue the SR
       announcement so the caller can BATCH compound transitions at end
       of poll (F2 fix — see _flushTransitions). */
    if (oldState === 'uncollected' && newState === 'ok') {
      _uncollectedForwardAt[sectionId] = _monotonicNow();
      if (forwardTransitions && (sectionId === 'network' || sectionId === 'time-location')) {
        forwardTransitions.push(sectionId);
      }
      /* ok → uncollected is intentionally silent per D9. */
    }

    priorStates[sectionId] = newState;

    if (!userInteractedSections[sectionId]) {
      /* Only anomaly sections auto-open; uncollected stays closed by
         default (D3 — the empty placeholder is the at-a-glance signal). */
      var shouldBeOpen = newState === 'anomaly';
      if (sectionEl.open !== shouldBeOpen) {
        _jsOpenInFlight[sectionId] = true;
        sectionEl.open = shouldBeOpen;
      }
    }
    if (newState === 'anomaly') {
      sectionEl.classList.add('diag-section--anomalous');
    } else {
      sectionEl.classList.remove('diag-section--anomalous');
    }
    var pill = sectionEl.querySelector('[data-diag-section-pill]');
    if (pill) {
      pill.classList.toggle('diag-section__pill--ok', newState === 'ok');
      pill.classList.toggle('diag-section__pill--warning', newState === 'anomaly');
      pill.classList.toggle('diag-section__pill--muted', newState === 'uncollected');
      if (newState === 'uncollected') {
        pill.setAttribute(
          'aria-label',
          'Not yet collected — data has not been recorded on this clock yet'
        );
      } else {
        pill.removeAttribute('aria-label');
      }
      var pillLabel = pill.querySelector('.diag-section__pill-label');
      if (pillLabel) {
        if (newState === 'ok') pillLabel.textContent = PILL_LABEL_OK;
        else if (newState === 'uncollected') pillLabel.textContent = PILL_LABEL_UNCOLLECTED;
        else {
          /* Fix D — anomaly: read the section-specific label from the
             pill's data-diag-anomaly-label attribute (SSR-rendered). Pre-
             fix the comment claimed "keep from SSR" but that was a lie if
             SSR rendered ok/uncollected and the poll flipped to anomaly:
             the warning-ochre pill kept reading "OK" or "Not yet
             collected", masking a real failure. */
          var anomalyLabel = pill.getAttribute('data-diag-anomaly-label')
            || 'Needs attention';
          pillLabel.textContent = anomalyLabel;
        }
      }
    }
    /* #432 D1/D2 — swap the visible row content on transition. Both the
       dl AND the placeholder are always rendered in SSR (template) when a
       section has an uncollected_placeholder; one carries `hidden` at
       request time. Here we just flip the attribute so the visible body
       matches the pill. Without this swap the pill could read "OK" while
       the placeholder copy "Network details fill in once your clock sees
       a network event." stayed on screen — contradicting the SR
       announcer's "Network details available." */
    var dlEl = sectionEl.querySelector('[data-diag-rows]');
    var placeholderEl = sectionEl.querySelector('[data-diag-rows-placeholder]');
    if (dlEl) dlEl.hidden = (newState === 'uncollected');
    if (placeholderEl) placeholderEl.hidden = (newState !== 'uncollected');

    /* Patch raw values into the dl rows so the section's data stays in
       sync regardless of pill state. The forEach is a no-op when the dl
       is hidden (uncollected state) — cell.textContent assignments on
       hidden nodes are harmless. */
    $$('[data-diag-value]', sectionEl).forEach(function (cell) {
      var field = cell.getAttribute('data-diag-value');
      if (!field || !Object.prototype.hasOwnProperty.call(valuesDict, field)) return;
      cell.textContent = renderValue(valuesDict[field]);
    });
    /* Fix B — return the effective post-debounce state so the caller
       can rebuild a debounce-aware uncollected/anomaly list for the
       banner severity calc. */
    return newState;
  }

  /* ----- Service journal tails: per-unit hydration (#436) ---------------
     Tails are no longer server-rendered — a cold journalctl is ~5-7s on a Pi
     Zero 2W and used to block first paint on the SSR/poll path. Each
     NON-healthy row hydrates its own tail from /api/diagnostics/journal?unit=,
     independently, so one slow/failed unit can't stall another's (the
     multi-failure case). Healthy rows have no tail and fire no fetch. */
  var _serviceTails = Object.create(null);  /* unit -> {status:'loading'|'ok'|'error', lines:[]} */
  var _tailGen = Object.create(null);       /* unit -> int; latest-wins guard against out-of-order fetches */
  var _serverCopyPayload = '';

  function hasNonHealthyServiceRow() {
    return !!$('[data-diag-unit][data-diag-healthy="0"]');
  }

  function nonHealthyUnits() {
    return $$('[data-diag-unit][data-diag-healthy="0"]').map(function (li) {
      return li.getAttribute('data-diag-unit');
    });
  }

  /* Own AbortController + timeout, mirroring fetchDiagnostics: a timeout is
     re-thrown as TimeoutError so the row's error branch shows "couldn't load
     logs" rather than silently swallowing the timeout (PR #440 pattern). */
  function fetchJournalTail(unit) {
    var url = '/api/diagnostics/journal?unit=' + encodeURIComponent(unit);
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timedOut = false;
    var t = controller ? setTimeout(function () { timedOut = true; controller.abort(); }, FETCH_TIMEOUT_MS) : null;
    return fetch(url, { credentials: 'same-origin', signal: controller ? controller.signal : undefined })
      .then(function (r) {
        if (t) clearTimeout(t);
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .catch(function (err) {
        if (t) clearTimeout(t);
        if (timedOut && err && err.name === 'AbortError') {
          var te = new Error('journal fetch timeout'); te.name = 'TimeoutError'; throw te;
        }
        throw err;
      });
  }

  function tailStatusText(entry) {
    if (!entry || entry.status === 'loading') return 'loading logs…';
    if (entry.status === 'error') return 'couldn’t load logs';
    return (entry.lines && entry.lines.length) ? entry.lines.join('\n') : 'no recent log lines';
  }

  /* Update (or create) the tail <pre> for one row from _serviceTails state. */
  function updateRowTail(unit) {
    var li = $('[data-diag-unit="' + unit + '"]');
    /* Never (re)create a tail on a row that recovered to healthy — its slot was
       removed by patchServicesSection — or that no longer exists. Without this,
       a stale in-flight fetch resolving after a recovery poll would resurrect
       old error logs onto a now-OK row (cross-model /review: Codex + Claude). */
    if (!li || li.getAttribute('data-diag-healthy') === '1') return;
    var pre = li.querySelector('[data-diag-tail]');
    if (!pre) {
      pre = document.createElement('pre');
      pre.className = 'diag-service__tail';
      pre.setAttribute('data-diag-tail', '');
      var code0 = document.createElement('code');
      code0.setAttribute('data-diag-tail-body', '');
      pre.appendChild(code0);
      li.appendChild(pre);
    }
    var entry = _serviceTails[unit];
    pre.setAttribute('data-diag-tail-status', entry ? entry.status : 'loading');
    var code = pre.querySelector('[data-diag-tail-body]') || pre.querySelector('code');
    if (code) code.textContent = tailStatusText(entry);
  }

  /* Fire one independent fetch per non-healthy row. Safe to call repeatedly
     (boot + every poll); the server caches each tail for DIAG_JOURNAL_TTL_S
     (45s > 30s poll) so a still-failed unit doesn't re-fork journalctl. */
  function hydrateServiceTails() {
    nonHealthyUnits().forEach(function (unit) {
      /* Only show 'loading' on the FIRST fetch for this unit. On a poll rebuild
         of an already-loaded tail, keep the prior lines visible + refetch in the
         background — avoids a "loading logs…" flash every 30s and stops the Copy
         blob from momentarily emitting (loading) for a unit the user is
         debugging (cross-model /review: Codex + Claude). */
      if (!_serviceTails[unit]) {
        _serviceTails[unit] = { status: 'loading', lines: [] };
        updateRowTail(unit);
      }
      /* Per-unit generation: a slower, older response can't overwrite a newer
         one (independent journalctl forks finish out of order). Mirrors the main
         poll's generation guard. */
      var gen = _tailGen[unit] = (_tailGen[unit] || 0) + 1;
      fetchJournalTail(unit)
        .then(function (body) {
          if (_tailGen[unit] !== gen) return;
          _serviceTails[unit] = { status: 'ok', lines: (body && body.journal_tail) || [] };
        })
        .catch(function () {
          if (_tailGen[unit] !== gen) return;
          /* Client-side failure (abort/timeout/http). A server-side journalctl
             failure arrives as an empty tail (renders as 'ok, no lines') — the
             collector can't distinguish empty from failed (#436 T3). */
          _serviceTails[unit] = { status: 'error', lines: [] };
        })
        .then(function () {
          if (_tailGen[unit] !== gen) return;
          updateRowTail(unit);
          renderCopyBlock();
        });
    });
  }

  /* T1/T3 — the copy support payload is server-built WITHOUT tails now, so
     append the client-hydrated logs so a Copy after hydration is complete. */
  function serviceLogsAppendix() {
    var units = nonHealthyUnits();
    if (!units.length) return '';
    var out = '\n\n## Service logs\n';
    units.forEach(function (unit) {
      var entry = _serviceTails[unit];
      out += '\n### ' + unit + '\n';
      if (!entry || entry.status === 'loading') out += '(loading)\n';
      else if (entry.status === 'error') out += '(logs unavailable)\n';
      else out += ((entry.lines && entry.lines.length) ? entry.lines.join('\n') : '(no recent log lines)') + '\n';
    });
    return out;
  }

  function renderCopyBlock() {
    var block = $('[data-diag-copy-block] code');
    if (!block) return;
    var appendix = serviceLogsAppendix();
    if (!appendix) { block.textContent = _serverCopyPayload; return; }
    /* Splice the hydrated logs INSIDE the server payload's fenced ```markdown```
       block (before the final closing fence) so they paste as literal log text,
       not active Markdown headings, in the support target (cross-model /review). */
    var fence = _serverCopyPayload.lastIndexOf('\n```');
    if (fence >= 0) {
      block.textContent = _serverCopyPayload.slice(0, fence) + appendix + _serverCopyPayload.slice(fence);
    } else {
      block.textContent = _serverCopyPayload + appendix;
    }
  }

  /* F-SERVICES-STALE: rebuild the services <ul> from values.service_states so
     the per-unit rows stay in sync with the banner + pill on every poll. Tails
     are NOT in values (they hydrate per-unit, #436) — non-healthy rows render
     their current _serviceTails state (loading/ok/error). createElement +
     textContent only (no innerHTML) to preserve the XSS-defense posture. */
  function patchServicesSection(valuesDict) {
    var ul = $('[data-diag-services]');
    if (!ul) return;
    var services = (valuesDict && valuesDict.service_states) || {};
    /* Clear in place to preserve focus position if user hasn't tabbed
       into a child. */
    while (ul.firstChild) ul.removeChild(ul.firstChild);
    Object.keys(services).forEach(function (unit) {
      var info = services[unit] || {};
      var li = document.createElement('li');
      li.className = 'diag-service';
      li.setAttribute('data-diag-unit', unit);
      /* #436 — server-computed health gates client-side tail hydration
         (data-diag-healthy="0" rows get a per-unit fetch). Reading the flag
         from the same predicate the server used avoids JS/server drift on
         oneshot-inactive / transient rows. */
      li.setAttribute('data-diag-healthy', info.healthy ? '1' : '0');

      var head = document.createElement('div');
      head.className = 'diag-service__head';
      var nameSpan = document.createElement('span');
      nameSpan.className = 'diag-service__name mono';
      nameSpan.textContent = unit;
      head.appendChild(nameSpan);
      var stateSpan = document.createElement('span');
      var state = info.state || 'unknown';
      /* Chip COLOR follows state_modifier (server emits 'transient-ok' for a
         oneshot mid-paint, #449, and 'settled-ok' for a oneshot at its
         by-design inactive resting state, #463, so the row tint matches the
         OK section pill); chip TEXT stays the literal systemd state. */
      var modifier = info.state_modifier || state;
      stateSpan.className = 'diag-service__state diag-service__state--' + modifier + ' mono';
      stateSpan.textContent = state;
      head.appendChild(stateSpan);
      li.appendChild(head);

      /* #436 — non-healthy rows carry a tail slot reflecting the current
         per-unit hydration state (loading/ok/error). Healthy rows have none.
         Reads _serviceTails so a poll rebuild doesn't wipe an already-loaded
         tail back to a "loading" flash. */
      if (!info.healthy) {
        var entry = _serviceTails[unit];
        var pre = document.createElement('pre');
        pre.className = 'diag-service__tail';
        pre.setAttribute('data-diag-tail', '');
        pre.setAttribute('data-diag-tail-status', entry ? entry.status : 'loading');
        var code = document.createElement('code');
        code.setAttribute('data-diag-tail-body', '');
        code.textContent = tailStatusText(entry);
        pre.appendChild(code);
        li.appendChild(pre);
      }

      ul.appendChild(li);
    });
  }

  /* ----- Banner (D29) --------------------------------------------------- */

  /* D29 + #432 D6 — banner has 4 visual states:
       ok          → green check + "All running" + --success
       warning     → ochre triangle + "Something needs attention" + --warning
                     (used for system / network / time-location / setup-markers
                     / build-version / last-quote anomalies — recoverable conditions)
       error       → oxblood triangle + "Clock isn't running" + --error
                     (used when services anomaly OR recent-log-entries anomaly
                     fires — clock paint is at risk)
       settling    → graphite hourglass + "Just settling in." + section-aware
                     body (--ink-muted, neutral, no border-top accent)
                     fires when ALL non-OK sections are uncollected and NO
                     real anomaly is firing.
     The page's lead visual swap is what owner-persona looks at first; the
     copy + color escalation matches the severity.

     Anomaly tiers ALWAYS win over settling: a real failure must never
     read as "just settling in." Order matters — settling is the lowest-
     priority non-OK tier.
  */
  function bannerSeverity(anomalies, uncollected) {
    if (anomalies.indexOf('services') !== -1 || anomalies.indexOf('recent-log-entries') !== -1) {
      return 'error';
    }
    if (anomalies.length > 0) return 'warning';
    if (uncollected && uncollected.length > 0) return 'settling';
    return 'ok';
  }

  function bannerTitle(severity) {
    if (severity === 'error') return "Clock isn't running";
    if (severity === 'warning') return 'Something needs attention';
    if (severity === 'settling') return 'Just settling in.';
    return 'All running';
  }

  function patchBanner(anomalies, uncollected, refreshedAtMs) {
    var banner = $('[data-diag-banner]');
    var severity = bannerSeverity(anomalies, uncollected);
    if (banner) {
      banner.classList.remove(
        'status-banner--ok',
        'status-banner--warning',
        'status-banner--error',
        'status-banner--settling'
      );
      banner.classList.add('status-banner--' + severity);
      var okIcon = banner.querySelector('.status-banner__icon--ok');
      var warnIcon = banner.querySelector('.status-banner__icon--warning');
      var settlingIcon = banner.querySelector('.status-banner__icon--settling');
      if (okIcon) okIcon.hidden = severity !== 'ok';
      if (warnIcon) warnIcon.hidden = severity === 'ok' || severity === 'settling';
      if (settlingIcon) settlingIcon.hidden = severity !== 'settling';
      /* F8 fix — role="status" + aria-live="polite" now live permanently
         in SSR on the [data-diag-banner-live] wrapper, NOT on this
         banner element. JS no longer adds/removes them at runtime, so
         the live region survives severity transitions without dropping
         queued announcements. */
    }
    var title = $('[data-diag-banner-title]');
    if (title) title.textContent = bannerTitle(severity);

    /* #432 D6 + F8 — body is rendered ONLY for the settling tier; insert
       or remove the node depending on severity so the layout doesn't
       leave an empty <p> behind on the OK tier. Insert INTO the
       [data-diag-banner-live] wrapper (NOT the outer __copy div) so the
       body change is announced by the permanent live region. */
    var live = banner ? banner.querySelector('[data-diag-banner-live]') : null;
    var body = $('[data-diag-banner-body]');
    if (severity === 'settling') {
      var bodyText = _settlingBody(uncollected || []);
      if (bodyText) {
        if (!body && live) {
          body = document.createElement('p');
          body.className = 'status-banner__body';
          body.setAttribute('data-diag-banner-body', '');
          /* Append to the live region — title is the only child today,
             so appendChild keeps reading order title → body. */
          live.appendChild(body);
        }
        if (body && body.textContent !== bodyText) {
          /* F12 micro-fix — only assign textContent if it changed, so a
             stable settling state doesn't re-trigger the live region
             every 30s poll. */
          body.textContent = bodyText;
        }
      } else if (body && body.parentNode) {
        body.parentNode.removeChild(body);
      }
    } else if (body && body.parentNode) {
      body.parentNode.removeChild(body);
    }

    var refreshed = $('[data-diag-banner-refreshed]');
    if (refreshed && refreshedAtMs) {
      refreshed.textContent = 'Refreshed ' + formatRelative(refreshedAtMs);
    }
  }

  function formatRelative(thenMs) {
    var ageS = Math.max(0, Math.round((Date.now() - thenMs) / 1000));
    if (ageS < 5) return 'just now';
    if (ageS < 60) return ageS + 's ago';
    if (ageS < 3600) return Math.floor(ageS / 60) + 'm ago';
    return Math.floor(ageS / 3600) + 'h ago';
  }

  /* ----- Copy payload --------------------------------------------------- */

  function patchCopyBlock(text) {
    /* #436 — the server payload no longer carries service logs (tails hydrate
       client-side). Store it and re-render with the client log appendix so a
       poll refresh doesn't drop the hydrated logs from the copy blob. */
    _serverCopyPayload = text || '';
    renderCopyBlock();
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'absolute';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) resolve(); else reject(new Error('copy failed'));
      } catch (e) {
        reject(e);
      }
    });
  }

  function announce(message) {
    var node = $('[data-diag-announcer]');
    if (!node) return;
    node.textContent = '';
    setTimeout(function () { node.textContent = message; }, ANNOUNCE_RESET_MS);
  }

  function flashCopySuccess(buttonEl) {
    if (!buttonEl) return;
    buttonEl.setAttribute('data-diag-copy-success', 'true');
    setTimeout(function () {
      buttonEl.removeAttribute('data-diag-copy-success');
    }, COPY_FLASH_MS);
  }

  /* ----- Per-log-entry copy (D10) -------------------------------------- */

  function formatLogEntryForCopy(li) {
    var t = $('.diag-log-entry__time', li);
    var l = $('.diag-log-entry__level', li);
    var m = $('.diag-log-entry__msg', li);
    return (
      (t ? t.textContent.trim() + ' ' : '') +
      (l ? l.textContent.trim() + ' ' : '') +
      (m ? m.textContent.trim() : '')
    ).trim();
  }

  function handleLogEntryCopyClick(e) {
    var btn = e.target.closest && e.target.closest('[data-diag-log-copy]');
    if (!btn) return;
    e.preventDefault();
    var li = btn.closest('.diag-log-entry');
    if (!li) return;
    copyToClipboard(formatLogEntryForCopy(li))
      .then(function () {
        announce('Copied');
        flashCopySuccess(btn);
      })
      .catch(function () {
        announce('Couldn’t copy. Long-press to select instead.');
      });
  }

  /* ----- Bottom copy button (D18) -------------------------------------- */

  function handleCopyButtonClick(e) {
    var btn = e.currentTarget;
    e.preventDefault();
    var block = $('[data-diag-copy-block] code');
    var text = block ? block.textContent : '';
    copyToClipboard(text)
      .then(function () {
        announce('Copied support payload');
        flashCopySuccess(btn);
      })
      .catch(function () {
        announce('Couldn’t copy. Long-press to select instead.');
      });
  }

  /* ----- Section toggle tracking ------------------------------------- */

  function handleSectionToggle(sectionEl, userInteractedSections) {
    return function () {
      var id = sectionEl.getAttribute('data-diag-section');
      if (!id) return;
      /* If THIS toggle was queued by patchSection's JS-driven open=X
         assignment, consume the marker and skip. Any toggle without a
         marker is a real user click on the <summary>. */
      if (_jsOpenInFlight[id]) {
        delete _jsOpenInFlight[id];
        return;
      }
      userInteractedSections[id] = true;
    };
  }

  /* ----- Refresh loop (D12) -------------------------------------------- */

  /* F-REVEAL-RACE: every refresh() captures the current reveal state at
     request start. When the response lands, we discard it if the reveal
     state has since changed — preventing the OLD revealed response from
     patching sensitive data into the DOM after the user toggled away.

     #435 (PR4 / CQ-3): returns ``{refresh, abort}`` so the Reveal click
     handler can `abort()` an in-flight fetch then immediately `refresh()`
     with the new reveal state, instead of waiting for the slow fetch
     (e.g. journal-tail on a degraded Pi Zero 2W) to either finish or
     time out. The state machine is explicit at the boundary. */
  function makeRefresher(state) {
    var pending = false;
    var generation = 0;
    var meta = $('[data-diag-banner-meta]');
    var currentController = null;
    var unwindTimer = null;

    function refresh() {
      if (pending) return;
      pending = true;
      generation += 1;
      var myGen = generation;
      var myReveal = readReveal();
      /* Each refresh gets its own controller. ``abort()`` snapshots
         ``currentController`` so an abort during refresh N aborts N's
         fetch, not a later N+1's. */
      var myController = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      currentController = myController;
      if (meta) meta.setAttribute('data-diag-meta-refreshing', 'true');
      var refreshStartedAt = Date.now();
      fetchDiagnostics(myReveal, myController)
        .then(function (body) {
          /* F-REVEAL-RACE guard: if the user toggled Reveal between
             request-start and response, discard. A fresh refresh() will
             land with the new state. */
          if (myGen !== generation) return;
          if (myReveal !== readReveal()) return;
          state.lastRefreshAt = Date.now();
          /* #432 D2 — getSectionStates defaults both fields to [] so a
             cached client reading a pre-v0.214.4 payload doesn't crash on
             a missing `uncollected` key. */
          var ss = getSectionStates(body);
          state.anomalies = ss.anomalies;
          state.uncollected = ss.uncollected;
          state.consecutiveFailures = 0;
          var values = body.values || {};
          /* F2 fix — collect forward-transition section IDs across the
             per-section loop and announce ONE batched message at the end
             instead of per-section announce() calls that overwrite each
             other through the announce() reset-then-setTimeout race.
             Fix B — also collect the effective post-debounce uncollected
             + anomaly lists so the banner sees the SAME tier the pills
             show (raw `state.uncollected` from server would let banner
             flip to "Just settling in." while a debounced pill stays
             green — internally contradictory). */
          var forwardTransitions = [];
          var effectiveUncollected = [];
          var effectiveAnomalies = [];
          $$('[data-diag-section]').forEach(function (sectionEl) {
            var id = sectionEl.getAttribute('data-diag-section');
            if (!id) return;
            var effective = patchSection(
              sectionEl,
              id,
              state.anomalies,
              state.uncollected,
              values,
              state.userInteractedSections,
              state.priorStates,
              forwardTransitions
            );
            if (effective === 'uncollected') effectiveUncollected.push(id);
            else if (effective === 'anomaly') effectiveAnomalies.push(id);
          });
          _announceForwardTransitions(forwardTransitions);
          patchServicesSection(values);
          /* #436 — re-hydrate tails for any still-non-healthy row after the
             rebuild (server caches each tail 45s > 30s poll, so this doesn't
             re-fork journalctl every cycle). */
          hydrateServiceTails();
          patchBanner(effectiveAnomalies, effectiveUncollected, state.lastRefreshAt);
          patchCopyBlock(body.copy_payload || '');
          /* #432 D11 — clear the page-level poll-stale flag on first
             success after a streak of failures. */
          _setPollStale(false);
        })
        .catch(function (err) {
          /* #435: ``abort()`` synthesises an AbortError — it's a user-
             intended cancel, NOT a failure. Don't count it against
             consecutiveFailures (would otherwise show "Last refresh
             failed — retrying" the instant the user toggled Reveal). */
          if (err && err.name === 'AbortError') return;
          /* Also discard if a newer refresh has superseded us — the new
             one owns the failure-count state. */
          if (myGen !== generation) return;
          /* F-FAILURE-MASKED: track consecutive failures so the user
             sees a persistent indicator after the 2s hint window. */
          state.consecutiveFailures = (state.consecutiveFailures || 0) + 1;
          /* #432 D11 — set the page-level poll-stale flag once we've
             accumulated POLL_STALE_FAILURES (default 2) consecutive
             failures. The pill opacity drops to 60% so the user can
             see at-a-glance "this surface is stale." */
          if (state.consecutiveFailures >= POLL_STALE_FAILURES) {
            _setPollStale(true);
          }
        })
        .then(function () {
          /* Drop the controller reference if we're still its owner. */
          if (currentController === myController) currentController = null;
          /* If a newer refresh has taken over (e.g. via abort+refresh),
             skip the meta-UI unwind — the new refresh owns the spinner
             and the pending flag now. */
          if (myGen !== generation) return;
          var elapsed = Date.now() - refreshStartedAt;
          var unwind = Math.max(0, REFRESH_HINT_MS - elapsed);
          unwindTimer = setTimeout(function () {
            unwindTimer = null;
            /* Second guard inside the timeout in case a refresh slipped
               in between the .then resolution and the setTimeout firing. */
            if (myGen !== generation) return;
            if (meta) {
              meta.removeAttribute('data-diag-meta-refreshing');
              if (state.consecutiveFailures > 0) {
                meta.setAttribute('data-diag-meta-failed', String(state.consecutiveFailures));
              } else {
                meta.removeAttribute('data-diag-meta-failed');
              }
            }
            pending = false;
          }, unwind);
        });
    }

    function abort() {
      /* /review on PR #440 found two regressions in the prior shape of
         this function: (1) the `if (!currentController) return;` guard
         left `pending=true` stuck in environments where AbortController
         is undefined (old WebViews) so the synchronous refresh() after
         abort() bailed on pending — strictly worse than the no-PR4
         baseline; (2) the "post-success dead zone" between fetch
         resolution and the 2 s unwind setTimeout was also pending=true
         + currentController=null, so abort() bailed early and the Reveal
         click was a no-op for up to 2 s after every successful refresh.
         Always reset the machine state — the generation bump alone is
         enough to discard any racing late response. */
      if (currentController) {
        try { currentController.abort(); } catch (e) { /* ignore */ }
      }
      if (unwindTimer) {
        clearTimeout(unwindTimer);
        unwindTimer = null;
      }
      pending = false;
      generation += 1;
      currentController = null;
      /* Drop the refreshing-meta attribute immediately so the user
         doesn't see a stale spinner in the gap before the new refresh
         flips it back on. */
      if (meta) meta.removeAttribute('data-diag-meta-refreshing');
    }

    return { refresh: refresh, abort: abort };
  }

  function startMetaTicker(state) {
    return setInterval(function () {
      var refreshed = $('[data-diag-banner-refreshed]');
      if (!refreshed) return;
      if (state.consecutiveFailures > 0) {
        refreshed.textContent = 'Last refresh failed — retrying';
      } else if (state.lastRefreshAt) {
        refreshed.textContent = 'Refreshed ' + formatRelative(state.lastRefreshAt);
      }
    }, META_TICKER_MS);
  }

  /* ----- Boot lifecycle + teardown (F-LISTENER-STACK) ------------------ */

  /* Listeners installed on document survive page reloads in jsdom-style
     test harnesses. Track them on a module-level handle so vitest can
     call window.__litclockDiag.teardown() between tests and clean up. */
  var _lifecycle = null;

  function boot() {
    if (_lifecycle) {
      /* Defensive: if boot is called twice without teardown, tear down
         first. Production never re-boots; this guards against test re-
         entry. */
      teardown();
    }

    var state = {
      lastRefreshAt: null,
      anomalies: [],
      /* #432 — uncollected mirrors anomalies in shape. priorStates is the
         per-section cached logical state ('ok' | 'uncollected' | 'anomaly')
         used by patchSection to detect forward/reverse transitions for
         the D8 debounce + D9 SR announcer. */
      uncollected: [],
      priorStates: Object.create(null),
      userInteractedSections: Object.create(null),
      consecutiveFailures: 0
    };

    /* F1 fix — seed priorStates from the SSR-rendered pill class so the
       FIRST post-boot poll that completes a fresh-flash uncollected → ok
       transition fires the D9 SR announcement. Without this seed,
       priorStates[sectionId] is undefined on the first poll, the
       `oldState === 'uncollected'` check fails, and the gift-recipient
       persona (the entire reason for D9) gets no auditory confirmation
       that the clock came online. Read every section's pill class once
       at boot. */
    $$('[data-diag-section]').forEach(function (sectionEl) {
      var id = sectionEl.getAttribute('data-diag-section');
      if (!id) return;
      var pill = sectionEl.querySelector('[data-diag-section-pill]');
      if (!pill) return;
      if (pill.classList.contains('diag-section__pill--muted')) {
        state.priorStates[id] = 'uncollected';
      } else if (pill.classList.contains('diag-section__pill--warning')) {
        state.priorStates[id] = 'anomaly';
      } else {
        state.priorStates[id] = 'ok';
      }
    });

    applyRevealUI(readReveal());

    /* Section toggle listeners. Each handler is named per section so
       teardown can remove the exact reference. */
    var sectionHandlers = [];
    $$('[data-diag-section]').forEach(function (sectionEl) {
      var handler = handleSectionToggle(sectionEl, state.userInteractedSections);
      sectionEl.addEventListener('toggle', handler);
      sectionHandlers.push({ el: sectionEl, handler: handler });
    });

    /* Document-level delegated handlers — single instance, removable. */
    document.addEventListener('click', handleLogEntryCopyClick);

    var copyBtn = $('[data-diag-copy-button]');
    if (copyBtn) copyBtn.addEventListener('click', handleCopyButtonClick);

    /* Reveal pill click handler. */
    var revealBtn = $('[data-diag-reveal]');
    var revealHandler = null;
    /* #435: refresher exposes ``{refresh, abort}`` so the click handler
       can abort an in-flight fetch and immediately fire a new one with
       the toggled reveal state. The Reveal click is the only caller
       that uses ``abort()``; poll + visibility paths only ``refresh()``. */
    var refresher = makeRefresher(state);
    if (revealBtn) {
      revealHandler = function (e) {
        e.preventDefault();
        var on = !readReveal();
        writeReveal(on);
        applyRevealUI(on);
        announce(on ? 'Reveal on. SSID, city, and coordinates now visible.'
                    : 'Reveal off. Sensitive values hidden again.');
        /* abort() is a no-op if no fetch is in-flight, so the literal
           sequence ``abort(); refresh();`` reads as the intent: cancel
           whatever's pending, fire a fresh fetch with the new reveal. */
        refresher.abort();
        refresher.refresh();
      };
      revealBtn.addEventListener('click', revealHandler);
    }

    /* Visibility-aware poll scheduling. */
    var pollTimer = null;
    function schedulePoll() {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(function tick() {
        if (!document.hidden) refresher.refresh();
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
      }, POLL_INTERVAL_MS);
    }
    function stopPoll() {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    }
    var visibilityHandler = function () {
      if (!document.hidden) {
        refresher.refresh();
        schedulePoll();
      } else {
        stopPoll();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    /* Boot refresh: if Reveal is on, fire immediately so values match.
       Otherwise SSR is authoritative for state/verdict; just wait for the
       first interval. (Since #436 tails are NOT part of that SSR authority —
       they hydrate separately just below.) */
    if (readReveal()) {
      refresher.refresh();
    }
    /* #436 — capture the SSR-rendered copy payload so client-hydrated service
       logs can be appended without losing it, then hydrate tails for any
       non-healthy row. Self-gating: a healthy clock has no data-diag-healthy="0"
       row, so this fires ZERO fetches. */
    var copyCode = $('[data-diag-copy-block] code');
    _serverCopyPayload = copyCode ? copyCode.textContent : '';
    hydrateServiceTails();
    schedulePoll();
    var tickerHandle = startMetaTicker(state);

    _lifecycle = {
      state: state,
      sectionHandlers: sectionHandlers,
      copyBtn: copyBtn,
      revealBtn: revealBtn,
      revealHandler: revealHandler,
      pollTimer: function () { return pollTimer; },
      stopPoll: stopPoll,
      visibilityHandler: visibilityHandler,
      tickerHandle: tickerHandle
    };
  }

  function teardown() {
    if (!_lifecycle) return;
    var lc = _lifecycle;
    lc.sectionHandlers.forEach(function (entry) {
      entry.el.removeEventListener('toggle', entry.handler);
    });
    document.removeEventListener('click', handleLogEntryCopyClick);
    if (lc.copyBtn) lc.copyBtn.removeEventListener('click', handleCopyButtonClick);
    if (lc.revealBtn && lc.revealHandler) lc.revealBtn.removeEventListener('click', lc.revealHandler);
    document.removeEventListener('visibilitychange', lc.visibilityHandler);
    lc.stopPoll();
    /* #432 — clear the module-level transition store so vitest re-entry
       between tests doesn't leak forward-transition timestamps from a
       prior IIFE evaluation. The _jsOpenInFlight marker store is also
       module-level — clear it for the same reason. */
    _uncollectedForwardAt = Object.create(null);
    _jsOpenInFlight = Object.create(null);
    /* #436 — clear per-unit tail state + captured copy payload so vitest
       re-entry between tests doesn't leak a prior IIFE's hydrated tails. */
    _serviceTails = Object.create(null);
    _tailGen = Object.create(null);
    _serverCopyPayload = '';
    if (lc.tickerHandle) clearInterval(lc.tickerHandle);
    _lifecycle = null;
  }

  /* Test seam — see F-LISTENER-STACK fix. Hidden behind a namespaced
     property so production-side consumers don't accidentally rely on it. */
  if (typeof window !== 'undefined') {
    window.__litclockDiag = { teardown: teardown, boot: boot };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
