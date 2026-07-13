// Behavior coverage for the #309 (piece 3) mDNS bookmark probe in
// status.js. After the PWA loads via IP, status.js silently probes
// http://litclock.local/api/health with a READABLE (CORS) fetch and
// checks the body for `app === 'litclock'` (#487 — identity, not just
// reachability). Only a real LitClock reveals the bookmark card offering a
// one-tap switch to the address-stable URL. Silent on failure OR on any
// responder that isn't our clock.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

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
        <button type="button" data-status-retry>Tap to retry</button>
      </div>
    </section>
    <aside data-mdns-bookmark hidden>
      <p>A more reliable link is available.</p>
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

// status.js sets the probe behind setTimeout(2000ms). Use fake timers so
// tests don't burn 2s each. installFetchMock is unaffected — it only
// replaces globalThis.fetch.
async function advancePastProbe() {
  // Probe is scheduled at 2000ms; advance a bit further to clear the
  // optional 1500ms AbortController fallback timer too.
  await vi.advanceTimersByTimeAsync(2100);
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("status.js — #309 (3) mDNS bookmark probe", () => {
  let mock;
  let originalLocation;
  let assignSpy;

  beforeEach(() => {
    vi.useFakeTimers();
    mock = installFetchMock();
    // Status poll registration so the first refresh doesn't reject and
    // pollute the test signal — the mDNS probe is the only thing we're
    // asserting on here.
    mock.register(/\/api\/status$/, {
      status: 200,
      body: { ok: true, quote: null, uptime_human: "0s", last_update_at_relative: "—" },
    });

    // Fresh localStorage per test. jsdom carries it across tests by default.
    try { window.localStorage.clear(); } catch (e) { /* ignore */ }

    // jsdom's window.location is a real Location instance whose .assign /
    // .replace / inner properties are non-writable. To capture navigation
    // calls without actually navigating, replace the whole window.location
    // with a plain object that mirrors the surface status.js touches.
    // (window.location itself IS configurable on the jsdom window, so the
    // delete-then-reassign works even though its inner properties aren't.)
    originalLocation = window.location;
    assignSpy = vi.fn();
    delete window.location;
    window.location = {
      // protocol MUST be set or status.js's HTTPS-skip guard
      // (probeMdns: `if (window.location.protocol !== 'http:') return`)
      // short-circuits every test that expects the probe to fire.
      // jsdom's default origin is http://localhost; mirror that.
      protocol: "http:",
      hostname: originalLocation.hostname,
      // #343: control_server is on port 80, so window.location.port is '' —
      // controlUrl() must then omit the port from the probe/switch target.
      port: "",
      pathname: originalLocation.pathname,
      search: originalLocation.search,
      href: originalLocation.href,
      assign: assignSpy,
      replace: vi.fn(),
    };
  });

  afterEach(() => {
    mock.restore();
    vi.useRealTimers();
    delete window.location;
    window.location = originalLocation;
    try { window.localStorage.clear(); } catch (e) { /* ignore */ }
  });

  it("reveals the bookmark card when the probe resolves", async () => {
    buildDom();
    // Probe URL pathname is /api/health — register a successful response.
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(false);
  });

  it("keeps the card hidden when a NON-LitClock device answers on the control port (#487 identity)", async () => {
    // The whole point of the readable/identity probe: some other `.local` device
    // could answer /api/health on the control port with 200 + JSON, but without our identity
    // marker. The old opaque no-cors probe would have false-positived and offered
    // to Switch the user onto a stranger's page. Now we fail closed.
    buildDom();
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "some-other-thing" } });

    loadScript("status.js");
    await advancePastProbe();

    expect(document.querySelector("[data-mdns-bookmark]").hidden).toBe(true);
  });

  it("keeps the card hidden when /api/health answers non-200", async () => {
    buildDom();
    mock.register(/\/api\/health$/, { status: 503, body: { ok: false } });

    loadScript("status.js");
    await advancePastProbe();

    expect(document.querySelector("[data-mdns-bookmark]").hidden).toBe(true);
  });

  it("keeps the bookmark card hidden when the probe rejects", async () => {
    buildDom();
    // Function form (not literal Promise.reject) — avoids surfacing the
    // rejection to Vitest's unhandled-rejection tracker before the
    // production .catch() in probeMdns() attaches.
    mock.register(/\/api\/health$/, () => Promise.reject(new Error("name not resolved")));

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(true);
  });

  it("does not probe (or show card) when the dismissed flag is set", async () => {
    buildDom();
    window.localStorage.setItem("litclock.mdns-bookmark-dismissed", "dismissed");
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(true);
    const healthCalls = mock.calls.filter((c) => c.path === "/api/health");
    expect(healthCalls.length).toBe(0);
  });

  it("does not probe when already on litclock.local", async () => {
    buildDom();
    // The replacement window.location set up in beforeEach is a plain
    // object, so this is just a property write — no defineProperty dance.
    window.location.hostname = "litclock.local";
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(true);
    const healthCalls = mock.calls.filter((c) => c.path === "/api/health");
    expect(healthCalls.length).toBe(0);
  });

  it("does not probe when loaded over HTTPS (mixed-content would block)", async () => {
    // /review adversarial finding A1: probing `http://litclock.local`
    // from an HTTPS origin is blocked as mixed active content. Stay
    // silent rather than fire-and-fail (the .catch would hide it, but
    // we'd also be one design choice away from a misleading "Switch"
    // prompt that triggers a downgrade interstitial on navigation).
    buildDom();
    window.location.protocol = "https:";
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(true);
    const healthCalls = mock.calls.filter((c) => c.path === "/api/health");
    expect(healthCalls.length).toBe(0);
  });

  it("persists dismissal and hides the card when Not now is tapped", async () => {
    buildDom();
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(false);

    document.querySelector("[data-mdns-dismiss]").click();

    expect(card.hidden).toBe(true);
    expect(window.localStorage.getItem("litclock.mdns-bookmark-dismissed"))
      .toBe("dismissed");
  });

  it("aborts the probe via AbortController after 1500ms if fetch hangs (testing specialist T10)", async () => {
    // Failure mode: mDNS-resolves-but-TCP-never-completes (some captive
    // portals, partial network failures). Without the AbortController
    // timeout, the bookmark card promise would hang forever and the
    // pending state would never settle. Verify the timer abort fires
    // by holding fetch open past the 1500ms timeout and asserting the
    // card stays hidden (the abort routes to .catch, which is silent).
    buildDom();
    mock.register(/\/api\/health$/, () => new Promise(() => { /* never resolves */ }));

    loadScript("status.js");
    // Probe schedules at 2000ms; timeout fires at 2000+1500 = 3500ms.
    // Advance past both with cushion.
    await vi.advanceTimersByTimeAsync(4000);
    for (let i = 0; i < 4; i++) await Promise.resolve();

    const card = document.querySelector("[data-mdns-bookmark]");
    expect(card.hidden).toBe(true);
    // Probe was attempted (assertion: we didn't bail early), then aborted.
    const healthCalls = mock.calls.filter((c) => c.path === "/api/health");
    expect(healthCalls.length).toBe(1);
  });

  it("navigates to .local and marks 'switched' when Switch is tapped", async () => {
    buildDom();
    mock.register(/\/api\/health$/, { status: 200, body: { ok: true, app: "litclock" } });

    loadScript("status.js");
    await advancePastProbe();

    document.querySelector("[data-mdns-switch]").click();

    expect(window.location.assign).toHaveBeenCalledTimes(1);
    const target = window.location.assign.mock.calls[0][0];
    // #343: bare host, no port (page is on 80 so location.port is '').
    expect(target.startsWith("http://litclock.local/")).toBe(true);
    expect(target).not.toContain("litclock.local:");
    expect(window.localStorage.getItem("litclock.mdns-bookmark-dismissed"))
      .toBe("switched");
  });
});
