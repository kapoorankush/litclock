// Behavior coverage for the post-WiFi handoff banner controller
// (EPIC #383 PR2, #388) — src/control_server/static/js/handoff.js — plus its
// coordination with aths-hint.js.
//
// Success state: the "Done" button POSTs /api/handoff/done, then the banner
// fades out and hands off to the AtHS hint.
// Failure state: the browser timezone (Intl) relabels the button and is POSTed
// to /api/handoff/set-timezone.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

// Query-aware matchMedia: reduce-motion → true (so handoff.js takes the
// synchronous teardown path, making the test deterministic), everything else
// (incl. display-mode: standalone, which aths-hint.js probes) → false.
function installMatchMedia() {
  window.matchMedia = (q) => ({
    matches: /prefers-reduced-motion/.test(q),
    media: q,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  });
}

function successBanner() {
  document.body.setAttribute("data-handoff-active", "");
  document.body.innerHTML = `
    <section class="handoff handoff--success" id="handoff-banner" data-handoff-state="success">
      <div class="handoff__sheet">
        <h2 id="handoff-heading">Setup complete</h2>
        <button type="button" class="handoff__btn" id="handoff-done">Done — Start the Clock</button>
      </div>
    </section>`;
}

function failureBanner() {
  document.body.setAttribute("data-handoff-active", "");
  document.body.innerHTML = `
    <section class="handoff handoff--failure" id="handoff-banner" data-handoff-state="failure">
      <div class="handoff__sheet">
        <h2 id="handoff-heading">Almost there<span class="handoff__period">.</span></h2>
        <p id="handoff-fail-body">We couldn’t detect your timezone.</p>
        <button type="button" class="handoff__btn" id="handoff-set-tz"
                data-tz-label-template="Use {tz}" data-fallback-label="Set my timezone">Set my timezone</button>
        <a class="handoff__link" href="/settings#weather">Pick a different timezone</a>
      </div>
    </section>`;
}

async function flush() {
  await new Promise((r) => setTimeout(r, 10));
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

describe("handoff.js — success state", () => {
  let mock;
  beforeEach(() => {
    installMatchMedia();
    mock = installFetchMock();
  });
  afterEach(() => {
    mock.restore();
    document.body.removeAttribute("data-handoff-active");
  });

  it("POSTs /api/handoff/done and tears the banner down on success", async () => {
    successBanner();
    mock.register(/\/api\/handoff\/done$/, { status: 200, body: { ok: true, complete: true } });

    loadScript("handoff.js");
    document.getElementById("handoff-done").click();
    await flush();

    const doneCalls = mock.calls.filter((c) => c.path === "/api/handoff/done");
    expect(doneCalls.length).toBe(1);
    expect(doneCalls[0].opts.method).toBe("POST");
    // Banner removed + body attribute cleared (Status hero un-dims).
    expect(document.getElementById("handoff-banner")).toBeNull();
    expect(document.body.hasAttribute("data-handoff-active")).toBe(false);
  });

  it("leaves the banner up when the server rejects", async () => {
    successBanner();
    mock.register(/\/api\/handoff\/done$/, { status: 500, body: { ok: false } });

    loadScript("handoff.js");
    document.getElementById("handoff-done").click();
    await flush();

    // Still present; user can retry or wait for the 120s timer.
    expect(document.getElementById("handoff-banner")).not.toBeNull();
    expect(document.body.hasAttribute("data-handoff-active")).toBe(true);
  });
});

describe("handoff.js — failure state (browser-tz fallback)", () => {
  let mock;
  let origDTF;
  beforeEach(() => {
    installMatchMedia();
    mock = installFetchMock();
    origDTF = Intl.DateTimeFormat;
    // Pin the browser timezone the controller reads.
    Intl.DateTimeFormat = function () {
      return { resolvedOptions: () => ({ timeZone: "America/Chicago" }) };
    };
  });
  afterEach(() => {
    Intl.DateTimeFormat = origDTF;
    mock.restore();
    document.body.removeAttribute("data-handoff-active");
  });

  it("relabels the button with the detected timezone", () => {
    failureBanner();
    loadScript("handoff.js");
    const btn = document.getElementById("handoff-set-tz");
    expect(btn.textContent).toBe("Use America/Chicago");
    expect(btn.getAttribute("data-timezone")).toBe("America/Chicago");
  });

  it("POSTs the detected timezone and tears down on success", async () => {
    failureBanner();
    mock.register(/\/api\/handoff\/set-timezone$/, { status: 200, body: { ok: true, complete: true } });

    loadScript("handoff.js");
    document.getElementById("handoff-set-tz").click();
    await flush();

    const tzCalls = mock.calls.filter((c) => c.path === "/api/handoff/set-timezone");
    expect(tzCalls.length).toBe(1);
    expect(JSON.parse(tzCalls[0].opts.body)).toEqual({ timezone: "America/Chicago" });
    expect(document.getElementById("handoff-banner")).toBeNull();
  });

  it("keeps the banner up when the server rejects the timezone", async () => {
    failureBanner();
    mock.register(/\/api\/handoff\/set-timezone$/, { status: 422, body: { ok: false } });

    loadScript("handoff.js");
    document.getElementById("handoff-set-tz").click();
    await flush();

    // POST was attempted; on reject the banner stays (user falls back to Settings).
    expect(mock.calls.filter((c) => c.path === "/api/handoff/set-timezone").length).toBe(1);
    expect(document.getElementById("handoff-banner")).not.toBeNull();
  });
});

describe("handoff.js — failure state with no browser timezone", () => {
  let mock;
  let origDTF;
  beforeEach(() => {
    installMatchMedia();
    mock = installFetchMock();
    origDTF = Intl.DateTimeFormat;
    // Browser can't report a timezone (rare, but possible).
    Intl.DateTimeFormat = function () {
      return { resolvedOptions: () => ({ timeZone: undefined }) };
    };
  });
  afterEach(() => {
    Intl.DateTimeFormat = origDTF;
    mock.restore();
    document.body.removeAttribute("data-handoff-active");
  });

  it("does not POST when no timezone is detected (routes to Settings instead)", async () => {
    failureBanner();
    loadScript("handoff.js");
    const btn = document.getElementById("handoff-set-tz");
    // Label stays the fallback; no data-timezone set.
    expect(btn.getAttribute("data-timezone")).toBeNull();
    btn.click();
    await flush();
    expect(mock.calls.filter((c) => c.path === "/api/handoff/set-timezone").length).toBe(0);
  });
});

describe("handoff.js + aths-hint.js coordination", () => {
  let mock;
  beforeEach(() => {
    installMatchMedia();
    mock = installFetchMock();
    try {
      window.localStorage.removeItem("litclock-aths-dismissed");
    } catch (_e) {
      /* ignore */
    }
  });
  afterEach(() => {
    mock.restore();
    document.body.removeAttribute("data-handoff-active");
  });

  function buildWithAths() {
    document.body.setAttribute("data-handoff-active", "");
    document.body.innerHTML = `
      <section class="handoff handoff--success" id="handoff-banner" data-handoff-state="success">
        <button type="button" id="handoff-done">Done — Start the Clock</button>
      </section>
      <aside class="aths-hint aths-hint--ios" role="region">
        <button type="button" class="aths-hint__dismiss">x</button>
      </aside>`;
  }

  it("suppresses the AtHS hint while the handoff banner is active", async () => {
    buildWithAths();
    loadScript("handoff.js");
    loadScript("aths-hint.js");
    // aths-hint's 600ms open timer must never have been scheduled (it bails on
    // data-handoff-active). Wait past the delay and confirm it stayed closed.
    await new Promise((r) => setTimeout(r, 650));
    const card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(false);
  });

  it("runs the AtHS hint after the handoff completes", async () => {
    buildWithAths();
    mock.register(/\/api\/handoff\/done$/, { status: 200, body: { ok: true, complete: true } });
    loadScript("handoff.js");
    loadScript("aths-hint.js");

    document.getElementById("handoff-done").click();
    await flush();
    // Banner gone, data-handoff-active cleared, litclock:handoff-complete fired
    // → aths-hint re-runs init and schedules its 600ms open.
    await new Promise((r) => setTimeout(r, 650));
    const card = document.querySelector(".aths-hint");
    expect(card.classList.contains("aths-hint--ready")).toBe(true);
  });
});
