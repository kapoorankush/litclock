// Behavior coverage for src/control_server/static/js/diagnostics.js (#416 PR3a).
//
// The module is an IIFE; tests drive it through the DOM. We mirror the SSR
// markup in buildDom() so a template-side drift is surfaced (CLAUDE.md
// learning: synthetic DOM tests silently pass against their own scaffold if
// production markup drifts — keep both ends in lockstep).
//
// /review fix F-LISTENER-STACK: the production module exposes a teardown
// hook at window.__litclockDiag.teardown that removes document listeners +
// clears intervals. We call it in afterEach so stacked listeners can't
// accumulate across the file's tests (LitClock learning
// [[iife-listener-stacking-vitest]]).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

let fetchMock;
let originalDocHiddenDescriptor;
let originalIsSecureContextDescriptor;
let originalClipboardDescriptor;

function buildDom({ anomalies = [], uncollected = [], logEntries = [], serviceUnits = [] } = {}) {
  const isAnomalous = (id) => anomalies.includes(id);
  const isUncollected = (id) => !isAnomalous(id) && uncollected.includes(id);
  const severity = (() => {
    if (anomalies.includes("services") || anomalies.includes("recent-log-entries")) return "error";
    if (anomalies.length > 0) return "warning";
    if (uncollected.length > 0) return "settling";
    return "ok";
  })();
  const title = (() => {
    if (severity === "error") return "Clock isn't running";
    if (severity === "warning") return "Something needs attention";
    if (severity === "settling") return "Just settling in.";
    return "All running";
  })();
  // #436 — mirror the SSR template: each row carries data-diag-healthy (the
  // server's _is_obviously_healthy verdict) and a NON-healthy row seeds a
  // "loading logs…" tail placeholder that JS hydrates per-unit. Default
  // healthy = (state === "active") unless the seed states it explicitly.
  const serviceRows = serviceUnits.map((u) => {
    const healthy = u.healthy !== undefined ? u.healthy : u.state === "active";
    const tail = healthy
      ? ""
      : `\n        <pre class="diag-service__tail" data-diag-tail data-diag-tail-status="loading"><code data-diag-tail-body>loading logs…</code></pre>`;
    return `
      <li class="diag-service" data-diag-unit="${u.unit}" data-diag-healthy="${healthy ? 1 : 0}">
        <div class="diag-service__head">
          <span class="diag-service__name mono">${u.unit}</span>
          <span class="diag-service__state diag-service__state--${u.state} mono">${u.state}</span>
        </div>${tail}
      </li>`;
  }).join("");
  const logRows = logEntries.map(
    (e) => `
      <li class="diag-log-entry diag-log-entry--${(e.level || "INFO").toLowerCase()}" tabindex="0">
        <span class="diag-log-entry__time mono">${e.timeStr || ""}</span>
        <span class="diag-log-entry__level mono">${e.level || "INFO"}</span>
        <span class="diag-log-entry__msg">${e.message || ""}</span>
        <button type="button" class="diag-log-entry__copy" data-diag-log-copy
                aria-label="Copy log entry"></button>
      </li>`
  ).join("");

  // #432 — section-aware "settling" banner body (SSR copy-paste from the
  // Jinja template's _settling_body lookup).
  const settlingBody = (() => {
    if (severity !== "settling") return "";
    const k = uncollected.slice().sort().join("+");
    if (k === "network") return "Your clock is finishing its first network check.";
    if (k === "time-location") return "Your clock is finishing its first location check.";
    if (k === "network+time-location") {
      return "Your clock is finishing its first network and location checks.";
    }
    return "";
  })();

  // #432 — helper to render a section pill in any tri-state. Anomaly
  // wins over uncollected (server-side precedence is already applied,
  // so this branch matches the macro's render rules).
  const pillFor = (id, anomalyLabel) => {
    if (isAnomalous(id)) {
      return {
        cls: "warning",
        label: anomalyLabel,
        cardOpen: true,
      };
    }
    if (isUncollected(id)) {
      return { cls: "muted", label: "Not yet collected", cardOpen: false };
    }
    return { cls: "ok", label: "OK", cardOpen: false };
  };

  const sysPill = pillFor("system", "Resource alert");
  const netPill = pillFor("network", "Connection issue");

  document.body.innerHTML = `
    <section class="status-banner status-banner--${severity}" data-diag-banner>
      <span class="status-banner__icon status-banner__icon--ok"
            ${severity !== "ok" ? "hidden" : ""}></span>
      <span class="status-banner__icon status-banner__icon--warning"
            ${severity === "ok" || severity === "settling" ? "hidden" : ""}></span>
      <span class="status-banner__icon status-banner__icon--settling"
            ${severity !== "settling" ? "hidden" : ""}></span>
      <div class="status-banner__copy">
        <div class="status-banner__live" data-diag-banner-live
             role="status" aria-live="polite">
          <h1 data-diag-banner-title>${title}</h1>
          ${
            severity === "settling" && settlingBody
              ? `<p class="status-banner__body" data-diag-banner-body>${settlingBody}</p>`
              : ""
          }
        </div>
        <p data-diag-banner-meta>
          <span data-diag-banner-refreshed>Refreshed just now</span>
          <span>Auto every 30s</span>
        </p>
      </div>
      <button data-diag-reveal aria-pressed="false">
        <svg class="reveal-pill__icon reveal-pill__icon--off"></svg>
        <svg class="reveal-pill__icon reveal-pill__icon--on" hidden></svg>
        <span data-diag-reveal-label>Reveal</span>
      </button>
    </section>

    <div class="diag-sections" data-diag-sections>
      <details class="diag-section${isAnomalous("system") ? " diag-section--anomalous" : ""}"
               data-diag-section="system"
               ${isAnomalous("system") ? "open" : ""}>
        <summary>
          <span data-diag-section-pill
                class="diag-section__pill diag-section__pill--${sysPill.cls}">
            <span class="diag-section__pill-label">${sysPill.label}</span>
          </span>
        </summary>
        <dl>
          <div class="diag-row">
            <dd class="mono" data-diag-value="cpu_temp_c">50.0</dd>
          </div>
        </dl>
      </details>

      <details class="diag-section${isAnomalous("network") ? " diag-section--anomalous" : ""}"
               data-diag-section="network"
               ${isAnomalous("network") ? "open" : ""}>
        <summary>
          <span data-diag-section-pill
                class="diag-section__pill diag-section__pill--${netPill.cls}">
            <span class="diag-section__pill-label">${netPill.label}</span>
          </span>
        </summary>
        <p class="diag-rows-placeholder"
           data-diag-rows-placeholder
           ${isUncollected("network") ? "" : "hidden"}>
          <em>Network details fill in once your clock sees a network event.</em>
        </p>
        <dl data-diag-rows ${isUncollected("network") ? "hidden" : ""}>
          <div class="diag-row"><dd class="mono" data-diag-value="lan_ip">—</dd></div>
        </dl>
      </details>

      <details class="diag-section${isAnomalous("services") ? " diag-section--anomalous" : ""}"
               data-diag-section="services"
               ${isAnomalous("services") ? "open" : ""}>
        <summary>
          <span data-diag-section-pill class="diag-section__pill diag-section__pill--ok">
            <span class="diag-section__pill-label">OK</span>
          </span>
        </summary>
        <ul data-diag-services>${serviceRows}</ul>
      </details>

      <details class="diag-section" data-diag-section="recent-log-entries">
        <summary>
          <span data-diag-section-pill class="diag-section__pill diag-section__pill--ok">
            <span class="diag-section__pill-label">OK</span>
          </span>
        </summary>
        <ol class="diag-log-snapshot" data-diag-log-snapshot>${logRows}</ol>
      </details>
    </div>

    <section class="diag-copy">
      <p>
        SSID, city, and coordinates are
        <span data-diag-copy-reveal-state>redacted</span>
        by default.
      </p>
      <pre data-diag-copy-block><code>old copy block content</code></pre>
      <button data-diag-copy-button>Copy</button>
    </section>

    <div data-diag-announcer></div>
  `;
}

// Fix B — patchBanner now consumes the EFFECTIVE post-debounce uncollected
// list rebuilt from DOM-present sections, NOT the raw server payload.
// Tests that assert banner behavior for sections buildDom doesn't render
// must inject them first so patchSection runs and feeds the effective list.
function _appendSection(sectionId) {
  const sections = document.querySelector("[data-diag-sections]");
  if (!sections) throw new Error("_appendSection: buildDom() must run first");
  const details = document.createElement("details");
  details.setAttribute("data-diag-section", sectionId);
  details.innerHTML = `<summary><span data-diag-section-pill
        class="diag-section__pill diag-section__pill--ok"
        data-diag-anomaly-label="Needs attention">
      <span class="diag-section__pill-label">OK</span>
    </span></summary>`;
  sections.appendChild(details);
}

// #436 — a Response-like literal for function-form fetch mocks (the mock
// matches on pathname only, so per-unit journal responses branch by call
// order inside a function rather than by URL pattern).
function jsonOk(body) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

beforeEach(() => {
  // Explicitly include `performance` in the fake-timer set so the
  // production code's `_monotonicNow()` (which prefers performance.now()
  // for NTP-step immunity per adversarial-review F4) advances with
  // vi.advanceTimersByTime. vitest 2.x's default useFakeTimers() does
  // NOT mock performance — verified empirically with a sentinel test.
  vi.useFakeTimers({
    toFake: [
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
      "Date",
      "performance",
      "queueMicrotask",
      "requestAnimationFrame",
      "cancelAnimationFrame",
    ],
  });
  document.body.innerHTML = "";
  try { sessionStorage.clear(); } catch (e) { /* ignore */ }
  fetchMock = installFetchMock();
  originalDocHiddenDescriptor = Object.getOwnPropertyDescriptor(document, "hidden");
  originalIsSecureContextDescriptor = Object.getOwnPropertyDescriptor(window, "isSecureContext");
  originalClipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
});

afterEach(() => {
  // /review fix F-LISTENER-STACK: tear down the production module's
  // listeners + intervals before next test loads the IIFE again.
  if (window.__litclockDiag && typeof window.__litclockDiag.teardown === "function") {
    window.__litclockDiag.teardown();
  }
  fetchMock.restore();
  if (originalDocHiddenDescriptor) {
    Object.defineProperty(document, "hidden", originalDocHiddenDescriptor);
  } else {
    delete document.hidden;
  }
  if (originalIsSecureContextDescriptor) {
    Object.defineProperty(window, "isSecureContext", originalIsSecureContextDescriptor);
  } else {
    delete window.isSecureContext;
  }
  if (originalClipboardDescriptor) {
    Object.defineProperty(navigator, "clipboard", originalClipboardDescriptor);
  } else {
    delete navigator.clipboard;
  }
  vi.useRealTimers();
});

describe("diagnostics.js boot", () => {
  it("applies Reveal=off UI when sessionStorage is empty", async () => {
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-reveal]");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(document.querySelector("[data-diag-reveal-label]").textContent).toBe("Reveal");
  });

  it("applies Reveal=on UI when sessionStorage carries the flag", async () => {
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { cpu_temp_c: 50.0 },
        anomalies: [],
        copy_payload: "```markdown\n# revealed\n```",
        section_order: ["system"],
        revealed_groups: ["location"],
      },
    });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-reveal]");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector("[data-diag-reveal-label]").textContent).toBe("Hide");
    await vi.runOnlyPendingTimersAsync();
    expect(fetchMock.calls.some((c) => c.url.includes("reveal=location"))).toBe(true);
  });
});

describe("diagnostics.js Reveal toggle", () => {
  it("flips state, persists to sessionStorage, refetches with ?reveal=location", async () => {
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { cpu_temp_c: 50.0 },
        anomalies: [],
        copy_payload: "```markdown\nrevealed\n```",
        section_order: [],
        revealed_groups: ["location"],
      },
    });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-reveal]");
    btn.click();
    expect(sessionStorage.getItem("litclock.diag.reveal-location")).toBe("1");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    await vi.runOnlyPendingTimersAsync();
    expect(fetchMock.calls.some((c) => c.url.includes("reveal=location"))).toBe(true);
    expect(document.querySelector("[data-diag-copy-block] code").textContent).toContain("revealed");
    expect(document.querySelector("[data-diag-copy-reveal-state]").textContent).toBe("visible");
  });

  it("clears sessionStorage and re-redacts on second toggle", async () => {
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-reveal]");
    btn.click();
    expect(sessionStorage.getItem("litclock.diag.reveal-location")).toBeNull();
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(document.querySelector("[data-diag-copy-reveal-state]").textContent).toBe("redacted");
  });
});

describe("diagnostics.js F-REVEAL-RACE — reveal-state guard on stale response", () => {
  it("discards an in-flight revealed response if the user toggled Hide before it landed", async () => {
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    buildDom();
    // The fetch mock matches against pathname only — both reveal+plain
    // URLs hit the same registered handler. Branch on opts/URL inside the
    // function form.
    const revealedBody = {
      ok: true,
      values: { cpu_temp_c: 99.9 },
      anomalies: [],
      copy_payload: "```markdown\nSECRET-REVEALED-CONTENT\n```",
      section_order: [],
      revealed_groups: ["location"],
    };
    let resolveRevealed;
    let callCount = 0;
    fetchMock.register(/\/api\/diagnostics/, function () {
      callCount++;
      if (callCount === 1) {
        // Boot reveal fetch — pending until we manually resolve.
        return new Promise((resolve) => {
          resolveRevealed = () =>
            resolve({
              ok: true,
              status: 200,
              json: async () => revealedBody,
              text: async () => JSON.stringify(revealedBody),
            });
        });
      }
      // Subsequent fetch (after user toggled Hide) — plain redacted body.
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true, values: {}, anomalies: [],
          copy_payload: "REDACTED-PAYLOAD", section_order: [],
        }),
        text: async () => "",
      };
    });
    loadScript("diagnostics.js");
    // Boot fired a refresh with reveal=location; the response is pending.
    // The pending guard means the user toggle's refresh() is a no-op,
    // but the reveal-state guard at response-time discards the stale.
    const btn = document.querySelector("[data-diag-reveal]");
    btn.click();
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    // Now the OLD revealed response finally lands.
    if (typeof resolveRevealed !== "function") {
      throw new Error("Test setup: revealed fetch never fired (callCount=" + callCount + ")");
    }
    resolveRevealed();
    await vi.runOnlyPendingTimersAsync();
    // The DOM must NOT show the revealed copy_payload.
    expect(document.querySelector("[data-diag-copy-block] code").textContent).not.toContain(
      "SECRET-REVEALED-CONTENT"
    );
    expect(document.querySelector("[data-diag-copy-reveal-state]").textContent).toBe("redacted");
  });
});

describe("#435 PR4: Reveal click aborts + restarts the in-flight fetch", () => {
  it("fires a SECOND fetch when Reveal is clicked while the first is still pending", async () => {
    // Pre-#435 the Reveal click during a pending fetch was a no-op
    // (early-return on pending=true), so the user waited up to 30s
    // (next poll cycle) to see fresh values. PR4 makes the click
    // call refresher.abort() + refresher.refresh() in sequence — so a
    // SECOND fetch fires immediately even if the first never resolves.
    //
    // Boot starts with Reveal=on so the first fetch fires on load.
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    buildDom();
    let callCount = 0;
    fetchMock.register(/\/api\/diagnostics/, function () {
      callCount++;
      // Each fetch is a never-resolving promise. Pre-PR4 callCount
      // would stay at 1 because the Reveal click would early-return
      // on the pending guard. PR4's abort+refresh fires a second.
      return new Promise(() => {});
    });
    loadScript("diagnostics.js");
    // Boot's `if (readReveal()) refresher.refresh()` fired one fetch.
    expect(callCount).toBe(1);

    const btn = document.querySelector("[data-diag-reveal]");
    btn.click();
    await vi.runOnlyPendingTimersAsync();

    // PR4: abort() cleared `pending=false` + bumped `generation`, so
    // the synchronous refresh() call after it actually fires.
    expect(callCount).toBe(2);
  });

  it("does NOT increment consecutiveFailures when fetch is aborted by Reveal click", async () => {
    // Pre-#435 a Reveal click that landed during a pending fetch would
    // (in a future world where the click could abort) make the catch
    // handler see an AbortError and mistakenly increment failures,
    // surfacing as "Last refresh failed — retrying" the instant the
    // user toggled. PR4 guards against this with `err.name === 'AbortError'`.
    //
    // /review on PR #440 added the timeout-vs-user-abort distinction via
    // a TimeoutError re-throw — so this test must NOT advance past
    // FETCH_TIMEOUT_MS (10s) or the auto-timeout would correctly fire
    // and increment failures. Use small-tick `advanceTimersByTimeAsync`
    // to drain microtasks for the abort signal listener WITHOUT
    // triggering the timeout.
    vi.useFakeTimers();
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, function (opts) {
      // Hook the signal so we can synthesise the AbortError that a
      // real fetch produces when its signal is aborted. fetchMock
      // doesn't natively honour signals, so we simulate it.
      const signal = opts && opts.signal;
      return new Promise((resolve, reject) => {
        if (signal) {
          signal.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        }
      });
    });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-reveal]");
    btn.click();
    // Drain microtasks (for the abort signal listener + the AbortError
    // catch chain) WITHOUT advancing real time past FETCH_TIMEOUT_MS.
    await vi.advanceTimersByTimeAsync(0);

    // The "Last refresh failed" attribute MUST NOT be set despite the
    // first fetch being aborted (its AbortError is by design, not a
    // genuine failure).
    const meta = document.querySelector("[data-diag-banner-meta]");
    expect(meta && meta.getAttribute("data-diag-meta-failed")).toBeFalsy();
    vi.useRealTimers();
  });
});

describe("#440 /review fixes — adversarial findings", () => {
  it("FETCH_TIMEOUT_MS auto-abort surfaces as a failure, NOT a silent user-cancel", async () => {
    // /review CRITICAL finding (Claude adversarial): pre-fix, the catch
    // handler treated EVERY AbortError as a user-cancel, including the
    // FETCH_TIMEOUT_MS timeout. So a wedged journalctl that hit the 10s
    // timeout silently swallowed the failure — no "Last refresh failed
    // — retrying" UI, no consecutiveFailures increment, just stale data.
    // The fix re-throws timeout aborts as TimeoutError so the failure
    // path increments correctly.
    vi.useFakeTimers();
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, function (opts) {
      // Honour the abort signal so we can verify the timeout-induced
      // abort propagates to a TimeoutError (not AbortError) at the
      // outer catch.
      var signal = opts && opts.signal;
      return new Promise((resolve, reject) => {
        if (signal) {
          signal.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        }
      });
    });
    sessionStorage.setItem("litclock.diag.reveal-location", "1");
    loadScript("diagnostics.js");
    // Advance past the 10s FETCH_TIMEOUT_MS — fetchDiagnostics fires
    // controller.abort(), the catch sees timedOut=true + AbortError →
    // re-throws as TimeoutError → makeRefresher's catch hits the
    // failure path (TimeoutError doesn't match AbortError).
    await vi.advanceTimersByTimeAsync(10001);
    // Advance past REFRESH_HINT_MS so the unwind setTimeout projects
    // state.consecutiveFailures onto the meta-failed attribute.
    await vi.advanceTimersByTimeAsync(2001);
    const meta = document.querySelector("[data-diag-banner-meta]");
    // The user-visible failure indicator MUST be set — proving the
    // timeout was surfaced (not silently swallowed as a user-cancel).
    expect(meta && meta.getAttribute("data-diag-meta-failed")).toBe("1");
    vi.useRealTimers();
  });

  it("abort() clears pending even when AbortController is undefined (old WebView)", async () => {
    // /review CRITICAL finding (testing T-1): pre-fix, if AbortController
    // was undefined, abort() early-returned without clearing pending. The
    // synchronous refresh() then bailed on the stale pending=true flag,
    // making the Reveal click a no-op — strictly worse than the no-PR4
    // baseline that CQ-3 was supposed to fix.
    const originalAbortController = globalThis.AbortController;
    globalThis.AbortController = undefined;
    try {
      sessionStorage.setItem("litclock.diag.reveal-location", "1");
      buildDom();
      let callCount = 0;
      fetchMock.register(/\/api\/diagnostics/, function () {
        callCount++;
        return new Promise(() => {}); // never resolves
      });
      loadScript("diagnostics.js");
      expect(callCount).toBe(1); // boot fetch
      const btn = document.querySelector("[data-diag-reveal]");
      btn.click();
      await vi.runOnlyPendingTimersAsync();
      // Even with no AbortController to call abort() on, abort()
      // MUST clear pending + bump generation so the synchronous
      // refresh() can fire a second fetch. The generation bump
      // separately handles discarding the stale (and never-cancelled)
      // first fetch's eventual response.
      expect(callCount).toBe(2);
    } finally {
      globalThis.AbortController = originalAbortController;
    }
  });
});

describe("diagnostics.js F-OPEN-RACE — JS-driven .open does not mark user-interaction", () => {
  it("section continues to auto-open on subsequent polls after a JS-driven transition", async () => {
    buildDom({ anomalies: [] }); // SSR: clean
    const responseQueue = [];
    fetchMock.register(/\/api\/diagnostics/, function () {
      const body = responseQueue.shift() || {
        ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [],
      };
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    // First poll: anomaly arrives → JS opens system section.
    responseQueue.push({
      ok: true, values: { cpu_temp_c: 99.9 }, anomalies: ["system"],
      copy_payload: "", section_order: [],
    });
    // Second poll: anomaly clears → JS should close it (proof user-touch
    // tracking did NOT lock the section).
    responseQueue.push({
      ok: true, values: { cpu_temp_c: 50.0 }, anomalies: [],
      copy_payload: "", section_order: [],
    });
    loadScript("diagnostics.js");
    const sys = document.querySelector('[data-diag-section="system"]');
    expect(sys.open).toBe(false);

    await vi.advanceTimersByTimeAsync(30001);
    // Microtask queue drain for the toggle-suppression flag reset.
    await Promise.resolve();
    expect(sys.open).toBe(true);

    await vi.advanceTimersByTimeAsync(30001);
    await Promise.resolve();
    expect(sys.open).toBe(false);
  });
});

describe("diagnostics.js anomaly default-open behavior (D3)", () => {
  it("preserves user-closed state across polls (real user toggle wins)", async () => {
    buildDom({ anomalies: ["system"] }); // SSR: system anomalous, open
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { cpu_temp_c: 99.9 },
        anomalies: ["system"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    const sys = document.querySelector('[data-diag-section="system"]');
    expect(sys.open).toBe(true);
    // Real user closes the section — dispatch the toggle event directly
    // since our suppression flag is set only around JS-driven flips.
    sys.open = false;
    sys.dispatchEvent(new Event("toggle"));
    await vi.advanceTimersByTimeAsync(30001);
    await Promise.resolve();
    expect(sys.open).toBe(false);
  });
});

describe("diagnostics.js banner severity escalation (D29)", () => {
  it("uses error severity when services anomaly fires", async () => {
    buildDom({ anomalies: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: ["services"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--error")).toBe(true);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe(
      "Clock isn't running"
    );
  });

  it("uses warning severity for non-services anomalies", async () => {
    buildDom({ anomalies: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: ["network"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--warning")).toBe(true);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe(
      "Something needs attention"
    );
  });

  it("swaps back to OK when anomalies clear", async () => {
    buildDom({ anomalies: ["services"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--ok")).toBe(true);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe("All running");
  });
});

describe("diagnostics.js F-SERVICES-STALE — services section patched on poll", () => {
  it("rebuilds the services list when the poll returns new service_states, then hydrates the tail per-unit (#436)", async () => {
    buildDom({
      serviceUnits: [{ unit: "litclock.service", state: "active" }],
    });
    // #436 — the per-unit tail endpoint (registered FIRST so it wins the
    // first-match lookup over the general /api/diagnostics pattern).
    fetchMock.register(/\/api\/diagnostics\/journal/, {
      body: { ok: true, unit: "litclock.service", journal_tail: ["err line"] },
    });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {
          service_states: {
            "litclock.service": { state: "failed", healthy: false, journal_tail: [] },
            "litclock-control.service": { state: "active", healthy: true, journal_tail: [] },
          },
        },
        anomalies: ["services"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const services = document.querySelectorAll("[data-diag-services] .diag-service");
    expect(services.length).toBe(2);
    // The new failed state is now in the DOM.
    const failed = document.querySelector('[data-diag-unit="litclock.service"] .diag-service__state');
    expect(failed.textContent).toBe("failed");
    expect(failed.className).toContain("diag-service__state--failed");
    // The failed row is marked non-healthy and its tail hydrated from the
    // per-unit endpoint (NOT from the poll payload, which carries no tails).
    const row = document.querySelector('[data-diag-unit="litclock.service"]');
    expect(row.getAttribute("data-diag-healthy")).toBe("0");
    await vi.advanceTimersByTimeAsync(1);
    const tail = document.querySelector('[data-diag-unit="litclock.service"] [data-diag-tail-body]');
    expect(tail.textContent).toBe("err line");
    // The healthy sibling gets no tail slot + no per-unit fetch.
    expect(document.querySelector('[data-diag-unit="litclock-control.service"] [data-diag-tail]')).toBeNull();
  });

  it("#449 — chip COLOR follows state_modifier while TEXT stays the literal state", async () => {
    buildDom({
      serviceUnits: [{ unit: "litclock.service", state: "active" }],
    });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {
          service_states: {
            // oneshot mid-paint: literal state is activating, tone is neutral.
            "litclock.service": { state: "activating", state_modifier: "transient-ok", journal_tail: [] },
          },
        },
        anomalies: [],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const chip = document.querySelector('[data-diag-unit="litclock.service"] .diag-service__state');
    expect(chip.textContent).toBe("activating");
    expect(chip.className).toContain("diag-service__state--transient-ok");
    expect(chip.className).not.toContain("diag-service__state--activating");
  });

  it("#449 — falls back to coloring by literal state when state_modifier is absent (deploy-skew safe)", async () => {
    // A payload from a pre-#449 server (or one mid-rollover) omits
    // state_modifier; the chip must color by the literal state, preserving
    // the old behavior rather than dropping to an unstyled class.
    buildDom({
      serviceUnits: [{ unit: "litclock.service", state: "active" }],
    });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {
          service_states: {
            "litclock.service": { state: "failed", journal_tail: [] },
          },
        },
        anomalies: ["services"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const chip = document.querySelector('[data-diag-unit="litclock.service"] .diag-service__state');
    expect(chip.textContent).toBe("failed");
    expect(chip.className).toContain("diag-service__state--failed");
  });
});

describe("diagnostics.js #436 — per-unit journal tail hydration", () => {
  it("boot: an all-healthy clock fires ZERO per-unit journal fetches", async () => {
    buildDom({
      serviceUnits: [
        { unit: "litclock.service", state: "active", healthy: true },
        { unit: "litclock-control.service", state: "active", healthy: true },
      ],
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1);
    const journalCalls = fetchMock.calls.filter((c) => c.path === "/api/diagnostics/journal");
    expect(journalCalls.length).toBe(0);
    // No healthy row gets a tail slot at all.
    expect(document.querySelector("[data-diag-tail]")).toBeNull();
  });

  it("boot: a non-healthy row hydrates its tail from the per-unit endpoint", async () => {
    buildDom({ serviceUnits: [{ unit: "litclock.service", state: "failed", healthy: false }] });
    fetchMock.register(/\/api\/diagnostics\/journal/, {
      body: { ok: true, unit: "litclock.service", journal_tail: ["boom at line 1", "boom at line 2"] },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1);
    const journalCalls = fetchMock.calls.filter((c) => c.path === "/api/diagnostics/journal");
    expect(journalCalls.length).toBe(1);
    expect(journalCalls[0].url).toContain("unit=litclock.service");
    const pre = document.querySelector('[data-diag-unit="litclock.service"] [data-diag-tail]');
    expect(pre.getAttribute("data-diag-tail-status")).toBe("ok");
    const tail = pre.querySelector("[data-diag-tail-body]");
    expect(tail.textContent).toBe("boom at line 1\nboom at line 2");
  });

  it("multi-failure: one unit's tail loads while another's fails, independently (T4)", async () => {
    buildDom({
      serviceUnits: [
        { unit: "litclock.service", state: "failed", healthy: false },
        { unit: "litclock-control.service", state: "failed", healthy: false },
      ],
    });
    // Call order = DOM order: first non-healthy unit succeeds, second rejects.
    let n = 0;
    fetchMock.register(/\/api\/diagnostics\/journal/, () => {
      n += 1;
      if (n === 1) return jsonOk({ ok: true, unit: "litclock.service", journal_tail: ["A logs"] });
      return Promise.reject(new Error("network down"));
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1);
    const a = document.querySelector('[data-diag-unit="litclock.service"] [data-diag-tail-body]');
    const b = document.querySelector('[data-diag-unit="litclock-control.service"] [data-diag-tail-body]');
    // A resolved to its tail; B independently shows the error affordance.
    expect(a.textContent).toBe("A logs");
    expect(b.textContent).toBe("couldn’t load logs");
    const bPre = document.querySelector('[data-diag-unit="litclock-control.service"] [data-diag-tail]');
    expect(bPre.getAttribute("data-diag-tail-status")).toBe("error");
  });

  it("Copy payload keeps the server blob AND appends the hydrated service logs (T3)", async () => {
    buildDom({ serviceUnits: [{ unit: "litclock.service", state: "failed", healthy: false }] });
    fetchMock.register(/\/api\/diagnostics\/journal/, {
      body: { ok: true, unit: "litclock.service", journal_tail: ["fatal thing"] },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1);
    const copy = document.querySelector("[data-diag-copy-block] code").textContent;
    expect(copy).toContain("old copy block content"); // SSR server payload preserved
    expect(copy).toContain("## Service logs");
    expect(copy).toContain("### litclock.service");
    expect(copy).toContain("fatal thing");
  });

  it("Copy splices hydrated logs INSIDE the server's markdown fence (F3, /review)", async () => {
    buildDom({ serviceUnits: [{ unit: "litclock.service", state: "failed", healthy: false }] });
    fetchMock.register(/\/api\/diagnostics\/journal/, {
      body: { ok: true, unit: "litclock.service", journal_tail: ["fatal thing"] },
    });
    // Poll delivers a FENCED copy_payload, exactly like build_copy_payload does.
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { service_states: { "litclock.service": { state: "failed", healthy: false, journal_tail: [] } } },
        anomalies: ["services"],
        copy_payload: "# LitClock diagnostics\n```markdown\nstuff\n```",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001); // poll → patchCopyBlock(fenced) + hydrate
    await vi.advanceTimersByTimeAsync(1);
    const copy = document.querySelector("[data-diag-copy-block] code").textContent;
    // The hydrated logs land BEFORE the closing fence — inside the code block.
    const fenceIdx = copy.lastIndexOf("\n```");
    const logsIdx = copy.indexOf("fatal thing");
    expect(logsIdx).toBeGreaterThan(-1);
    expect(logsIdx).toBeLessThan(fenceIdx);
  });

  it("poll rebuild of an already-loaded tail does NOT flash back to 'loading' (F2, /review)", async () => {
    buildDom({ serviceUnits: [{ unit: "litclock.service", state: "failed", healthy: false }] });
    let journalCall = 0;
    fetchMock.register(/\/api\/diagnostics\/journal/, () => {
      journalCall += 1;
      if (journalCall === 1) return jsonOk({ ok: true, unit: "litclock.service", journal_tail: ["first load"] });
      return new Promise(() => {}); // poll's re-hydration fetch stays pending
    });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { service_states: { "litclock.service": { state: "failed", healthy: false, journal_tail: [] } } },
        anomalies: ["services"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1); // boot hydration → "first load"
    let pre = document.querySelector('[data-diag-unit="litclock.service"] [data-diag-tail]');
    expect(pre.getAttribute("data-diag-tail-status")).toBe("ok");
    expect(pre.querySelector("[data-diag-tail-body]").textContent).toBe("first load");
    // Poll rebuilds the row; re-hydration's 2nd fetch is pending. The row must
    // KEEP the loaded lines, not reset to "loading logs…".
    await vi.advanceTimersByTimeAsync(30001);
    pre = document.querySelector('[data-diag-unit="litclock.service"] [data-diag-tail]');
    expect(pre.getAttribute("data-diag-tail-status")).toBe("ok");
    expect(pre.querySelector("[data-diag-tail-body]").textContent).toBe("first load");
  });

  it("a stale in-flight tail fetch does NOT resurrect logs on a recovered healthy row (F1, /review)", async () => {
    buildDom({ serviceUnits: [{ unit: "litclock.service", state: "failed", healthy: false }] });
    let resolveJournal;
    fetchMock.register(/\/api\/diagnostics\/journal/, () =>
      new Promise((res) => { resolveJournal = res; }).then(() =>
        jsonOk({ ok: true, unit: "litclock.service", journal_tail: ["stale error logs"] })
      )
    );
    // The poll flips the unit to HEALTHY (recovered).
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: { service_states: { "litclock.service": { state: "active", healthy: true, journal_tail: [] } } },
        anomalies: [],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(1); // boot: row failed, hydration fetch pending
    await vi.advanceTimersByTimeAsync(30001); // poll flips to healthy → tail slot removed
    let row = document.querySelector('[data-diag-unit="litclock.service"]');
    expect(row.getAttribute("data-diag-healthy")).toBe("1");
    expect(row.querySelector("[data-diag-tail]")).toBeNull();
    // The STALE boot fetch resolves now — must NOT recreate a tail on the OK row.
    resolveJournal();
    await vi.advanceTimersByTimeAsync(1);
    row = document.querySelector('[data-diag-unit="litclock.service"]');
    expect(row.querySelector("[data-diag-tail]")).toBeNull();
  });
});

describe("diagnostics.js D10 — per-row log entry Copy", () => {
  it("copies the formatted line to the clipboard + announces", async () => {
    buildDom({
      logEntries: [{ timeStr: "12:34:56", level: "ERROR", message: "oops" }],
    });
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-log-copy]");
    btn.click();
    await vi.runOnlyPendingTimersAsync();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0][0]).toBe("12:34:56 ERROR oops");
    await vi.advanceTimersByTimeAsync(20);
    expect(document.querySelector("[data-diag-announcer]").textContent).toBe("Copied");
  });
});

describe("diagnostics.js copy-payload button (D18)", () => {
  it("copies the current block content to the clipboard + announces", async () => {
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    loadScript("diagnostics.js");
    const btn = document.querySelector("[data-diag-copy-button]");
    btn.click();
    await vi.runOnlyPendingTimersAsync();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0][0]).toBe("old copy block content");
    await vi.advanceTimersByTimeAsync(20);
    expect(document.querySelector("[data-diag-announcer]").textContent).toBe(
      "Copied support payload"
    );
  });
});

describe("diagnostics.js F-FAILURE-MASKED — persistent failure indicator", () => {
  it("flags meta-failed after consecutive failures (does not auto-clear)", async () => {
    buildDom();
    // Function-form fetch mock so each retry gets a fresh rejection
    // (a shared rejected Promise would trigger jsdom unhandled-rejection
    // warnings across retries).
    fetchMock.register(/\/api\/diagnostics/, function () {
      const p = Promise.reject(new Error("network"));
      // Silence the unhandled rejection by attaching a no-op catch — the
      // production code's .catch will still see the rejection too because
      // the fetch mock returns THIS same promise reference (the
      // rejection isn't "consumed" by attaching a handler; both attachers
      // see it).
      p.catch(() => {});
      return p;
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    // After the REFRESH_HINT_MS window, the failed attribute should land
    // and STAY (until a poll succeeds).
    await vi.advanceTimersByTimeAsync(2500);
    const meta = document.querySelector("[data-diag-banner-meta]");
    expect(meta.getAttribute("data-diag-meta-failed")).not.toBeNull();
  });
});

describe("diagnostics.js visibility-aware polling (D12)", () => {
  it("does NOT refresh while document.hidden is true", async () => {
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, {
      body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
    });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    loadScript("diagnostics.js");
    const before = fetchMock.calls.length;
    await vi.advanceTimersByTimeAsync(30001);
    expect(fetchMock.calls.length).toBe(before);
  });
});

describe("diagnostics.js sessionStorage unavailable (private browsing)", () => {
  it("survives sessionStorage.getItem throwing on boot", async () => {
    // iOS Safari Private Browsing throws on sessionStorage access.
    const origGet = Storage.prototype.getItem;
    Storage.prototype.getItem = function () { throw new DOMException("denied", "SecurityError"); };
    try {
      buildDom();
      fetchMock.register(/\/api\/diagnostics/, {
        body: { ok: true, values: {}, anomalies: [], copy_payload: "", section_order: [] },
      });
      expect(() => loadScript("diagnostics.js")).not.toThrow();
      // Reveal defaults to off (no throw → readReveal returns false).
      const btn = document.querySelector("[data-diag-reveal]");
      expect(btn.getAttribute("aria-pressed")).toBe("false");
    } finally {
      Storage.prototype.getItem = origGet;
    }
  });
});

// ----------------------------------------------------------------------------
// #432 — Tri-state grey "Not yet collected" tier
// ----------------------------------------------------------------------------

describe("#432 grey tier — pill flips to --muted on poll", () => {
  it("paints the network section's pill --muted when payload uncollected includes it", async () => {
    // SSR: network in OK state.
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {},
        anomalies: [],
        uncollected: ["network"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const pill = document.querySelector(
      '[data-diag-section="network"] [data-diag-section-pill]'
    );
    expect(pill.classList.contains("diag-section__pill--muted")).toBe(true);
    expect(pill.classList.contains("diag-section__pill--ok")).toBe(false);
    expect(pill.querySelector(".diag-section__pill-label").textContent).toBe(
      "Not yet collected"
    );
    // aria-label on the pill announces the longer explanation.
    expect(pill.getAttribute("aria-label")).toContain("Not yet collected");
  });

  it("falls back to OK when uncollected is absent from the payload (cached stale client)", async () => {
    // SSR uncollected; payload doesn't include the field (simulates a
    // cached pre-v0.214.4 client reading a new payload — or vice versa).
    buildDom({ anomalies: [], uncollected: ["network"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {},
        anomalies: [],
        // uncollected key intentionally absent — must not crash.
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const pill = document.querySelector(
      '[data-diag-section="network"] [data-diag-section-pill]'
    );
    expect(pill.classList.contains("diag-section__pill--ok")).toBe(true);
    expect(pill.classList.contains("diag-section__pill--muted")).toBe(false);
  });
});

describe("#432 grey tier — SR announcer on forward transition", () => {
  it("announces 'Network details available.' on uncollected → ok for network", async () => {
    buildDom({ anomalies: [], uncollected: ["network"] });
    const responseQueue = [
      {
        ok: true,
        values: {},
        anomalies: [],
        uncollected: ["network"], // poll 1 stays uncollected
        copy_payload: "",
        section_order: [],
      },
      {
        ok: true,
        values: {},
        anomalies: [],
        uncollected: [], // poll 2 forward-transitions to ok
        copy_payload: "",
        section_order: [],
      },
    ];
    fetchMock.register(/\/api\/diagnostics/, function () {
      const body = responseQueue.shift() || responseQueue[0];
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      };
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    // After poll 1 the section is still uncollected — no announce yet.
    await vi.advanceTimersByTimeAsync(30001);
    // After poll 2 the section flipped to ok — announce fires after the
    // 16ms ANNOUNCE_RESET_MS delay.
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    expect(announcer.textContent).toBe("Network details available.");
  });

  it("is silent on ok → uncollected (reverse transition)", async () => {
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {},
        anomalies: [],
        uncollected: ["network"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    // No "Network details available." (forward only) — and certainly no
    // "Network details unavailable" or similar.
    expect(announcer.textContent).toBe("");
  });
});

describe("#432 grey tier — reverse-transition debounce (D8)", () => {
  it("suppresses ok → uncollected within 60s of the last forward transition", async () => {
    // Sequence: poll 1 uncollected, poll 2 ok (forward — records timestamp),
    // poll 3 uncollected again (reverse within 60s — must be suppressed).
    buildDom({ anomalies: [], uncollected: ["network"] });
    const responseQueue = [
      { ok: true, values: {}, anomalies: [], uncollected: ["network"], copy_payload: "", section_order: [] },
      { ok: true, values: {}, anomalies: [], uncollected: [], copy_payload: "", section_order: [] },
      { ok: true, values: {}, anomalies: [], uncollected: ["network"], copy_payload: "", section_order: [] },
    ];
    fetchMock.register(/\/api\/diagnostics/, function () {
      const body = responseQueue.shift() || responseQueue[responseQueue.length - 1];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001); // poll 1 — stays uncollected
    await vi.advanceTimersByTimeAsync(30001); // poll 2 — forward to ok
    await vi.advanceTimersByTimeAsync(30001); // poll 3 — reverse within 60s
    const pill = document.querySelector(
      '[data-diag-section="network"] [data-diag-section-pill]'
    );
    // The debounce window suppressed the reverse — pill stays ok.
    expect(pill.classList.contains("diag-section__pill--ok")).toBe(true);
    expect(pill.classList.contains("diag-section__pill--muted")).toBe(false);
  });
});

describe("#432 grey tier — banner sync (severity + body)", () => {
  it("flips banner severity to 'settling' when only uncollected sections are non-OK", async () => {
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true,
        values: {},
        anomalies: [],
        uncollected: ["network"],
        copy_payload: "",
        section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--settling")).toBe(true);
    expect(banner.classList.contains("status-banner--ok")).toBe(false);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe(
      "Just settling in."
    );
    // Settling icon visible, ok + warning icons hidden.
    expect(banner.querySelector(".status-banner__icon--settling").hidden).toBe(false);
    expect(banner.querySelector(".status-banner__icon--ok").hidden).toBe(true);
    expect(banner.querySelector(".status-banner__icon--warning").hidden).toBe(true);
  });

  it("renders network-only body copy when only network is uncollected", async () => {
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: ["network"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    expect(document.querySelector("[data-diag-banner-body]").textContent).toBe(
      "Your clock is finishing its first network check."
    );
  });

  it("renders time-location-only body copy when only time-location is uncollected", async () => {
    buildDom({ anomalies: [], uncollected: [] });
    // Fix B — banner severity is computed from the effective post-debounce
    // states of sections ACTUALLY in the DOM. buildDom doesn't include a
    // time-location section by default, so we inject one.
    _appendSection("time-location");
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: ["time-location"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    expect(document.querySelector("[data-diag-banner-body]").textContent).toBe(
      "Your clock is finishing its first location check."
    );
  });

  it("renders combined body copy when BOTH network + time-location are uncollected", async () => {
    buildDom({ anomalies: [], uncollected: [] });
    _appendSection("time-location");
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [],
        uncollected: ["time-location", "network"], // order-independent
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    expect(document.querySelector("[data-diag-banner-body]").textContent).toBe(
      "Your clock is finishing its first network and location checks."
    );
  });

  it("swaps settling → ok when all sections clear", async () => {
    buildDom({ anomalies: [], uncollected: ["network"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--ok")).toBe(true);
    expect(banner.classList.contains("status-banner--settling")).toBe(false);
    // Body line removed entirely (not just emptied).
    expect(document.querySelector("[data-diag-banner-body]")).toBeNull();
  });
});

describe("#432 grey tier — page-level poll-stale flag (D11)", () => {
  it("sets [data-poll-stale=true] after 2 consecutive failures", async () => {
    buildDom();
    fetchMock.register(/\/api\/diagnostics/, function () {
      const p = Promise.reject(new Error("network"));
      p.catch(() => {});
      return p;
    });
    loadScript("diagnostics.js");
    // First failure: failures=1, NOT yet stale.
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(2500);
    let container = document.querySelector("[data-diag-sections]");
    expect(container.getAttribute("data-poll-stale")).toBeNull();
    // Second failure: failures=2, stale flag MUST be set.
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(2500);
    container = document.querySelector("[data-diag-sections]");
    expect(container.getAttribute("data-poll-stale")).toBe("true");
  });

  it("clears [data-poll-stale] on the first successful poll after a streak", async () => {
    buildDom();
    let callCount = 0;
    fetchMock.register(/\/api\/diagnostics/, function () {
      callCount++;
      if (callCount <= 2) {
        const p = Promise.reject(new Error("network"));
        p.catch(() => {});
        return p;
      }
      const body = {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      };
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    loadScript("diagnostics.js");
    // Two failures → stale.
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(2500);
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(2500);
    expect(
      document.querySelector("[data-diag-sections]").getAttribute("data-poll-stale")
    ).toBe("true");
    // Third call succeeds → flag cleared.
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(2500);
    expect(
      document.querySelector("[data-diag-sections]").getAttribute("data-poll-stale")
    ).toBeNull();
  });
});

describe("#432 grey tier — coverage gaps surfaced by /review testing specialist", () => {
  it("announces 'Location details available.' on time-location forward transition (D9 symmetric branch)", async () => {
    // The network branch is covered above; this test covers the symmetric
    // time-location branch at diagnostics.js:298. A regression silencing
    // either branch would ship without this assertion.
    buildDom({ anomalies: [], uncollected: ["time-location"] });
    const responseQueue = [
      { ok: true, values: {}, anomalies: [], uncollected: ["time-location"], copy_payload: "", section_order: [] },
      { ok: true, values: {}, anomalies: [], uncollected: [], copy_payload: "", section_order: [] },
    ];
    fetchMock.register(/\/api\/diagnostics/, function () {
      const body = responseQueue.shift() || responseQueue[0];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    // SSR DOM doesn't include a time-location section by default; inject
    // a minimal one so patchSection can find it.
    const sections = document.querySelector("[data-diag-sections]");
    const details = document.createElement("details");
    details.setAttribute("data-diag-section", "time-location");
    details.innerHTML = `<summary><span data-diag-section-pill class="diag-section__pill diag-section__pill--muted"><span class="diag-section__pill-label">Not yet collected</span></span></summary>`;
    sections.appendChild(details);

    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001); // poll 1 — stays uncollected
    await vi.advanceTimersByTimeAsync(30001); // poll 2 — forward to ok
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    expect(announcer.textContent).toBe("Location details available.");
  });

  it("applies the reverse transition AFTER the 60s debounce window expires", async () => {
    // The debounce test above covers suppression INSIDE the window. This
    // covers the inverse: a real reverse transition more than 60s after
    // the forward must NOT be suppressed (else the debounce would become
    // a permanent latch).
    buildDom({ anomalies: [], uncollected: ["network"] });
    const RESP_UNCOLLECTED = {
      ok: true, values: {}, anomalies: [], uncollected: ["network"],
      copy_payload: "", section_order: [],
    };
    const RESP_OK = {
      ok: true, values: {}, anomalies: [], uncollected: [],
      copy_payload: "", section_order: [],
    };
    const responseQueue = [
      RESP_UNCOLLECTED, // poll 1 — stays uncollected
      RESP_OK,          // poll 2 — forward to ok (records timestamp)
      RESP_UNCOLLECTED, // poll 3 — reverse inside debounce window (suppressed)
      RESP_UNCOLLECTED, // poll 4 — reverse OUTSIDE debounce window (must apply)
    ];
    fetchMock.register(/\/api\/diagnostics/, function () {
      // Fall back to the last legitimate uncollected response if the
      // queue runs over — undefined would let body.uncollected default
      // to [], which would silently mask a real regression.
      const body = responseQueue.shift() || RESP_UNCOLLECTED;
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001); // t≈30s — poll 1
    await vi.advanceTimersByTimeAsync(30001); // t≈60s — poll 2 forward
    await vi.advanceTimersByTimeAsync(30001); // t≈90s — poll 3 (inside window, suppressed)
    await vi.advanceTimersByTimeAsync(30001); // t≈120s — poll 4 (outside window, applied)
    const pill = document.querySelector(
      '[data-diag-section="network"] [data-diag-section-pill]'
    );
    expect(pill.classList.contains("diag-section__pill--muted")).toBe(true);
    expect(pill.classList.contains("diag-section__pill--ok")).toBe(false);
  });

  it("banner severity is 'warning' (NOT 'settling') when anomalies AND uncollected fire together", async () => {
    // The bannerSeverity() ladder must keep anomaly tiers ABOVE settling.
    // A regression where settling beats warning would surface a calmer
    // banner on a clock with a REAL problem — strictly worse UX than the
    // pre-#432 baseline. Locks the precedence at the banner level.
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: ["system"], uncollected: ["network"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--warning")).toBe(true);
    expect(banner.classList.contains("status-banner--settling")).toBe(false);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe(
      "Something needs attention"
    );
  });

  it("forward-compat: unknown section ID in uncollected renders headline alone, no body, severity stays 'settling'", async () => {
    // _settlingBody() returns '' for unknown section IDs (a future
    // section that lands server-side before the JS lookup is updated).
    // The banner must NOT render wrong copy AND must NOT crash — graceful
    // degradation per the failure-modes table.
    buildDom({ anomalies: [], uncollected: [] });
    // Fix B — the future section must exist in the DOM for patchSection
    // to mark it 'uncollected' in the effective list that drives banner
    // severity. Without the inject the banner would stay ok (correct
    // architectural behavior post-Fix-B but defeats this test's intent).
    _appendSection("future-section");
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: ["future-section"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const banner = document.querySelector("[data-diag-banner]");
    expect(banner.classList.contains("status-banner--settling")).toBe(true);
    expect(document.querySelector("[data-diag-banner-title]").textContent).toBe(
      "Just settling in."
    );
    // No body element rendered (would have been wrong copy).
    expect(document.querySelector("[data-diag-banner-body]")).toBeNull();
  });

  it("uncollected sections stay CLOSED on poll; anomaly sections auto-open", async () => {
    // patchSection's auto-open contract (D3): anomaly opens, uncollected
    // does NOT (the italic placeholder is the at-a-glance signal — opening
    // would push the placeholder into focus and steal attention from real
    // anomalies higher up the page).
    buildDom({ anomalies: [], uncollected: [] });
    const responseQueue = [
      // poll 1: network goes uncollected — must stay closed
      { ok: true, values: {}, anomalies: [], uncollected: ["network"], copy_payload: "", section_order: [] },
      // poll 2: network goes anomaly — must auto-open
      { ok: true, values: {}, anomalies: ["network"], uncollected: [], copy_payload: "", section_order: [] },
    ];
    fetchMock.register(/\/api\/diagnostics/, function () {
      const body = responseQueue.shift() || responseQueue[responseQueue.length - 1];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const net = document.querySelector('[data-diag-section="network"]');
    expect(net.open).toBe(false); // uncollected stays closed
    await vi.advanceTimersByTimeAsync(30001);
    expect(net.open).toBe(true); // anomaly auto-opens
  });
});

describe("#432 grey tier — D1/D2 DOM swap on transition (design specialist)", () => {
  it("hides the dl and shows the placeholder on ok → uncollected reverse", async () => {
    // SSR: network is OK (dl visible, placeholder hidden). Poll returns
    // uncollected → JS must hide the dl AND unhide the placeholder so
    // the visible content matches the muted pill.
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: ["network"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    const net = document.querySelector('[data-diag-section="network"]');
    expect(net.querySelector("[data-diag-rows]").hidden).toBe(false);
    expect(net.querySelector("[data-diag-rows-placeholder]").hidden).toBe(true);
    await vi.advanceTimersByTimeAsync(30001);
    expect(net.querySelector("[data-diag-rows]").hidden).toBe(true);
    expect(net.querySelector("[data-diag-rows-placeholder]").hidden).toBe(false);
  });

  it("hides the placeholder and shows the dl on uncollected → ok forward", async () => {
    // SSR: network uncollected (placeholder visible, dl hidden). Poll
    // returns ok → JS must unhide the dl AND hide the placeholder so the
    // green pill + "Network details available." announcement matches
    // the visible content.
    buildDom({ anomalies: [], uncollected: ["network"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: { lan_ip: "192.168.1.5" }, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    const net = document.querySelector('[data-diag-section="network"]');
    expect(net.querySelector("[data-diag-rows]").hidden).toBe(true);
    expect(net.querySelector("[data-diag-rows-placeholder]").hidden).toBe(false);
    await vi.advanceTimersByTimeAsync(30001);
    expect(net.querySelector("[data-diag-rows]").hidden).toBe(false);
    expect(net.querySelector("[data-diag-rows-placeholder]").hidden).toBe(true);
    // The dl's value cell was patched from the poll response.
    expect(net.querySelector('[data-diag-value="lan_ip"]').textContent).toBe("192.168.1.5");
  });
});

describe("F1 fix — priorStates seeded from SSR pill class at boot", () => {
  it("fires 'Network details available.' on the FIRST post-boot poll that flips SSR-uncollected to ok", async () => {
    // Pre-fix priorStates was empty {} at boot. SSR-rendered uncollected
    // → polled ok set newState='ok' but oldState was undefined (not
    // 'uncollected') so the forward-transition branch didn't fire and
    // the SR announce was silent — for the EXACT gift-recipient persona
    // D9 was designed for. With F1 seed, the first poll fires the
    // announce as the user expects.
    buildDom({ anomalies: [], uncollected: ["network"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    expect(announcer.textContent).toBe("Network details available.");
  });

  it("does NOT fire forward announcement when SSR already rendered ok and poll returns ok", async () => {
    // Sanity check that the seed doesn't introduce a false positive.
    // SSR ok → poll ok: priorStates seeded as 'ok', _stateOf returns
    // 'ok', no transition, no announce.
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    expect(announcer.textContent).toBe("");
  });
});

describe("F2 fix — compound forward transitions batched into ONE announcement", () => {
  it("announces 'Network and location details available.' when BOTH sections flip uncollected → ok in same poll", async () => {
    // Pre-fix the two per-section announce() calls raced through the
    // announcer's textContent='' + setTimeout reset, and the second
    // call overwrote the first before any AT picked it up. The user
    // heard ONE of the two transitions. With F2's batched flush, both
    // are conveyed in one message.
    buildDom({ anomalies: [], uncollected: ["network"] });
    // Append a time-location section that buildDom doesn't include
    // by default, so both sections can flip in lockstep.
    const sections = document.querySelector("[data-diag-sections]");
    const tl = document.createElement("details");
    tl.setAttribute("data-diag-section", "time-location");
    tl.innerHTML = `<summary><span data-diag-section-pill class="diag-section__pill diag-section__pill--muted"><span class="diag-section__pill-label">Not yet collected</span></span></summary>`;
    sections.appendChild(tl);

    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    await vi.advanceTimersByTimeAsync(20);
    const announcer = document.querySelector("[data-diag-announcer]");
    expect(announcer.textContent).toBe(
      "Network and location details available."
    );
  });
});

describe("F8 fix — permanent live region wrapping title+body (not meta)", () => {
  it("SSR renders role=status + aria-live=polite on the live wrapper, NOT on the outer banner", async () => {
    // The live region is on [data-diag-banner-live] which wraps ONLY
    // the title (+ body when settling) — the 30s-updating meta line is
    // OUTSIDE so it doesn't re-announce on every poll. The outer
    // [data-diag-banner] section must NOT carry role/aria-live.
    buildDom({ anomalies: [], uncollected: ["network"] });
    const banner = document.querySelector("[data-diag-banner]");
    const live = document.querySelector("[data-diag-banner-live]");
    expect(banner.getAttribute("role")).toBeNull();
    expect(banner.getAttribute("aria-live")).toBeNull();
    expect(live.getAttribute("role")).toBe("status");
    expect(live.getAttribute("aria-live")).toBe("polite");
  });

  it("preserves role + aria-live on the live wrapper across severity transitions (F8 regression)", async () => {
    // Pre-fix the JS removed role+aria-live on the OUTER banner whenever
    // severity wasn't 'settling', which destroyed the live region at
    // exactly the same tick the title changed — AT could drop the
    // queued title-change announce. With the wrapper permanent, the
    // attrs survive every poll-driven transition.
    buildDom({ anomalies: [], uncollected: ["network"] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: [],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const live = document.querySelector("[data-diag-banner-live]");
    expect(live.getAttribute("role")).toBe("status");
    expect(live.getAttribute("aria-live")).toBe("polite");
  });

  it("inserts the settling body INSIDE the live wrapper (not in __copy)", async () => {
    // Poll transitions ok → settling. The body element MUST be appended
    // as a child of [data-diag-banner-live] so its textContent change
    // is announced by the live region. If JS inserted it as a sibling
    // outside the wrapper, SR users would miss the section-aware copy.
    buildDom({ anomalies: [], uncollected: [] });
    fetchMock.register(/\/api\/diagnostics/, {
      body: {
        ok: true, values: {}, anomalies: [], uncollected: ["network"],
        copy_payload: "", section_order: [],
      },
    });
    loadScript("diagnostics.js");
    await vi.advanceTimersByTimeAsync(30001);
    const live = document.querySelector("[data-diag-banner-live]");
    const body = document.querySelector("[data-diag-banner-body]");
    expect(body).not.toBeNull();
    expect(body.parentElement).toBe(live);
    expect(body.textContent).toBe(
      "Your clock is finishing its first network check."
    );
  });
});
