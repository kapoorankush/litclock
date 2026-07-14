// Behavior coverage for the #274 follow-up #5 Status hero "Phase 3 skip"
// banner in status.js. The banner shows when update.sh's last Phase 3 run
// hit a flock timeout (rc=75) and skipped the env.sh.sample merge — the
// reader-side staleness clamp lives server-side (status route returns null
// when the marker is > 1 day old), so the JS just toggles [hidden] on the
// phase3_skipped_at_unix field's truthiness.

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
    update_state: null,
    update_phase_index: null,
    ...extras,
  };
}

// Mirror the relevant data-status-* hooks from status.html.j2. Same scaffold
// approach as status.shim.test.js — if production markup drifts, the
// SSR contract tests in tests/test_control_server.py::TestPhase3SkipMarkerSurface
// flag it; the JS test focuses on patch() behavior given the contracted DOM.
function buildDom({ phase3BannerHidden = true } = {}) {
  document.body.innerHTML = `
    <div data-status-stale-banner hidden>
      <span data-status-stale-text></span>
    </div>
    <div data-status-phase3-skip-banner${phase3BannerHidden ? " hidden" : ""}>
      <span data-status-phase3-skip-text>
        Last update skipped the env-vars merge — will retry next Sunday.
      </span>
    </div>
    <section>
      <div data-status-hero-full hidden>
        <blockquote data-status-quote></blockquote>
        <p>
          <span data-status-attr-prefix></span>
          <span data-status-attr-title-wrap hidden><em data-status-attr-title></em></span>
          <span data-status-attr-time-wrap hidden><span data-status-attr-time></span></span>
        </p>
      </div>
      <p data-status-hero-empty></p>
    </section>
    <dl>
      <div><dt>WiFi</dt><dd data-status-wifi>—</dd></div>
      <div><dt>Weather</dt><dd data-status-weather>—</dd></div>
      <div><dt>Version</dt><dd data-status-version>—</dd></div>
      <div><dt>Uptime</dt><dd data-status-uptime>—</dd></div>
      <div><dt>Last update</dt>
        <dd data-status-last-update>
          <span class="mono" data-status-last-update-version hidden></span>
          <span data-status-last-update-sep hidden>,&nbsp;</span>
          <span data-status-last-update-relative>—</span>
        </dd>
      </div>
    </dl>
  `;
}

// Same flushRefresh strategy as status.shim.test.js — status.js kicks off
// via requestAnimationFrame then settles through a microtask chain. 60ms +
// 4 microtask awaits is enough cushion across reasonable CI hosts.
async function flushRefresh() {
  await new Promise((r) => setTimeout(r, 60));
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("status.js — Phase 3 skip banner (#274 followup #5)", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
  });

  it("shows the banner when phase3_skipped_at_unix is non-null", async () => {
    buildDom({ phase3BannerHidden: true });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({
        // Server only returns this when the marker is fresh; JS doesn't
        // re-compute staleness, just toggles on truthiness.
        phase3_skipped_at_unix: Date.now() / 1000 - 600,
      }),
    });

    loadScript("status.js");
    await flushRefresh();

    const banner = document.querySelector("[data-status-phase3-skip-banner]");
    expect(banner).not.toBeNull();
    expect(banner.hidden).toBe(false);
  });

  it("hides the banner when phase3_skipped_at_unix is null", async () => {
    // Banner started visible (server-side rendered with a marker);
    // a subsequent poll where the marker has cleared should toggle it back.
    buildDom({ phase3BannerHidden: false });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ phase3_skipped_at_unix: null }),
    });

    loadScript("status.js");
    await flushRefresh();

    const banner = document.querySelector("[data-status-phase3-skip-banner]");
    expect(banner.hidden).toBe(true);
  });

  it("ignores zero as a falsy value (server never sends 0 but pin it)", async () => {
    // Defense-in-depth: epoch 0 (1970-01-01) would be ~57 years stale and
    // would never pass the server-side freshness clamp. But if a buggy
    // serializer ever sent 0, JS should treat it as "no marker" and hide.
    buildDom({ phase3BannerHidden: false });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ phase3_skipped_at_unix: 0 }),
    });

    loadScript("status.js");
    await flushRefresh();

    const banner = document.querySelector("[data-status-phase3-skip-banner]");
    expect(banner.hidden).toBe(true);
  });

  it("does not interfere with the stale banner state", async () => {
    // Both banners independently controlled by their own fields. A Pi can
    // be stale AND have a Phase 3 skip simultaneously.
    buildDom({ phase3BannerHidden: true });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({
        stale: true,
        picked_at_age_s: 600,
        phase3_skipped_at_unix: Date.now() / 1000 - 600,
      }),
    });

    loadScript("status.js");
    await flushRefresh();

    const staleBanner = document.querySelector("[data-status-stale-banner]");
    const phase3Banner = document.querySelector("[data-status-phase3-skip-banner]");
    expect(staleBanner.hidden).toBe(false);
    expect(phase3Banner.hidden).toBe(false);
  });
});
