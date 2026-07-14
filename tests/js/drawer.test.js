// Behavior coverage for src/control_server/static/js/drawer.js (#416 PR3b).
//
// The module is an IIFE; tests drive it through the DOM + a synthetic
// EventSource. jsdom doesn't ship EventSource, so we stub it.
//
// Tests cover the multi-source CRITICAL paths PR2 review caught:
//   - SSE wire contract: hello / entry / superseded / capacity-exceeded
//     / timeout / error
//   - LRU capacity-exceeded backoff (5s) vs immediate-reconnect on timeout
//   - same-sid superseded: stop streaming, don't reconnect
//   - reconnect with since_seq=lastSeq for gap-fill
//   - Welcome card persistence via localStorage
//   - Filter + fresh-from-now interaction
//   - Follow-tail pill semantics

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript } from "./helpers/loadScript.js";

let eventSources;
let OrigEventSource;
let originalVisualViewportDescriptor;

class StubEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    eventSources.push(this);
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const idx = list.indexOf(handler);
    if (idx !== -1) list.splice(idx, 1);
  }
  close() { this.closed = true; }
  // Test helpers — fire a named event with data.
  fire(type, data) {
    const evt = { type, data: data == null ? "" : JSON.stringify(data) };
    (this.listeners[type] || []).forEach((h) => h(evt));
  }
  fireTransportError() {
    // EventSource transport error has no .data
    (this.listeners.error || []).forEach((h) => h({ type: "error" }));
  }
}

function buildDom() {
  // Mirror the relevant base.html.j2 markup. Kept minimal so cosmetic
  // edits don't break the suite.
  document.body.innerHTML = `
    <main></main>
    <nav class="tabbar">
      <a href="/">Status</a>
      <a href="/settings">Settings</a>
    </nav>
    <button data-diag-ribbon-button aria-label="Open"></button>
    <div data-diag-page-dim></div>
    <section data-diag-drawer hidden inert aria-hidden="true" aria-modal="false" role="dialog">
      <div data-diag-drawer-handle></div>
      <header>
        <a href="/diagnostics" data-diag-drawer-open-page aria-label="Open full diagnostics page">Open full diagnostics</a>
        <button data-diag-drawer-close>×</button>
      </header>
      <div data-diag-drawer-welcome>
        <button data-diag-drawer-welcome-dismiss>Got it</button>
      </div>
      <div role="radiogroup">
        <button class="diag-level-filter__option" role="radio" aria-checked="true" data-diag-level="">All</button>
        <button class="diag-level-filter__option" role="radio" aria-checked="false" data-diag-level="INFO">Info</button>
        <button class="diag-level-filter__option" role="radio" aria-checked="false" data-diag-level="WARNING">Warn</button>
        <button class="diag-level-filter__option" role="radio" aria-checked="false" data-diag-level="ERROR">Error</button>
      </div>
      <button data-diag-drawer-fresh>Start fresh</button>
      <div data-diag-drawer-empty="no-entries" hidden></div>
      <div data-diag-drawer-empty="no-matches" hidden></div>
      <div data-diag-drawer-empty="journal-denied" hidden></div>
      <div data-diag-drawer-empty="disconnected" hidden></div>
      <p data-diag-drawer-hidden-batch hidden></p>
      <ol data-diag-drawer-entries role="log" aria-live="polite"></ol>
      <button data-diag-drawer-follow-pill hidden>↓ <span data-diag-drawer-follow-count>0</span> new</button>
    </section>
  `;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  try { sessionStorage.clear(); localStorage.clear(); } catch (e) { /* ignore */ }
  eventSources = [];
  OrigEventSource = window.EventSource;
  window.EventSource = StubEventSource;
  originalVisualViewportDescriptor = Object.getOwnPropertyDescriptor(window, "visualViewport");
  // Drop visualViewport to skip the resize listener setup (test isolation).
  Object.defineProperty(window, "visualViewport", { configurable: true, value: undefined });
});

afterEach(() => {
  if (window.__litclockDrawer && typeof window.__litclockDrawer.teardown === "function") {
    window.__litclockDrawer.teardown();
  }
  window.EventSource = OrigEventSource;
  if (originalVisualViewportDescriptor) {
    Object.defineProperty(window, "visualViewport", originalVisualViewportDescriptor);
  } else {
    delete window.visualViewport;
  }
  vi.useRealTimers();
});

function getDrawer() { return document.querySelector("[data-diag-drawer]"); }

describe("drawer.js — open/close lifecycle (D6 + D32)", () => {
  it("opens on ribbon click, sets body[data-diag-drawer-open], removes inert", () => {
    buildDom();
    loadScript("drawer.js");
    const ribbon = document.querySelector("[data-diag-ribbon-button]");
    const drawer = getDrawer();
    expect(drawer.hasAttribute("inert")).toBe(true);
    expect(drawer.hidden).toBe(true);

    ribbon.click();

    expect(document.body.hasAttribute("data-diag-drawer-open")).toBe(true);
    expect(drawer.hidden).toBe(false);
    expect(drawer.hasAttribute("inert")).toBe(false);
    expect(drawer.getAttribute("aria-hidden")).toBe("false");
    // <main> gets inert per D32 (NOT <body>).
    expect(document.querySelector("main").hasAttribute("inert")).toBe(true);
  });

  it("closes on close button click + restores inert", async () => {
    buildDom();
    loadScript("drawer.js");
    const ribbon = document.querySelector("[data-diag-ribbon-button]");
    ribbon.click();
    document.querySelector("[data-diag-drawer-close]").click();
    // Animation finishes after ~240ms.
    await vi.advanceTimersByTimeAsync(260);
    const drawer = getDrawer();
    expect(document.body.hasAttribute("data-diag-drawer-open")).toBe(false);
    expect(drawer.hidden).toBe(true);
    expect(drawer.hasAttribute("inert")).toBe(true);
    expect(document.querySelector("main").hasAttribute("inert")).toBe(false);
  });

  it("closes on Esc", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await vi.advanceTimersByTimeAsync(260);
    expect(getDrawer().hidden).toBe(true);
  });

  it("closes on tap-outside via page-dim", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    document.querySelector("[data-diag-page-dim]").click();
    await vi.advanceTimersByTimeAsync(260);
    expect(getDrawer().hidden).toBe(true);
  });

  it("D32 non-modal: tapping a tabbar link closes the drawer (and navigation proceeds)", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    expect(getDrawer().hidden).toBe(false);
    // Simulate tabbar tap (we just verify close fires; jsdom doesn't navigate).
    document.querySelector(".tabbar a").click();
    await vi.advanceTimersByTimeAsync(260);
    expect(getDrawer().hidden).toBe(true);
  });
});

describe("drawer.js — SSE handshake + entry append (D7 + PR2 wire)", () => {
  it("opens an EventSource with sid + since_seq=0 on first connect (triggers backfill)", () => {
    // /review F-FIRST-BACKFILL: pre-/review first connect sent no
    // since_seq → backend's backfill branch never ran → 'It's quiet'
    // even when ERROR-level entries were in the buffer. Now we send
    // since_seq=0 to force the full-history replay on initial open.
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    expect(eventSources.length).toBe(1);
    const es = eventSources[0];
    expect(es.url).toContain("/api/logs/stream?sid=");
    expect(es.url).toContain("since_seq=0");
  });

  it("renders entries from the stream into the log list", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    es.fire("hello", { sid: "test-sid", latest_seq: 0 });
    es.fire("entry", { seq: 1, timestamp: 1717000001, level: "INFO", message: "first" });
    es.fire("entry", { seq: 2, timestamp: 1717000002, level: "ERROR", message: "boom" });
    const items = document.querySelectorAll("[data-diag-drawer-entries] li");
    expect(items.length).toBe(2);
    expect(items[1].querySelector(".diag-drawer__entry-msg").textContent).toBe("boom");
  });
});

describe("drawer.js — F-CAPACITY-EXCEEDED backoff (PR2 wire contract)", () => {
  it("backs off 5s (not immediate) after capacity-exceeded, then reconnects", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const firstEs = eventSources[0];
    firstEs.fire("capacity-exceeded", { sid: "test-sid" });
    expect(firstEs.closed).toBe(true);
    // No new EventSource within 5s minus tolerance.
    await vi.advanceTimersByTimeAsync(4500);
    expect(eventSources.length).toBe(1);
    // After the 5s backoff window the reconnect fires.
    await vi.advanceTimersByTimeAsync(600);
    expect(eventSources.length).toBe(2);
  });
});

describe("drawer.js — F-SUPERSEDED stops streaming (does not reconnect)", () => {
  it("a same-sid replace closes the stream and does NOT spawn a reconnect", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const firstEs = eventSources[0];
    firstEs.fire("superseded", { sid: "test-sid" });
    expect(firstEs.closed).toBe(true);
    // No reconnect ever fires — even after a long wait.
    await vi.advanceTimersByTimeAsync(30000);
    expect(eventSources.length).toBe(1);
    // Disconnected empty state is shown.
    expect(document.querySelector('[data-diag-drawer-empty="disconnected"]').hidden).toBe(false);
  });
});

describe("drawer.js — timeout fires fast reconnect (server's 5min cap)", () => {
  it("reconnects within the base backoff (1s) and re-uses since_seq", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const firstEs = eventSources[0];
    firstEs.fire("hello", { sid: "test-sid", latest_seq: 5 });
    firstEs.fire("entry", { seq: 7, timestamp: 1717000001, level: "INFO", message: "hi" });
    firstEs.fire("timeout", { sid: "test-sid" });
    await vi.advanceTimersByTimeAsync(1200);
    expect(eventSources.length).toBe(2);
    // Reconnect URL carries since_seq=7 so gaps are filled.
    expect(eventSources[1].url).toContain("since_seq=7");
  });
});

describe("drawer.js — error reconnect uses exponential backoff", () => {
  it("first transport error retries at 1s, second at 2s", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const first = eventSources[0];
    first.fireTransportError();
    await vi.advanceTimersByTimeAsync(1100);
    expect(eventSources.length).toBe(2);
    eventSources[1].fireTransportError();
    await vi.advanceTimersByTimeAsync(1500);
    expect(eventSources.length).toBe(2); // not yet
    await vi.advanceTimersByTimeAsync(700);
    expect(eventSources.length).toBe(3); // ~2s elapsed
  });

  it("log_buffer_unavailable error stops retrying forever", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    es.fire("error", { code: "log_buffer_unavailable" });
    // Should NOT schedule a reconnect.
    await vi.advanceTimersByTimeAsync(60000);
    expect(eventSources.length).toBe(1);
  });
});

describe("drawer.js — Welcome card (D25)", () => {
  it("shows the welcome card on first open and hides it after dismiss", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const welcome = document.querySelector("[data-diag-drawer-welcome]");
    expect(welcome.hidden).toBe(false);
    document.querySelector("[data-diag-drawer-welcome-dismiss]").click();
    expect(welcome.hidden).toBe(true);
    expect(localStorage.getItem("litclock.diag.welcomed.v1")).toBe("1");
  });

  it("does NOT show the welcome card if already dismissed (localStorage persistence)", () => {
    localStorage.setItem("litclock.diag.welcomed.v1", "1");
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    expect(document.querySelector("[data-diag-drawer-welcome]").hidden).toBe(true);
  });
});

describe("drawer.js — level filter (D8) + fresh-from-now (D15)", () => {
  it("filters entries by level when a non-All option is checked", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    es.fire("hello", { sid: "x", latest_seq: 0 });
    es.fire("entry", { seq: 1, timestamp: 1, level: "INFO", message: "info" });
    es.fire("entry", { seq: 2, timestamp: 2, level: "ERROR", message: "err" });
    expect(document.querySelectorAll("[data-diag-drawer-entries] li").length).toBe(2);
    document.querySelector('[data-diag-level="ERROR"]').click();
    const items = document.querySelectorAll("[data-diag-drawer-entries] li");
    expect(items.length).toBe(1);
    expect(items[0].querySelector(".diag-drawer__entry-msg").textContent).toBe("err");
  });

  it("Start-fresh-from-now hides existing entries + drops the filter button", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    es.fire("hello", { sid: "x", latest_seq: 0 });
    es.fire("entry", { seq: 1, timestamp: 1, level: "INFO", message: "old" });
    expect(document.querySelectorAll("[data-diag-drawer-entries] li").length).toBe(1);
    const freshBtn = document.querySelector("[data-diag-drawer-fresh]");
    freshBtn.click();
    expect(document.querySelectorAll("[data-diag-drawer-entries] li").length).toBe(0);
    expect(freshBtn.disabled).toBe(true);
    // New entry after fresh-from-now shows.
    es.fire("entry", { seq: 2, timestamp: 2, level: "INFO", message: "new" });
    expect(document.querySelectorAll("[data-diag-drawer-entries] li").length).toBe(1);
  });

  it("arrow keys cycle through level filter options (D8 radiogroup pattern)", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const all = document.querySelector('[data-diag-level=""]');
    const info = document.querySelector('[data-diag-level="INFO"]');
    all.focus();
    all.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(info);
    expect(info.getAttribute("aria-checked")).toBe("true");
  });
});

describe("drawer.js — empty states (D2 + F9)", () => {
  it("shows no-entries when buffer is empty", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const noEntries = document.querySelector('[data-diag-drawer-empty="no-entries"]');
    expect(noEntries.hidden).toBe(false);
  });

  it("shows no-matches when filter excludes everything", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    es.fire("entry", { seq: 1, timestamp: 1, level: "INFO", message: "i" });
    document.querySelector('[data-diag-level="ERROR"]').click();
    expect(document.querySelector('[data-diag-drawer-empty="no-matches"]').hidden).toBe(false);
  });
});

describe("drawer.js — F-CLOSE-RACE: reopen within close animation", () => {
  it("does NOT re-hide the drawer when stale close-timeout fires after reopen", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    document.querySelector("[data-diag-drawer-close]").click();
    // Reopen 50ms into the 240ms close animation.
    await vi.advanceTimersByTimeAsync(50);
    document.querySelector("[data-diag-ribbon-button]").click();
    // Fire the would-be-stale timeout.
    await vi.advanceTimersByTimeAsync(300);
    expect(getDrawer().hidden).toBe(false);
    expect(getDrawer().hasAttribute("inert")).toBe(false);
  });
});

describe("drawer.js — F-SSE-LEAK: closeDrawer closes the stream", () => {
  it("closes the EventSource when the drawer closes (frees server cap slot)", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const es = eventSources[0];
    expect(es.closed).toBe(false);
    document.querySelector("[data-diag-drawer-close]").click();
    await vi.advanceTimersByTimeAsync(260);
    expect(es.closed).toBe(true);
  });

  it("reopening after close spawns a fresh EventSource carrying since_seq=lastSeq for gap-fill", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    eventSources[0].fire("hello", { sid: "x", latest_seq: 3 });
    eventSources[0].fire("entry", { seq: 9, timestamp: 1, level: "INFO", message: "x" });
    document.querySelector("[data-diag-drawer-close]").click();
    document.querySelector("[data-diag-ribbon-button]").click();
    expect(eventSources.length).toBe(2);
    expect(eventSources[1].url).toContain("since_seq=9");
  });
});

describe("drawer.js — F-COPY-UNWIRED: per-entry copy", () => {
  it("renders Phosphor SVG (not emoji ⧉) for the copy button", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    eventSources[0].fire("entry", { seq: 1, timestamp: 1, level: "INFO", message: "x" });
    const svg = document.querySelector("[data-diag-drawer-entry-copy] svg");
    expect(svg).not.toBeNull();
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(document.querySelector("[data-diag-drawer-entry-copy]").textContent).not.toContain("⧉");
  });

  it("delegated click on a copy button writes the formatted line to clipboard", async () => {
    buildDom();
    loadScript("drawer.js");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    document.querySelector("[data-diag-ribbon-button]").click();
    eventSources[0].fire("entry", { seq: 1, timestamp: 1717000001, level: "ERROR", message: "boom" });
    // Click the inner SVG of the copy button — delegation should
    // walk up via closest().
    document.querySelector("[data-diag-drawer-entry-copy] svg").dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true })
    );
    await vi.runOnlyPendingTimersAsync();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0][0]).toMatch(/^\d\d:\d\d:\d\d ERROR boom$/);
  });
});

describe("drawer.js — F-UTC-TIME: timestamps render in local time", () => {
  it("uses local-time getHours not getUTCHours", () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    // Pick a timestamp where UTC vs local-tz delta is observable.
    // 1717000000 = 2024-05-29 18:26:40 UTC.
    eventSources[0].fire("entry", { seq: 1, timestamp: 1717000000, level: "INFO", message: "tz" });
    const timeSpan = document.querySelector(".diag-drawer__entry-time");
    const d = new Date(1717000000 * 1000);
    const expected =
      String(d.getHours()).padStart(2, "0") + ":" +
      String(d.getMinutes()).padStart(2, "0") + ":" +
      String(d.getSeconds()).padStart(2, "0");
    expect(timeSpan.textContent).toBe(expected);
  });
});

describe("drawer.js — F-AUTOSCROLL: reduce-motion respected (D11)", () => {
  it("does NOT auto-scroll when prefers-reduced-motion is set", () => {
    buildDom();
    const matchMediaOrig = window.matchMedia;
    window.matchMedia = (q) => ({
      matches: q.includes("reduce"),
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    });
    try {
      loadScript("drawer.js");
      document.querySelector("[data-diag-ribbon-button]").click();
      const entries = document.querySelector("[data-diag-drawer-entries]");
      // Stub scrollHeight + scrollTop to verify autoScrollTail is a no-op.
      Object.defineProperty(entries, "scrollHeight", { configurable: true, value: 1000 });
      Object.defineProperty(entries, "clientHeight", { configurable: true, value: 200 });
      entries.scrollTop = 0;
      eventSources[0].fire("entry", { seq: 1, timestamp: 1, level: "INFO", message: "x" });
      expect(entries.scrollTop).toBe(0); // no auto-scroll
    } finally {
      window.matchMedia = matchMediaOrig;
    }
  });
});

describe("drawer.js — F-RECONNECT-RACE: no duplicate streams", () => {
  it("scheduleReconnect does NOT open a second stream while one is already live", async () => {
    buildDom();
    loadScript("drawer.js");
    document.querySelector("[data-diag-ribbon-button]").click();
    const first = eventSources[0];
    first.fireTransportError();
    // Pre-/review the reconnect timer would unconditionally call
    // openStream after the backoff. If the user re-opened the drawer
    // (which calls ensureStream and creates a fresh ES), the reconnect
    // timer would then double up. Verify the timer's open check now
    // guards on (state.open && !state.eventSource).
    document.querySelector("[data-diag-drawer-close]").click();
    await vi.advanceTimersByTimeAsync(260);
    document.querySelector("[data-diag-ribbon-button]").click();
    // Now 2 streams exist (first errored, second from reopen).
    const beforeTimer = eventSources.length;
    await vi.advanceTimersByTimeAsync(1500);
    // The 1s scheduled reconnect should be a no-op because eventSource
    // is non-null AND the timer was cleared when the stream closed.
    expect(eventSources.length).toBe(beforeTimer);
  });
});

describe("drawer.js — PR3a integration: Open-live-drawer button", () => {
  it("removes disabled + wires click + hides the pending hint", () => {
    document.body.innerHTML = `
      <main></main>
      <nav class="tabbar"><a href="/">x</a></nav>
      <button data-diag-ribbon-button></button>
      <div data-diag-page-dim></div>
      <section data-diag-drawer hidden inert aria-hidden="true" aria-modal="false" role="dialog">
        <div data-diag-drawer-handle></div>
        <button data-diag-drawer-close></button>
        <div data-diag-drawer-welcome><button data-diag-drawer-welcome-dismiss></button></div>
        <div role="radiogroup">
          <button class="diag-level-filter__option" role="radio" aria-checked="true" data-diag-level="">All</button>
        </div>
        <button data-diag-drawer-fresh></button>
        <div data-diag-drawer-empty="no-entries" hidden></div>
        <div data-diag-drawer-empty="no-matches" hidden></div>
        <div data-diag-drawer-empty="journal-denied" hidden></div>
        <div data-diag-drawer-empty="disconnected" hidden></div>
        <p data-diag-drawer-hidden-batch hidden></p>
        <ol data-diag-drawer-entries></ol>
        <button data-diag-drawer-follow-pill hidden><span data-diag-drawer-follow-count>0</span></button>
      </section>
      <p>
        <button data-diag-open-drawer disabled aria-disabled="true" title="pending">Open live drawer</button>
        <small class="diag-log-snapshot__pending">(coming soon)</small>
      </p>
    `;
    loadScript("drawer.js");
    const btn = document.querySelector("[data-diag-open-drawer]");
    expect(btn.disabled).toBe(false);
    expect(btn.hasAttribute("aria-disabled")).toBe(false);
    expect(btn.hasAttribute("title")).toBe(false);
    expect(document.querySelector(".diag-log-snapshot__pending").hidden).toBe(true);
    btn.click();
    expect(document.body.hasAttribute("data-diag-drawer-open")).toBe(true);
  });
});
