/* Live-logs drawer (#416 PR3b) — cross-cutting controller.
 *
 * Reachable from every tab via the dots-three ribbon at bottom-right.
 * Consumes the SSE stream from PR2 backend (/api/logs/stream) and the
 * snapshot from /api/logs for backfill.
 *
 * Design decisions: see /plan-design-review for D1–D33.
 *
 *   D2  — 4 empty states: zero entries, zero filter matches, journal
 *         denied (with helper-paste command), stream disconnected.
 *   D6  — div role=dialog aria-modal=false (NON-modal — page stays
 *         scrollable, tabbar interactive). Close: button + Esc +
 *         swipe-down on handle + tap-outside (page-dim).
 *   D7  — Entries in role=log aria-live=polite aria-relevant=additions
 *         aria-atomic=false. Backfill batch goes to a sibling node
 *         silently; only new (post-hello) entries announce.
 *   D8  — Level filter as role=radiogroup with arrow-key handler.
 *   D13 — Open animation translateY(100%→0) over 280ms iOS sheet curve.
 *   D14 — Ribbon hides on visualViewport shrink (keyboard up).
 *   D15 — "Start fresh" filter snapshots latest_seq + drops everything
 *         older from the visible list.
 *   D21 — 200-entry visible cap + 'earlier N hidden' header; follow-tail
 *         with 60pt threshold + '↓ N new' pill when scrolled up.
 *   D25 — First-open welcome card persisted via localStorage
 *         'litclock.diag.welcomed.v1'.
 *   D32 — Non-modal contract: tab bar interactive; tap tab closes
 *         drawer + navigates. inert applied to <main> (not <body>)
 *         when drawer opens, so the tabbar isn't blocked.
 *
 * SSE wire contract (from PR2):
 *   event: hello         — sid + latest_seq baseline (announces "Live")
 *   event: entry         — { seq, timestamp, level, logger, message }
 *   event: heartbeat     — { t }     (every 15s, no UI change)
 *   event: superseded    — { sid }   (same-sid replace; UI says "Stream replaced")
 *   event: capacity-exceeded — { sid } (LRU evicted; UI backs off + retries)
 *   event: timeout       — { sid }   (5min server-side cap; auto-reconnect)
 *   event: error         — { code }  (mostly log_buffer_unavailable)
 */

(function () {
  'use strict';

  var WELCOMED_KEY = 'litclock.diag.welcomed.v1';
  var SID_KEY = 'litclock.diag.sid';
  var VISIBLE_CAP = 200;
  var FOLLOW_TAIL_THRESHOLD_PX = 60;
  var BACKOFF_BASE_MS = 1000;
  var BACKOFF_MAX_MS = 30000;
  // EventSource reconnect spacing AFTER a capacity-exceeded close. The
  // server LRU-evicts the oldest when 7+ tabs/clients try to connect; if
  // the client tight-loops it churns the cap and evicts a peer. 5s gives
  // other clients a window to do useful work.
  var CAPACITY_BACKOFF_MS = 5000;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /* ----- Welcome card (D25) -------------------------------------------- */

  function welcomeAlreadySeen() {
    try { return window.localStorage.getItem(WELCOMED_KEY) === '1'; }
    catch (e) { return false; }
  }
  function markWelcomed() {
    try { window.localStorage.setItem(WELCOMED_KEY, '1'); } catch (e) { /* ignore */ }
  }

  /* ----- SID (per-tab UUID for the SSE supersession contract) ---------- */

  function getOrCreateSid() {
    try {
      var existing = window.sessionStorage.getItem(SID_KEY);
      if (existing && existing.length >= 4 && existing.length <= 128) return existing;
    } catch (e) { /* fall through */ }
    var sid = 'litclock-' + Math.random().toString(36).slice(2, 14) + '-' + Date.now().toString(36);
    try { window.sessionStorage.setItem(SID_KEY, sid); } catch (e) { /* ignore */ }
    return sid;
  }

  /* ----- DOM cache ----------------------------------------------------- */

  function snapshotDom() {
    return {
      ribbon: $('[data-diag-ribbon-button]'),
      drawer: $('[data-diag-drawer]'),
      pageDim: $('[data-diag-page-dim]'),
      closeBtn: $('[data-diag-drawer-close]'),
      handle: $('[data-diag-drawer-handle]'),
      welcome: $('[data-diag-drawer-welcome]'),
      welcomeDismiss: $('[data-diag-drawer-welcome-dismiss]'),
      entries: $('[data-diag-drawer-entries]'),
      filters: $$('.diag-level-filter__option'),
      fresh: $('[data-diag-drawer-fresh]'),
      emptyNoEntries: $('[data-diag-drawer-empty="no-entries"]'),
      emptyNoMatches: $('[data-diag-drawer-empty="no-matches"]'),
      emptyJournalDenied: $('[data-diag-drawer-empty="journal-denied"]'),
      emptyDisconnected: $('[data-diag-drawer-empty="disconnected"]'),
      hiddenBatch: $('[data-diag-drawer-hidden-batch]'),
      followPill: $('[data-diag-drawer-follow-pill]'),
      followCount: $('[data-diag-drawer-follow-count]'),
      main: document.querySelector('main'),
    };
  }

  /* ----- Open/close (D6 + D13 + D32) ----------------------------------- */

  function openDrawer(dom, state) {
    if (state.open) return;
    state.open = true;
    // /review F-CLOSE-RACE: a fast close→reopen would let the stale
    // setTimeout from the previous close re-hide the now-open drawer.
    // Cancel any pending close-animation timeout and clear the closing
    // class so the slide-up animation isn't interrupted mid-frame.
    if (state.closeAnimationTimer) {
      clearTimeout(state.closeAnimationTimer);
      state.closeAnimationTimer = null;
      dom.drawer.removeAttribute('data-diag-closing');
    }
    document.body.setAttribute('data-diag-drawer-open', '');
    dom.drawer.hidden = false;
    dom.drawer.removeAttribute('inert');
    dom.drawer.setAttribute('aria-hidden', 'false');
    // D32 inert on <main>, NOT body — keeps the tabbar interactive so
    // tapping a tab closes the drawer AND navigates.
    if (dom.main) {
      if ('inert' in dom.main) dom.main.inert = true;
      dom.main.setAttribute('inert', '');
    }
    // First-open welcome card — only shows if user hasn't dismissed.
    if (dom.welcome) {
      dom.welcome.hidden = welcomeAlreadySeen();
    }
    if (dom.closeBtn) dom.closeBtn.focus();
    ensureStream(dom, state);
    refreshEmptyStates(dom, state);
  }

  function closeDrawer(dom, state) {
    if (!state.open) return;
    state.open = false;
    document.body.removeAttribute('data-diag-drawer-open');
    dom.drawer.setAttribute('data-diag-closing', 'true');
    // /review F-CLOSE-RACE: track the timeout handle on state so a
    // fast re-open can cancel it.
    state.closeAnimationTimer = setTimeout(function () {
      state.closeAnimationTimer = null;
      if (state.open) return; // user reopened mid-animation; bail out
      dom.drawer.hidden = true;
      dom.drawer.removeAttribute('data-diag-closing');
      dom.drawer.setAttribute('inert', '');
      dom.drawer.setAttribute('aria-hidden', 'true');
    }, 240);
    if (dom.main) {
      if ('inert' in dom.main) dom.main.inert = false;
      dom.main.removeAttribute('inert');
    }
    // /review F-SSE-LEAK: detach the SSE stream when the drawer closes
    // so we don't hold one of the 6-client server cap slots forever.
    // state.buffer + state.lastSeq stay so a reopen does an incremental
    // since_seq=lastSeq reconnect for gap-fill.
    closeStream(state);
    state.disconnected = false; // cancel any 'disconnected' empty state
    // Return focus to the ribbon button per D6.
    if (dom.ribbon) dom.ribbon.focus();
  }

  /* ----- Level filter (D8) --------------------------------------------- */

  function applyLevelFilter(dom, state, level) {
    state.level = level;
    dom.filters.forEach(function (btn) {
      var match = btn.getAttribute('data-diag-level') === level;
      btn.setAttribute('aria-checked', match ? 'true' : 'false');
    });
    rerenderEntries(dom, state);
  }

  function setupLevelKeyboardNav(dom) {
    dom.filters.forEach(function (btn, idx) {
      btn.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        var next = e.key === 'ArrowRight'
          ? (idx + 1) % dom.filters.length
          : (idx - 1 + dom.filters.length) % dom.filters.length;
        dom.filters[next].focus();
        dom.filters[next].click();
      });
    });
  }

  /* ----- Clipboard helpers (D10 mirror) -------------------------------- */

  function formatEntryRowForCopy(li) {
    var t = li.querySelector('.diag-drawer__entry-time');
    var l = li.querySelector('.diag-drawer__entry-level');
    var m = li.querySelector('.diag-drawer__entry-msg');
    return (
      (t ? t.textContent.trim() + ' ' : '') +
      (l ? l.textContent.trim() + ' ' : '') +
      (m ? m.textContent.trim() : '')
    ).trim();
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
      } catch (e) { reject(e); }
    });
  }

  /* ----- Entry rendering (D7 + D21) ------------------------------------ */

  function makeEntryElement(entry) {
    var li = document.createElement('li');
    li.className = 'diag-drawer__entry diag-drawer__entry--' + (entry.level || 'INFO').toLowerCase();
    li.setAttribute('data-diag-drawer-entry-seq', String(entry.seq || 0));

    var t = document.createElement('span');
    t.className = 'diag-drawer__entry-time';
    if (typeof entry.timestamp === 'number') {
      var d = new Date(entry.timestamp * 1000);
      // /review F-UTC-TIME: the e-ink hero quote uses local time per
      // status.html.j2. Rendering UTC here makes owner-persona users
      // ask why the log time doesn't match the clock. Use local time.
      var hh = String(d.getHours()).padStart(2, '0');
      var mm = String(d.getMinutes()).padStart(2, '0');
      var ss = String(d.getSeconds()).padStart(2, '0');
      t.textContent = hh + ':' + mm + ':' + ss;
    }
    li.appendChild(t);

    var l = document.createElement('span');
    l.className = 'diag-drawer__entry-level';
    l.textContent = entry.level || 'INFO';
    li.appendChild(l);

    var m = document.createElement('span');
    m.className = 'diag-drawer__entry-msg';
    m.textContent = entry.message || '';
    li.appendChild(m);

    var copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'diag-drawer__entry-copy';
    copyBtn.setAttribute('data-diag-drawer-entry-copy', '');
    // /review F-COPY-UNWIRED: replace the '⧉' Unicode glyph with an
    // inline Phosphor `copy` SVG (DESIGN.md icon set), and wire a real
    // click handler via boot()'s delegated listener.
    copyBtn.innerHTML = ''; // textContent: '' is the safer move pre-spec, but we need an SVG child.
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 256 256');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M216,32H88a8,8,0,0,0-8,8V80H40a8,8,0,0,0-8,8V216a8,8,0,0,0,8,8H168a8,8,0,0,0,8-8V176h40a8,8,0,0,0,8-8V40A8,8,0,0,0,216,32ZM160,208H48V96H160Zm48-48H176V88a8,8,0,0,0-8-8H96V48H208Z');
    svg.appendChild(path);
    copyBtn.appendChild(svg);
    li.appendChild(copyBtn);

    return li;
  }

  function rerenderEntries(dom, state) {
    if (!dom.entries) return;
    while (dom.entries.firstChild) dom.entries.removeChild(dom.entries.firstChild);
    var visible = filterEntries(state.buffer, state);
    // D21 cap: keep only the most recent VISIBLE_CAP. Older ones surface
    // via the hidden-batch header. The fresh-from-now filter also lives
    // in filterEntries above so the cap applies AFTER the user's intent.
    var hiddenCount = Math.max(0, visible.length - VISIBLE_CAP);
    var slice = hiddenCount > 0 ? visible.slice(-VISIBLE_CAP) : visible;
    slice.forEach(function (entry) {
      dom.entries.appendChild(makeEntryElement(entry));
    });
    if (dom.hiddenBatch) {
      if (hiddenCount > 0) {
        dom.hiddenBatch.textContent = 'Earlier ' + hiddenCount + ' hidden — open the full diagnostics page to see all';
        dom.hiddenBatch.hidden = false;
      } else {
        dom.hiddenBatch.hidden = true;
      }
    }
    refreshEmptyStates(dom, state);
  }

  function filterEntries(buffer, state) {
    var startSeq = state.freshFromSeq || 0;
    var out = [];
    for (var i = 0; i < buffer.length; i++) {
      var e = buffer[i];
      if (e.seq < startSeq) continue;
      if (state.level && e.level !== state.level) continue;
      out.push(e);
    }
    return out;
  }

  function appendEntry(dom, state, entry) {
    state.buffer.push(entry);
    // Bound the in-memory buffer to MAX_ENTRIES + slack so this doesn't
    // leak across a 6-hour drawer session.
    if (state.buffer.length > 500) state.buffer.shift();
    var passesFilter = !state.level || entry.level === state.level;
    var afterFresh = !state.freshFromSeq || entry.seq >= state.freshFromSeq;
    if (!passesFilter || !afterFresh) {
      refreshEmptyStates(dom, state);
      return;
    }
    if (!isFollowingTail(dom)) {
      state.unseenCount += 1;
      showFollowPill(dom, state);
      return;
    }
    dom.entries.appendChild(makeEntryElement(entry));
    // Enforce the visible cap incrementally.
    while (dom.entries.children.length > VISIBLE_CAP) {
      dom.entries.removeChild(dom.entries.firstChild);
    }
    refreshEmptyStates(dom, state);
    autoScrollTail(dom);
  }

  function isFollowingTail(dom) {
    if (!dom.entries) return true;
    var el = dom.entries;
    return (el.scrollHeight - el.scrollTop - el.clientHeight) < FOLLOW_TAIL_THRESHOLD_PX;
  }
  function prefersReducedMotion() {
    try {
      return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) { return false; }
  }
  function autoScrollTail(dom) {
    // /review F-AUTOSCROLL: D11 locks "auto-scroll → disabled" under
    // prefers-reduced-motion. Without this gate every new entry caused
    // a hard scroll jump for owner-persona users who set Larger Text +
    // Reduce Motion together (common iOS pairing).
    if (prefersReducedMotion()) return;
    if (dom.entries) dom.entries.scrollTop = dom.entries.scrollHeight;
  }

  function showFollowPill(dom, state) {
    if (!dom.followPill || !dom.followCount) return;
    dom.followCount.textContent = String(state.unseenCount);
    dom.followPill.hidden = false;
  }
  function hideFollowPill(dom, state) {
    state.unseenCount = 0;
    if (dom.followPill) dom.followPill.hidden = true;
  }

  /* ----- Empty states (D2 + F9) ---------------------------------------- */

  function refreshEmptyStates(dom, state) {
    if (!dom.entries) return;
    var hasEntries = state.buffer.length > 0;
    var visibleCount = filterEntries(state.buffer, state).length;
    // Reset all empties.
    [dom.emptyNoEntries, dom.emptyNoMatches, dom.emptyJournalDenied, dom.emptyDisconnected]
      .forEach(function (el) { if (el) el.hidden = true; });
    if (state.disconnected) {
      if (dom.emptyDisconnected) dom.emptyDisconnected.hidden = false;
      return;
    }
    if (state.journalDenied) {
      if (dom.emptyJournalDenied) dom.emptyJournalDenied.hidden = false;
      return;
    }
    if (!hasEntries) {
      if (dom.emptyNoEntries) dom.emptyNoEntries.hidden = false;
      return;
    }
    if (visibleCount === 0) {
      if (dom.emptyNoMatches) dom.emptyNoMatches.hidden = false;
    }
  }

  /* ----- SSE stream (PR2 wire contract) -------------------------------- */

  function ensureStream(dom, state) {
    if (state.eventSource) return;
    openStream(dom, state);
  }

  function openStream(dom, state) {
    var sid = state.sid || (state.sid = getOrCreateSid());
    // /review F-FIRST-BACKFILL: pre-/review we sent NO since_seq on
    // first open, so the backend's `if since_seq is not None and
    // since_seq >= 0` backfill branch never ran. Owner persona opened
    // the drawer expecting recent context and saw 'It's quiet' even
    // when ERROR-level entries were already in the buffer. Send
    // since_seq=0 on first connect to trigger the snapshot replay.
    // Reconnects carry the last-seen seq for incremental gap-fill.
    var sinceParam = state.lastSeq > 0 ? state.lastSeq : 0;
    var url = '/api/logs/stream?sid=' + encodeURIComponent(sid) +
              '&since_seq=' + sinceParam;
    var es;
    try {
      es = new EventSource(url, { withCredentials: false });
    } catch (err) {
      scheduleReconnect(dom, state, BACKOFF_BASE_MS);
      return;
    }
    state.eventSource = es;
    state.disconnected = false;
    refreshEmptyStates(dom, state);

    es.addEventListener('hello', function (e) {
      try {
        var payload = JSON.parse(e.data);
        // Reset backoff on a successful hello.
        state.backoffMs = BACKOFF_BASE_MS;
        if (typeof payload.latest_seq === 'number') {
          state.lastSeq = Math.max(state.lastSeq, payload.latest_seq);
        }
      } catch (err) { /* ignore */ }
    });
    es.addEventListener('entry', function (e) {
      try {
        var entry = JSON.parse(e.data);
        state.lastSeq = Math.max(state.lastSeq, entry.seq || 0);
        appendEntry(dom, state, entry);
      } catch (err) { /* ignore */ }
    });
    es.addEventListener('heartbeat', function () { /* keep-alive only */ });
    es.addEventListener('superseded', function () {
      // Another tab in THIS browser took over the sid. Stop streaming
      // and let the user know quietly. Don't auto-reconnect because the
      // newer session is the source of truth.
      closeStream(state);
      state.disconnected = true;
      refreshEmptyStates(dom, state);
    });
    es.addEventListener('capacity-exceeded', function () {
      // 7th+ client; the server LRU-evicted us. Back off longer than the
      // usual reconnect so we don't churn other clients out of their slots.
      closeStream(state);
      state.disconnected = true;
      refreshEmptyStates(dom, state);
      scheduleReconnect(dom, state, CAPACITY_BACKOFF_MS);
    });
    es.addEventListener('timeout', function () {
      // 5min server cap; clean reconnect.
      closeStream(state);
      scheduleReconnect(dom, state, BACKOFF_BASE_MS);
    });
    es.addEventListener('error', function (e) {
      // Server sent the named error event OR the EventSource transport
      // hit a transport-level error. Either way: disconnect + retry.
      if (e && typeof e.data === 'string') {
        try {
          var payload = JSON.parse(e.data);
          if (payload && payload.code === 'log_buffer_unavailable') {
            // Server says the buffer isn't initialised. Stop retrying
            // forever — show a journal-denied-style empty state.
            closeStream(state);
            state.disconnected = true;
            refreshEmptyStates(dom, state);
            return;
          }
        } catch (err) { /* ignore */ }
      }
      closeStream(state);
      state.disconnected = true;
      refreshEmptyStates(dom, state);
      scheduleReconnect(dom, state, state.backoffMs);
      // Exponential backoff with max.
      state.backoffMs = Math.min(state.backoffMs * 2, BACKOFF_MAX_MS);
    });
  }

  function closeStream(state) {
    if (state.eventSource) {
      try { state.eventSource.close(); } catch (e) { /* ignore */ }
      state.eventSource = null;
    }
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function scheduleReconnect(dom, state, delayMs) {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(function () {
      state.reconnectTimer = null;
      // /review F-RECONNECT-RACE: only reconnect if (a) the drawer is
      // still open AND (b) no stream is currently active. Pre-/review
      // a fast close-then-reopen could spawn two same-sid EventSources;
      // the old one's stale 'superseded' event would close the newer
      // active stream.
      if (state.open && !state.eventSource) openStream(dom, state);
    }, delayMs);
  }

  /* ----- D14: visualViewport ribbon hide ------------------------------ */

  function setupViewportListener() {
    if (typeof window.visualViewport === 'undefined') return null;
    var baseline = window.visualViewport.height;
    var handler = function () {
      var shrunk = window.visualViewport.height < baseline * 0.75;
      document.body.toggleAttribute('data-diag-ribbon-hidden', shrunk);
    };
    window.visualViewport.addEventListener('resize', handler);
    return function () {
      window.visualViewport.removeEventListener('resize', handler);
    };
  }

  /* ----- Swipe-down handler (D6) -------------------------------------- */

  function setupSwipeDown(dom, state) {
    if (!dom.handle) return null;
    var startY = null;
    function start(e) {
      startY = (e.touches ? e.touches[0].clientY : e.clientY);
    }
    function move(e) {
      if (startY === null) return;
      var y = (e.touches ? e.touches[0].clientY : e.clientY);
      if (y - startY > 60) {
        startY = null;
        closeDrawer(dom, state);
      }
    }
    function end() { startY = null; }
    dom.handle.addEventListener('touchstart', start, { passive: true });
    dom.handle.addEventListener('touchmove', move, { passive: true });
    dom.handle.addEventListener('touchend', end);
    return function () {
      dom.handle.removeEventListener('touchstart', start);
      dom.handle.removeEventListener('touchmove', move);
      dom.handle.removeEventListener('touchend', end);
    };
  }

  /* ----- Tab-tap closes drawer (D32) ----------------------------------- */

  function setupTabbarClose(dom, state) {
    var tabbar = document.querySelector('.tabbar');
    if (!tabbar) return null;
    var handler = function () {
      if (state.open) closeDrawer(dom, state);
      // Navigation proceeds — we don't preventDefault.
    };
    tabbar.addEventListener('click', handler, true);
    return function () { tabbar.removeEventListener('click', handler, true); };
  }

  /* ----- Lifecycle ----------------------------------------------------- */

  function boot() {
    var dom = snapshotDom();
    if (!dom.drawer || !dom.ribbon) {
      // Page doesn't render the drawer markup (defensive).
      return;
    }
    var state = {
      open: false,
      sid: null,
      buffer: [],
      level: '',
      freshFromSeq: 0,
      lastSeq: 0,
      eventSource: null,
      reconnectTimer: null,
      backoffMs: BACKOFF_BASE_MS,
      disconnected: false,
      journalDenied: false,
      unseenCount: 0,
    };

    // Ribbon open.
    dom.ribbon.addEventListener('click', function () { openDrawer(dom, state); });

    // Close button + page-dim tap-outside + Esc.
    if (dom.closeBtn) dom.closeBtn.addEventListener('click', function () { closeDrawer(dom, state); });
    if (dom.pageDim) dom.pageDim.addEventListener('click', function () { closeDrawer(dom, state); });
    var escHandler = function (e) {
      if (e.key === 'Escape' && state.open) closeDrawer(dom, state);
    };
    document.addEventListener('keydown', escHandler);

    // Welcome card dismiss.
    if (dom.welcomeDismiss) {
      dom.welcomeDismiss.addEventListener('click', function () {
        if (dom.welcome) dom.welcome.hidden = true;
        markWelcomed();
      });
    }

    // Filter clicks.
    dom.filters.forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyLevelFilter(dom, state, btn.getAttribute('data-diag-level') || '');
      });
    });
    setupLevelKeyboardNav(dom);

    // Fresh-from-now.
    if (dom.fresh) {
      dom.fresh.addEventListener('click', function () {
        state.freshFromSeq = state.lastSeq + 1;
        dom.fresh.disabled = true;
        dom.fresh.textContent = 'Fresh from now';
        rerenderEntries(dom, state);
      });
    }

    // Follow-tail pill click → scroll to bottom + reset.
    if (dom.followPill) {
      dom.followPill.addEventListener('click', function () {
        autoScrollTail(dom);
        hideFollowPill(dom, state);
      });
    }

    // Scroll resets the unseen count once the user reaches the tail.
    if (dom.entries) {
      dom.entries.addEventListener('scroll', function () {
        if (isFollowingTail(dom)) hideFollowPill(dom, state);
      });
    }

    // /review F-COPY-UNWIRED: per-entry copy button. Delegated on the
    // entries <ol> so newly-appended rows inherit the handler without
    // re-binding. Reads time + level + message from the row's spans.
    if (dom.entries) {
      dom.entries.addEventListener('click', function (e) {
        var btn = e.target && e.target.closest && e.target.closest('[data-diag-drawer-entry-copy]');
        if (!btn) return;
        e.preventDefault();
        var li = btn.closest('.diag-drawer__entry');
        if (!li) return;
        var text = formatEntryRowForCopy(li);
        copyToClipboard(text)
          .then(function () {
            btn.setAttribute('data-diag-drawer-copy-success', 'true');
            setTimeout(function () { btn.removeAttribute('data-diag-drawer-copy-success'); }, 1500);
          })
          .catch(function () { /* silent — no toast surface yet */ });
      });
    }

    // Cross-cutting integration: PR3a's disabled "Open live drawer →"
    // button on /diagnostics. When the drawer ships (now), wire the
    // button to open() and remove the disabled state.
    var pageDrawerLink = document.querySelector('[data-diag-open-drawer]');
    if (pageDrawerLink) {
      pageDrawerLink.removeAttribute('disabled');
      pageDrawerLink.removeAttribute('aria-disabled');
      pageDrawerLink.removeAttribute('title');
      pageDrawerLink.addEventListener('click', function (e) {
        e.preventDefault();
        openDrawer(dom, state);
      });
      // Hide the PR3a "(Live drawer arrives in #416 PR3b.)" hint since
      // it's no longer accurate.
      var pending = document.querySelector('.diag-log-snapshot__pending');
      if (pending) pending.hidden = true;
    }

    var teardownVp = setupViewportListener();
    var teardownSwipe = setupSwipeDown(dom, state);
    var teardownTabbar = setupTabbarClose(dom, state);

    // Test seam — same pattern as diagnostics.js, see PR3a F-LISTENER-STACK.
    window.__litclockDrawer = {
      teardown: function () {
        document.removeEventListener('keydown', escHandler);
        if (teardownVp) teardownVp();
        if (teardownSwipe) teardownSwipe();
        if (teardownTabbar) teardownTabbar();
        closeStream(state);
        if (state.open) closeDrawer(dom, state);
        delete window.__litclockDrawer;
      },
      // Expose minimal handles for tests; production code should never
      // touch these.
      _state: state,
      _dom: dom,
      _open: function () { openDrawer(dom, state); },
      _close: function () { closeDrawer(dom, state); },
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
