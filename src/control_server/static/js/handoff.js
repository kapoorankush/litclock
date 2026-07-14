/* Post-WiFi handoff banner controller (EPIC #383 PR2, #388).
 *
 * Two states, keyed on the banner's data-handoff-state:
 *
 *   success — IP-geo detected a location. The "Done — Start the Clock" button
 *             POSTs /api/handoff/done; quotes start on the next minute tick.
 *
 *   failure — no location/tz detected. We read the browser's timezone via
 *             Intl.DateTimeFormat (the server can't know the phone's tz),
 *             relabel the button "Use {tz}", and POST it to
 *             /api/handoff/set-timezone so quotes start at the RIGHT time
 *             (design-review A2: a wrong-time clock is worse than no clock).
 *
 * On success, complete() removes data-handoff-active from <body> (restoring
 * the dimmed Status hero), slides the banner out, and dispatches
 * 'litclock:handoff-complete' so aths-hint.js runs its normal first-run logic.
 *
 * No CSRF token: these endpoints are non-destructive + idempotent and live
 * only in the brief handoff window on the user's own LAN (locked plan A6). */
(function () {
  'use strict';

  var banner = document.getElementById('handoff-banner');
  if (!banner) return;

  var state = banner.getAttribute('data-handoff-state');

  var reduceMotion = false;
  try {
    reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (_e) { /* matchMedia missing — default to animated */ }

  function postJson(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok && !!(data && data.ok), status: res.status, data: data };
      });
    });
  }

  function busy(btn, on) {
    if (!btn) return;
    btn.disabled = on;
    btn.classList.toggle('handoff__btn--busy', on);
  }

  function complete() {
    // Remove the body attribute FIRST so aths-hint's init() (fired by the
    // event below) doesn't re-bail on the suppression check.
    document.body.removeAttribute('data-handoff-active');

    function finish() {
      if (banner.parentNode) banner.parentNode.removeChild(banner);
      document.dispatchEvent(new CustomEvent('litclock:handoff-complete'));
    }

    if (reduceMotion) {
      finish();
      return;
    }

    var done = false;
    function onEnd() {
      if (done) return;
      done = true;
      finish();
    }
    banner.addEventListener('transitionend', onEnd);
    // Fallback if transitionend never fires (e.g. display:none mid-transition).
    setTimeout(onEnd, 500);
    banner.classList.add('handoff--leaving');
  }

  function detectTimezone() {
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return (typeof tz === 'string' && tz) ? tz : null;
    } catch (_e) {
      return null;
    }
  }

  function wireDone() {
    var btn = document.getElementById('handoff-done');
    if (!btn) return;
    btn.addEventListener('click', function () {
      busy(btn, true);
      postJson('/api/handoff/done', {}).then(function (r) {
        if (r.ok) {
          complete();
        } else {
          // Leave the banner up; the 120s timer will complete it, or the user
          // can retry. Re-enable so the tap isn't dead.
          busy(btn, false);
        }
      }).catch(function () {
        busy(btn, false);
      });
    });
  }

  function wireSetTimezone() {
    var btn = document.getElementById('handoff-set-tz');
    if (!btn) return;

    var tz = detectTimezone();
    if (tz) {
      var template = btn.getAttribute('data-tz-label-template') || 'Use {tz}';
      btn.textContent = template.replace('{tz}', tz);
      btn.setAttribute('data-timezone', tz);
      var failBody = document.getElementById('handoff-fail-body');
      if (failBody) {
        failBody.textContent =
          'We couldn’t detect your timezone. Your phone says you’re in ' + tz +
          '. Confirm so quotes show at the right time.';
      }
    }

    btn.addEventListener('click', function () {
      var chosen = btn.getAttribute('data-timezone');
      if (!chosen) {
        // No browser tz available — send the user to Settings to pick one.
        window.location.href = '/settings#weather';
        return;
      }
      busy(btn, true);
      postJson('/api/handoff/set-timezone', { timezone: chosen }).then(function (r) {
        if (r.ok) {
          complete();
        } else {
          // Server rejected the tz (unrecognized IANA name) — fall back to the
          // manual picker in Settings.
          busy(btn, false);
          window.location.href = '/settings#weather';
        }
      }).catch(function () {
        busy(btn, false);
      });
    });
  }

  if (state === 'success') {
    wireDone();
  } else {
    wireSetTimezone();
  }
})();
