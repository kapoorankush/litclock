// Coordination tests for the /review #406 follow-up: aths-hint.js must
// defer when status.js's mDNS probe is in flight so the user doesn't see
// the AtHS card AND the mDNS bookmark card stacked on first load.
//
// Contract:
//   - status.js sets window.__litclockMdnsPending = true synchronously
//     during its IIFE if it WILL probe (HTTP origin, not on .local,
//     not previously dismissed).
//   - After probe settles, status.js dispatches
//     `litclock:mdns-result` with {available: boolean}.
//   - aths-hint.js checks the flag in init() and either defers (waiting
//     for the event with a 4s defensive timeout) or proceeds immediately.
//
// Tests here exercise aths-hint.js with synthetic flag/event signals.
// status.js's own behavior is covered separately in status.mdns-probe.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript } from "./helpers/loadScript.js";

function buildDom() {
  // Minimal AtHS markup matching base.html.j2.
  document.body.innerHTML = `
    <aside class="aths-hint aths-hint--ios" role="region" aria-label="Add to Home Screen hint">
      <span class="aths-hint__icon aths-hint__icon--ios"></span>
      <span class="aths-hint__icon aths-hint__icon--android"></span>
      <div class="aths-hint__body">
        <p class="aths-hint__title">Add to Home Screen</p>
      </div>
      <button type="button" class="aths-hint__dismiss" aria-label="Dismiss hint">×</button>
    </aside>
    <nav class="tabbar"></nav>
  `;
}

// aths-hint.js wraps init() in setTimeout(0) at load time, then opens the
// card at +600ms inside proceedToShow(). Advance enough to clear both.
async function advancePastAthSOpen() {
  await vi.advanceTimersByTimeAsync(700);
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

describe("aths-hint.js — mDNS coordination (/review #406)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    try { window.localStorage.clear(); } catch (_e) { /* ignore */ }
    // Ensure NOT standalone (jsdom default) and not previously dismissed.
    delete window.__litclockMdnsPending;
  });

  afterEach(() => {
    vi.useRealTimers();
    try { window.localStorage.clear(); } catch (_e) { /* ignore */ }
    delete window.__litclockMdnsPending;
  });

  it("opens the AtHS card normally when no mDNS probe is pending", async () => {
    buildDom();
    // Flag intentionally NOT set — simulates /settings, /system, or any
    // page where status.js doesn't run.
    loadScript("aths-hint.js");
    await advancePastAthSOpen();

    const card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(true);
  });

  it("defers AtHS while mDNS probe is pending; suppresses on probe success", async () => {
    buildDom();
    window.__litclockMdnsPending = true;
    loadScript("aths-hint.js");

    // Advance past the 600ms open delay — card should NOT be open yet
    // because aths-hint deferred while waiting for the mDNS result.
    await advancePastAthSOpen();
    let card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(false);

    // Signal mDNS success — aths-hint must NOT open the card.
    document.dispatchEvent(new CustomEvent("litclock:mdns-result", {
      detail: { available: true },
    }));
    await vi.advanceTimersByTimeAsync(700);

    card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(false);
  });

  it("defers AtHS while mDNS pending; opens after probe-failure event", async () => {
    buildDom();
    window.__litclockMdnsPending = true;
    loadScript("aths-hint.js");

    await advancePastAthSOpen();
    let card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(false);

    // mDNS doesn't work → AtHS should fall through to its normal flow.
    document.dispatchEvent(new CustomEvent("litclock:mdns-result", {
      detail: { available: false },
    }));
    // Allow init's setTimeout(0) → proceedToShow → 600ms open timer.
    await vi.advanceTimersByTimeAsync(700);

    card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(true);
  });

  it("opens AtHS after the 4s defensive timeout if no result event fires", async () => {
    // Failsafe: if status.js's probe throws unexpectedly and never
    // dispatches the result event, AtHS must still fire so the user
    // gets the home-screen prompt eventually.
    buildDom();
    window.__litclockMdnsPending = true;
    loadScript("aths-hint.js");

    // Within the 4s window, card stays hidden.
    await vi.advanceTimersByTimeAsync(3500);
    let card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(false);

    // Past the 4s timeout + 600ms card-open delay.
    await vi.advanceTimersByTimeAsync(1500);
    card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(true);
  });
});
