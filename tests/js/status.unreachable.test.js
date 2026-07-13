// Behavior coverage for the #309 graceful-disconnect hero state in
// status.js. When /api/status polling fails (network error, timeout,
// non-OK HTTP), the hero swaps to a friendly "Couldn't reach LitClock"
// card with non-technical guidance. On the next successful poll, the
// card hides and the hero re-renders normally.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

function baseStatusPayload(extras = {}) {
  return {
    ok: true,
    stale: false,
    picked_at_age_s: null,
    quote: null,
    author: null,
    title: null,
    time: null,
    wifi_ssid: null,
    weather_city: null,
    version: null,
    uptime_human: "0s",
    last_update_version: null,
    last_update_at_relative: "—",
    phase3_skipped_at_unix: null,
    ...extras,
  };
}

function buildDom() {
  document.body.innerHTML = `
    <div data-status-stale-banner hidden><span data-status-stale-text></span></div>
    <div data-status-phase3-skip-banner hidden><span data-status-phase3-skip-text></span></div>
    <section>
      <div data-status-hero-full hidden>
        <blockquote data-status-quote></blockquote>
        <p>
          <span data-status-attr-prefix></span>
          <span data-status-attr-title-wrap hidden><em data-status-attr-title></em></span>
          <span data-status-attr-time-wrap hidden><span data-status-attr-time></span></span>
        </p>
      </div>
      <p data-status-hero-empty>Starting up…</p>
      <div data-status-hero-unreachable role="alert" hidden>
        <p><em>Couldn't reach LitClock.</em></p>
        <p>A few things that often help:</p>
        <ul>
          <li>Make sure your clock is plugged in and the screen is lit</li>
          <li>Try scanning the QR code on the clock again</li>
          <li>Check that your phone is on the same WiFi as the clock</li>
        </ul>
        <button type="button" data-status-retry>Tap to retry</button>
      </div>
    </section>
    <aside data-mdns-bookmark hidden>
      <button type="button" data-mdns-switch>Switch</button>
      <button type="button" data-mdns-dismiss>Not now</button>
    </aside>
    <dl>
      <div><dt>WiFi</dt><dd data-status-wifi>—</dd></div>
      <div><dt>Weather</dt><dd data-status-weather>—</dd></div>
      <div><dt>Version</dt><dd data-status-version>—</dd></div>
      <div><dt>Uptime</dt><dd data-status-uptime>—</dd></div>
      <div><dt>Last update</dt>
        <dd data-status-last-update>
          <span data-status-last-update-version hidden></span>
          <span data-status-last-update-sep hidden>,&nbsp;</span>
          <span data-status-last-update-relative>—</span>
        </dd>
      </div>
    </dl>
  `;
}

// Mirrors the cushion used by sibling status.js tests. status.js settles
// across rAF + a small microtask chain.
async function flushRefresh() {
  await new Promise((r) => setTimeout(r, 60));
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("status.js — #309 graceful disconnect hero state", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
  });

  it("reveals the unreachable card when /api/status network-fails", async () => {
    buildDom();
    // Function form so the rejection only materializes on each fetch call
    // — registering a literal Promise.reject(...) triggers Vitest's
    // unhandled-rejection tracker synchronously, before status.js's
    // .catch() has a chance to attach.
    mock.register(/\/api\/status$/, () => Promise.reject(new Error("network down")));

    loadScript("status.js");
    await flushRefresh();

    const card = document.querySelector("[data-status-hero-unreachable]");
    const heroFull = document.querySelector("[data-status-hero-full]");
    const heroEmpty = document.querySelector("[data-status-hero-empty]");

    expect(card.hidden).toBe(false);
    expect(heroFull.hidden).toBe(true);
    expect(heroEmpty.hidden).toBe(true);
  });

  it("reveals the unreachable card when /api/status returns 500", async () => {
    buildDom();
    mock.register(/\/api\/status$/, { status: 500, body: "{}" });

    loadScript("status.js");
    await flushRefresh();

    const card = document.querySelector("[data-status-hero-unreachable]");
    expect(card.hidden).toBe(false);
  });

  it("re-hides the unreachable card on the next successful poll", async () => {
    buildDom();
    // Start unreachable (server-rendered visible to simulate a prior failure).
    document.querySelector("[data-status-hero-unreachable]").hidden = false;
    document.querySelector("[data-status-hero-full]").hidden = true;
    document.querySelector("[data-status-hero-empty]").hidden = true;

    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({
        quote: "It was the best of times.",
        author: "Charles Dickens",
        title: "A Tale of Two Cities",
        time: "12:00",
      }),
    });

    loadScript("status.js");
    await flushRefresh();

    const card = document.querySelector("[data-status-hero-unreachable]");
    const heroFull = document.querySelector("[data-status-hero-full]");
    expect(card.hidden).toBe(true);
    expect(heroFull.hidden).toBe(false);
  });

  it("re-fetches when the retry button is clicked, restoring hero on success", async () => {
    buildDom();
    // First poll fails.
    let callCount = 0;
    mock.register(/\/api\/status$/, () => {
      callCount += 1;
      if (callCount === 1) return Promise.reject(new Error("network down"));
      return {
        ok: true,
        status: 200,
        json: async () =>
          baseStatusPayload({
            quote: "All happy families are alike.",
            author: "Leo Tolstoy",
            time: "08:00",
          }),
      };
    });

    loadScript("status.js");
    await flushRefresh();

    // After failure, card is showing.
    const card = document.querySelector("[data-status-hero-unreachable]");
    expect(card.hidden).toBe(false);

    // User taps retry.
    document.querySelector("[data-status-retry]").click();
    await flushRefresh();

    expect(card.hidden).toBe(true);
    expect(document.querySelector("[data-status-hero-full]").hidden).toBe(false);
    expect(document.querySelector("[data-status-quote]").textContent).toBe(
      "All happy families are alike."
    );
  });

  it("hides the mDNS bookmark card when the unreachable state is shown (D2)", async () => {
    // /review design finding D2: hero-unreachable retry and mdns-bookmark
    // switch are both Primary-styled. DESIGN.md "Buttons" locks one
    // Primary per screen. When unreachable shows, suppress bookmark.
    buildDom();
    // Simulate prior mDNS probe success (bookmark visible) followed by
    // an API failure.
    document.querySelector("[data-mdns-bookmark]").hidden = false;
    mock.register(/\/api\/status$/, () => Promise.reject(new Error("network down")));

    loadScript("status.js");
    await flushRefresh();

    expect(document.querySelector("[data-status-hero-unreachable]").hidden).toBe(false);
    expect(document.querySelector("[data-mdns-bookmark]").hidden).toBe(true);
  });

  it("routes 200-OK responses where the server reports ok:false through unreachable", async () => {
    // /review adversarial finding A7: 200-OK with {ok: false} = server is
    // reachable but not usable, user can't act from the PWA. Better to
    // show "Couldn't reach LitClock" honestly than leave them staring at
    // a stale hero that looks fine but won't refresh. User-approved
    // 2026-05-28 (Q1 of review).
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: { ok: false, error: "internal" },
    });

    loadScript("status.js");
    await flushRefresh();

    const card = document.querySelector("[data-status-hero-unreachable]");
    expect(card.hidden).toBe(false);
  });

  it("refreshes when the tab regains visibility (testing specialist T11)", async () => {
    // status.js attaches a visibilitychange handler that calls refresh()
    // when the tab is brought back to the foreground. Previously zero
    // coverage — a regression that breaks the polarity (firing on hidden
    // instead of visible, or never firing) would slip through.
    buildDom();
    let mockMode = "fail";
    mock.register(/\/api\/status$/, () => {
      if (mockMode === "fail") return Promise.reject(new Error("down"));
      return {
        ok: true,
        status: 200,
        json: async () => baseStatusPayload({ quote: "Welcome back.", author: "X", time: "12:00" }),
      };
    });

    loadScript("status.js");
    await flushRefresh();
    const card = document.querySelector("[data-status-hero-unreachable]");
    expect(card.hidden).toBe(false); // initial failure

    // Recover the server. Without the visibilitychange refresh, the next
    // poll wouldn't fire for 30s — but a foreground event must trigger
    // immediately so a user returning to the app sees fresh content.
    mockMode = "success";
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushRefresh();

    expect(card.hidden).toBe(true);
    expect(document.querySelector("[data-status-hero-full]").hidden).toBe(false);
  });

  it("recovers on fail→success→fail cycles (testing specialist T7)", async () => {
    // Multi-cycle coverage: a single failure shows the card, a success
    // hides it, a subsequent failure must SHOW IT AGAIN. A regression in
    // `pending` flag management or showUnreachable() polarity could break
    // the second-failure case while passing the simpler single-direction
    // tests above.
    //
    // Mock uses a switchable mode flag instead of a call counter — the
    // status.js IIFE registers a document-level click listener that
    // accumulates across loadScript() calls in prior tests, so each retry
    // click can fire N parallel refreshes where N = prior loads. A
    // count-based mock would land all N calls in different "buckets" and
    // confuse the test. Mode-based mock keeps every parallel refresh in
    // the same logical state.
    buildDom();
    let mockMode = "fail";
    mock.register(/\/api\/status$/, () => {
      if (mockMode === "fail") return Promise.reject(new Error("down"));
      return {
        ok: true,
        status: 200,
        json: async () => baseStatusPayload({ quote: "Hello.", author: "X", time: "12:00" }),
      };
    });

    loadScript("status.js");
    await flushRefresh();
    const card = document.querySelector("[data-status-hero-unreachable]");
    expect(card.hidden).toBe(false); // first failure

    // Switch to success and trigger via retry-click.
    mockMode = "success";
    document.querySelector("[data-status-retry]").click();
    await flushRefresh();
    expect(card.hidden).toBe(true); // hidden after success
    expect(document.querySelector("[data-status-hero-full]").hidden).toBe(false);

    // Switch back to failure and trigger via retry-click. The card MUST
    // reappear — the regression hazard this test guards against.
    mockMode = "fail";
    document.querySelector("[data-status-retry]").click();
    await flushRefresh();
    expect(card.hidden).toBe(false); // reappears after second failure
  });
});
