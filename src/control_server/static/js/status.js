/* Status tab live refresh (#290).
 *
 * Server-side rendering carries the quote on first paint (PRD §7.5
 * progressive enhancement). Without JS, the user sees whatever was current
 * when the page loaded and has to reload to see new quotes. With JS, we
 * poll /api/status every 30s and patch the hero + status-row DOM in place
 * so the PWA reflects what the e-ink is showing.
 *
 * Android's service worker serves the cached navigation HTML during
 * slow-network / Pi-rebooting windows, so without client-side refresh the
 * stale hero would persist across what would have been a fresh-fetch moment.
 *
 * Patch strategy: refetch /api/status, swap textContent / class state on
 * data-status-* hooks in status.html.j2. Never innerHTML — defends against
 * XSS from any future field that might leak HTML.
 */

(function () {
  'use strict';

  var POLL_INTERVAL_MS = 30000;
  var FETCH_TIMEOUT_MS = 5000;
  // SSID truncation mirrors the Jinja `truncate(18, true, '…')` filter in
  // status.html.j2 — DESIGN.md "Status row list" locks SSID display at 18
  // chars regardless of viewport width. Both sites must stay in lockstep
  // or the SSR and post-poll renders disagree.
  var SSID_MAX_CHARS = 18;

  // #309 — mDNS bookmark probe constants. After the PWA loads via IP
  // (the address the corner QR encoded), we check whether the phone
  // can also resolve litclock.local on this network. If yes, the user
  // gets a one-tap switch to the address-stable URL so AtHS bookmarks
  // a name that survives DHCP churn. Silent if the probe fails.
  var MDNS_HOST = 'litclock.local';
  // Build a control-server URL for a given host at the SAME port this page was
  // served on (#343). window.location.port is '' when the port is the scheme
  // default (80), so the URL is bare `http://litclock.local` — no port to type.
  // Deriving from the live origin means the probe/switch target can never drift
  // from control_server's actual port (80 today, 8443 historically, any dev
  // override) without any constant to maintain.
  function controlUrl(host) {
    var port = window.location.port;
    return 'http://' + host + (port ? ':' + port : '');
  }
  var MDNS_PROBE_TIMEOUT_MS = 1500;
  var MDNS_PROBE_DELAY_MS = 2000;  // wait past first paint
  var MDNS_STORAGE_KEY = 'litclock.mdns-bookmark-dismissed';
  // /review (#406 user feedback): mDNS bookmark + AtHS prompt fired
  // back-to-back on first load, causing card fatigue + risking two
  // home-screen bookmarks (one per origin). aths-hint.js now defers
  // until the mDNS probe result is known via this custom event. If
  // mDNS works: AtHS suppressed (user gets the .local prompt instead,
  // and AtHS fires naturally on the new origin post-Switch). If mDNS
  // doesn't work: AtHS fires normally with a small delay.
  var MDNS_RESULT_EVENT = 'litclock:mdns-result';

  function getEl(selector) {
    return document.querySelector(selector);
  }

  function fetchWithTimeout(url, ms) {
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var opts = { headers: { Accept: 'application/json' } };
    var timer = null;
    if (ctrl) {
      opts.signal = ctrl.signal;
      timer = setTimeout(function () { ctrl.abort(); }, ms);
    }
    return fetch(url, opts).finally(function () {
      // Clear the abort timer on success/failure so we don't leak a timer
      // per poll. Without this, a 30-min PWA session accumulates ~60 pending
      // setTimeout entries — harmless individually but compounds.
      if (timer !== null) clearTimeout(timer);
    });
  }

  function setText(el, text) {
    if (!el) return;
    var s = (text === null || text === undefined) ? '' : String(text);
    if (el.textContent !== s) el.textContent = s;
  }

  function setHidden(el, hidden) {
    if (!el) return;
    if (hidden) {
      el.setAttribute('hidden', '');
    } else {
      el.removeAttribute('hidden');
    }
  }

  function buildAttributionPrefix(s) {
    // Mirrors the Jinja format at status.html.j2:61. Just the prefix span
    // contents: em-dash + author + ", " if title is present. The italic
    // title node + the locked '·&nbsp;' time separator are static template
    // text — JS doesn't touch them, just the inner [data-status-attr-title]
    // and [data-status-attr-time] textContent.
    return '— ' + (s.author || '') + (s.title ? ', ' : '');
  }

  function formatStaleBannerText(s) {
    if (s.picked_at_age_s !== null && s.picked_at_age_s !== undefined) {
      var mins = Math.floor(s.picked_at_age_s / 60);
      return 'Clock service may be paused — last quote ' + mins + ' min ago';
    }
    return 'Clock service may be paused — no quote published yet';
  }

  function patch(s) {
    if (!s || s.ok !== true) return;

    // Stale banner: show / hide + update text.
    var banner = getEl('[data-status-stale-banner]');
    if (banner) {
      setHidden(banner, !s.stale);
      if (s.stale) {
        var bannerText = banner.querySelector('[data-status-stale-text]');
        setText(bannerText, formatStaleBannerText(s));
      }
    }

    // #274 follow-up #5 — Phase 3 skip banner. Server already clamps
    // phase3_skipped_at_unix to "fresh" (< 1 day), so non-null = show.
    // Banner is informational; no text update needed (the template's
    // static body covers the only state we surface).
    var phase3SkipBanner = getEl('[data-status-phase3-skip-banner]');
    if (phase3SkipBanner) {
      setHidden(phase3SkipBanner, !s.phase3_skipped_at_unix);
    }

    // Hero: either show quote+attribution or the empty-state. The Jinja
    // template renders ONE of the two branches at SSR; we toggle between
    // them via [hidden] so the JS doesn't have to manage element creation.
    var heroFull = getEl('[data-status-hero-full]');
    var heroEmpty = getEl('[data-status-hero-empty]');
    if (s.quote) {
      setText(getEl('[data-status-quote]'), s.quote);
      setText(getEl('[data-status-attr-prefix]'), buildAttributionPrefix(s));
      setText(getEl('[data-status-attr-title]'), s.title || '');
      // Show/hide the title em wrapper depending on whether title is present.
      setHidden(getEl('[data-status-attr-title-wrap]'), !s.title);
      setText(getEl('[data-status-attr-time]'), s.time || '');
      setHidden(getEl('[data-status-attr-time-wrap]'), !s.time);
      setHidden(heroFull, false);
      setHidden(heroEmpty, true);
    } else {
      setHidden(heroFull, true);
      setHidden(heroEmpty, false);
    }

    // Status rows: WiFi (truncated to SSID_MAX_CHARS to match the Jinja
    // truncate filter in status.html.j2), Weather, Version, Uptime, Last update.
    setText(getEl('[data-status-wifi]'), s.wifi_ssid ? truncate(s.wifi_ssid, SSID_MAX_CHARS) : '—');
    setText(getEl('[data-status-weather]'), s.weather_city || '—');
    setText(getEl('[data-status-version]'), s.version || '—');
    setText(getEl('[data-status-uptime]'), s.uptime_human || '—');
    // #330: Last update row is "v, relative" (e.g., "5f12b8b, 3 min ago")
    // when last_update_version is known, else just "relative". SSR carries
    // three spans (version, separator, relative); we toggle [hidden] on
    // the first two and patch textContent in place so the mono styling
    // on the SHA doesn't get clobbered by a textContent rewrite on the dd.
    var versionEl = getEl('[data-status-last-update-version]');
    var sepEl = getEl('[data-status-last-update-sep]');
    var relEl = getEl('[data-status-last-update-relative]');
    var hasVersion = !!s.last_update_version;
    setText(versionEl, s.last_update_version || '');
    setHidden(versionEl, !hasVersion);
    setHidden(sepEl, !hasVersion);
    setText(relEl, s.last_update_at_relative || '—');
    // #335 backward-compat shim: if all three child hooks are absent (the
    // service worker is serving cached HTML from before PR #333 added the
    // three-span structure), fall back to writing the combined string into
    // the parent [data-status-last-update] element so the row populates
    // instead of freezing silently. Defensive + idempotent: when the child
    // hooks DO exist (post-#333 path), this branch is skipped entirely.
    if (!versionEl && !sepEl && !relEl) {
      var parentEl = getEl('[data-status-last-update]');
      var rel = s.last_update_at_relative || '—';
      var combined;
      if (hasVersion) {
        combined = s.last_update_version + ', ' + rel;
      } else {
        combined = rel;
      }
      setText(parentEl, combined);
    }
  }

  function truncate(str, maxLen) {
    if (!str) return '';
    // Match Jinja's `truncate(N, true, "…")`: hard cut at maxLen including
    // the ellipsis, killwords=true so no word-boundary slack.
    //
    // Use Array.from for code-point splits — `str.length` and `str.slice`
    // operate on UTF-16 code units, which split surrogate pairs (4-byte
    // characters like emoji) and yield a corrupt half-glyph in the output.
    // Python's `truncate(18, true, '…')` counts code points; without the
    // Array.from indirection JS and SSR would disagree on SSIDs containing
    // emoji or other non-BMP characters.
    var chars = Array.from(str);
    if (chars.length <= maxLen) return str;
    return chars.slice(0, maxLen - 1).join('') + '…';
  }

  // #309 — graceful disconnect state. Swaps the hero card to a friendly
  // "couldn't reach LitClock" panel with non-technical guidance when
  // /api/status polling fails. Hides on the next successful poll. The
  // [data-status-hero-unreachable] node is SSR-rendered hidden; we just
  // toggle [hidden] so no DOM creation.
  function showUnreachable(show) {
    var heroFull = getEl('[data-status-hero-full]');
    var heroEmpty = getEl('[data-status-hero-empty]');
    var heroUnreachable = getEl('[data-status-hero-unreachable]');
    // #309 /review (design D2): the hero-unreachable retry button and the
    // mdns-bookmark Switch button are both Primary-styled (DESIGN.md
    // locks one Primary action per screen). When the unreachable card
    // appears, suppress the bookmark card so they don't co-render. The
    // mDNS prompt is a nice-to-have and the user can't act on it while
    // the API is unreachable anyway.
    var bookmark = getEl('[data-mdns-bookmark]');
    if (show) {
      setHidden(heroFull, true);
      setHidden(heroEmpty, true);
      setHidden(heroUnreachable, false);
      setHidden(bookmark, true);
    } else {
      setHidden(heroUnreachable, true);
      // Don't restore heroFull / heroEmpty here — the next successful
      // patch() call sets the right one based on s.quote.
      // Don't restore bookmark here either — its lifecycle is owned by
      // probeMdns() + the dismiss/switch handlers + localStorage state.
    }
  }

  var pending = false;
  function refresh() {
    if (pending) return;
    pending = true;
    fetchWithTimeout('/api/status', FETCH_TIMEOUT_MS).then(function (r) {
      // Treat non-OK (5xx, 4xx) as a failure and route to the unreachable
      // UX — server is responding but not in a usable state, which from
      // the user's perspective is indistinguishable from "down."
      if (!r.ok) throw new Error('status ' + r.status);
      return r.json();
    }).then(function (s) {
      if (s && s.ok === true) {
        showUnreachable(false);
        patch(s);
      } else {
        // /review adversarial finding A7: 200-OK with {ok: false} means
        // the server is reachable but not usable, and the user can't
        // act from the PWA. Route through unreachable so they're not
        // stuck on a frozen hero with no feedback. The "Couldn't reach
        // LitClock" framing is more honest than a stale quote that
        // looks fine but won't refresh.
        showUnreachable(true);
      }
    }).catch(function () {
      // Network error / timeout / non-OK. Show the disconnect state on
      // the first failure — DESIGN.md "Empty / loading / error states"
      // line 258 favors immediate, designed feedback over silent retries.
      showUnreachable(true);
    }).then(function () {
      pending = false;
    });
  }

  // Retry button wired via event delegation so the listener survives any
  // future DOM rebuild and works even if the SSR node was missing on first
  // paint (defensive — the template always renders it today). Clears
  // `pending` so the click takes effect even if a 5s-timeout poll is
  // mid-flight (the old promise's `.then(pending = false)` is harmless
  // when we've already reset and started a new request).
  document.addEventListener('click', function (ev) {
    var target = ev.target;
    if (target && target.closest && target.closest('[data-status-retry]')) {
      pending = false;
      refresh();
    }
  });

  // Visibility-aware: refresh immediately when the tab regains focus, so a
  // user coming back from another app sees fresh data instead of stale.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refresh();
  });

  // ─── #309 (3): mDNS bookmark probe ───────────────────────────────
  //
  // After the PWA loads via IP (corner QR scan), see if the phone can
  // also reach the Pi via litclock.local on this network. If yes, the
  // user is offered a one-tap navigation to the address-stable URL so
  // Add-to-Home-Screen will save a bookmark that survives DHCP churn.
  //
  // Probe is a readable (CORS) fetch of litclock.local/api/health and
  // checks the body for `app === 'litclock'` (#487). /api/health sends
  // Access-Control-Allow-Origin: *, so the cross-origin read succeeds only
  // against a real LitClock. This VERIFIES IDENTITY, not just reachability —
  // the earlier opaque `mode:'no-cors'` probe could only tell "something
  // answered on the control port", so a different `.local` device would false-positive
  // the Switch offer. We fail closed: DNS/network failure, CORS block, or a
  // mismatched/absent identity all mean "don't offer the Switch". (The PWA and
  // /api/health always ship together on the same Pi, so there's no version-skew
  // case where the server lacks the CORS header the client expects.)
  //
  // localStorage state machine:
  //   absent      → probe + show on success
  //   'dismissed' → never probe / show
  //   'switched'  → never probe / show (we're now on .local)
  // Storage access wrapped in try/catch per M6 F5 (Safari Private mode).

  function safeLocalStorageGet(key) {
    try { return window.localStorage.getItem(key); }
    catch (e) { return null; }
  }

  function safeLocalStorageSet(key, value) {
    try { window.localStorage.setItem(key, value); }
    catch (e) { /* Private mode / quota — silently degrade. */ }
  }

  function showMdnsBookmark() {
    var card = getEl('[data-mdns-bookmark]');
    if (!card) return;
    setHidden(card, false);
  }

  // Compute "will we actually run the probe?" synchronously at IIFE eval.
  // aths-hint.js reads window.__litclockMdnsPending at its init time to
  // decide whether to defer. We must set/clear this flag BEFORE aths-hint
  // checks it — both scripts load with `defer` on the same page, so
  // ordering by tag position is reliable.
  var willProbeMdns = (
    window.location.hostname !== MDNS_HOST &&
    window.location.protocol === 'http:' &&
    !safeLocalStorageGet(MDNS_STORAGE_KEY)
  );
  if (willProbeMdns) {
    window.__litclockMdnsPending = true;
  }

  function signalMdnsResult(available) {
    // Single-fire signal. Clear the pending flag first so any listener
    // that reads the flag synchronously inside its handler sees the
    // settled state, then dispatch the event for the deferred listeners.
    window.__litclockMdnsPending = false;
    try {
      document.dispatchEvent(new CustomEvent(MDNS_RESULT_EVENT, {
        detail: { available: !!available }
      }));
    } catch (_e) {
      // Older browsers without CustomEvent constructor: aths-hint's
      // defensive timeout fallback will fire AtHS after ~4s anyway.
    }
  }

  function probeMdns() {
    if (!willProbeMdns) {
      // We never set the pending flag, so AtHS already proceeded
      // (or will, on its own DOMContentLoaded path). Nothing to signal.
      return;
    }

    var probeUrl = controlUrl(MDNS_HOST) + '/api/health';
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    // #487: readable (CORS) fetch, NOT no-cors. We must read the body to confirm
    // the responder is THIS LitClock (`app === 'litclock'`), not some other
    // `.local` device that happens to answer on the control port. /api/health sends
    // Access-Control-Allow-Origin: *. Fail closed — any error, non-JSON, or a
    // mismatched/absent identity means we do NOT offer the "Switch" card.
    var opts = { cache: 'no-store' };
    var timer = null;
    if (ctrl) {
      opts.signal = ctrl.signal;
      timer = setTimeout(function () { ctrl.abort(); }, MDNS_PROBE_TIMEOUT_MS);
    }

    fetch(probeUrl, opts).then(function (resp) {
      if (!resp.ok) { throw new Error('mdns health not ok'); }
      return resp.json();
    }).then(function (data) {
      if (data && data.app === 'litclock') {
        showMdnsBookmark();
        signalMdnsResult(true);
      } else {
        // Something answered on litclock.local, but it isn't our clock.
        // Don't offer the Switch — that would strand the user on a stranger's page.
        signalMdnsResult(false);
      }
    }).catch(function () {
      // DNS/network failure, CORS block, aborted, or non-JSON — mDNS not usable.
      signalMdnsResult(false);
    }).then(function () {
      if (timer !== null) clearTimeout(timer);
    });
  }

  // Wire bookmark card buttons. Bound once at load; the card itself is
  // toggled visible by probeMdns(). Direct binding (not delegated) because
  // the buttons live in the SSR'd template and exist at evaluation time.
  var bookmarkCard = getEl('[data-mdns-bookmark]');
  if (bookmarkCard) {
    var switchBtn = bookmarkCard.querySelector('[data-mdns-switch]');
    var dismissBtn = bookmarkCard.querySelector('[data-mdns-dismiss]');
    if (switchBtn) {
      switchBtn.addEventListener('click', function () {
        // Cross-origin navigation — preserve pathname + search so the
        // user lands on the same tab they were on. Mark as 'switched'
        // first so a back-button bounce doesn't re-show the prompt.
        safeLocalStorageSet(MDNS_STORAGE_KEY, 'switched');
        var target = controlUrl(MDNS_HOST)
                   + window.location.pathname + window.location.search;
        window.location.assign(target);
      });
    }
    if (dismissBtn) {
      dismissBtn.addEventListener('click', function () {
        safeLocalStorageSet(MDNS_STORAGE_KEY, 'dismissed');
        setHidden(bookmarkCard, true);
      });
    }
  }

  // Defer the probe past first paint so the user's first impression is
  // the hero quote, not a tip card.
  setTimeout(probeMdns, MDNS_PROBE_DELAY_MS);

  // Kick off a first refresh shortly after first paint so the gap between
  // SSR and the first 30s tick is bounded. Without this, a Pi that
  // publishes a new quote 1s after the page loads keeps the user on stale
  // SSR'd content for up to 30s. requestAnimationFrame defers until after
  // the browser has painted; the existing `pending` flag serializes against
  // the visibilitychange handler so there's no race risk.
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(refresh);
  } else {
    setTimeout(refresh, 0);
  }

  setInterval(refresh, POLL_INTERVAL_MS);
})();
