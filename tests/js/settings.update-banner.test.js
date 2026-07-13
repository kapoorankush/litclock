// Behavior coverage for the #274 follow-up #2 Settings tab "Update in
// progress" banner in settings.js. The banner surfaces when an update.sh
// run is mid-flight in Phase 3 (env.sh merge — holds the shared sidecar
// flock) or Phase 4 (pip install — the long-running phase where users
// are most likely to be staring at Settings). settings.js polls /api/status
// every 15s when the Settings tab is visible; the banner toggles based on
// the {update_state, update_phase_index} pair.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

// Minimal /api/status payload — only the two new fields matter here, but
// the JS expects the rest of the shape for forward compat with future
// settings.js polls that might look at other fields.
function baseStatusPayload(extras = {}) {
  return {
    ok: true,
    stale: false,
    last_update_version: null,
    last_update_at_relative: "—",
    phase3_skipped_at_unix: null,
    update_state: null,
    update_phase_index: null,
    ...extras,
  };
}

// Mirror the data-settings-update-banner element from settings.html.j2.
// The CSRF/save-form scaffolding settings.js wires against is irrelevant
// to the banner code path; keep the DOM minimal so the test reads cleanly.
function buildDom() {
  document.body.innerHTML = `
    <div data-settings-update-banner hidden>
      <span data-settings-update-banner-text>
        Auto-update in progress — Save may briefly block while env-vars sync.
      </span>
    </div>
    <form class="settings-form"></form>
  `;
}

// settings.js's banner code path is synchronous-ish: it calls fetch
// immediately on load, then on each setInterval tick. We need to let the
// fetch promise + .then() chain resolve before assertions. 50ms + a few
// microtask awaits is sufficient on any reasonable CI host.
async function flushInitialPoll() {
  await new Promise((r) => setTimeout(r, 50));
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("settings.js — update-in-progress banner (#274 followup #2)", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
    // jsdom defaults document.visibilityState to 'visible' — settings.js's
    // start gate relies on this, so we leave it alone.
  });

  afterEach(() => {
    mock.restore();
  });

  it("shows the banner when state=running AND phase_index=3", async () => {
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: "running", update_phase_index: 3 }),
    });

    loadScript("settings.js");
    await flushInitialPoll();

    const banner = document.querySelector("[data-settings-update-banner]");
    expect(banner).not.toBeNull();
    expect(banner.hidden).toBe(false);
  });

  it("shows the banner when state=running AND phase_index=4 (pip install)", async () => {
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: "running", update_phase_index: 4 }),
    });

    loadScript("settings.js");
    await flushInitialPoll();

    expect(document.querySelector("[data-settings-update-banner]").hidden).toBe(false);
  });

  it("hides the banner when state=running but phase is not 3 or 4", async () => {
    // Phases 1, 2, 5, 6, 7 are quick (< few seconds each) and don't hold
    // the env.sh flock — surfacing the banner would just flash uselessly.
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: "running", update_phase_index: 1 }),
    });

    loadScript("settings.js");
    await flushInitialPoll();

    expect(document.querySelector("[data-settings-update-banner]").hidden).toBe(true);
  });

  it("hides the banner when state=complete (post-update)", async () => {
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: "complete", update_phase_index: 7 }),
    });

    loadScript("settings.js");
    await flushInitialPoll();

    expect(document.querySelector("[data-settings-update-banner]").hidden).toBe(true);
  });

  it("hides the banner when no update is in flight (both fields null)", async () => {
    // Common steady-state — the API contract returns both as null when
    // /run/litclock/update.status is absent.
    buildDom();
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: null, update_phase_index: null }),
    });

    loadScript("settings.js");
    await flushInitialPoll();

    expect(document.querySelector("[data-settings-update-banner]").hidden).toBe(true);
  });

  it("survives a fetch failure without crashing or unhiding the banner", async () => {
    // Network blip on the poll — the banner state should not flip just
    // because a poll dropped. Critical because the Settings page must
    // stay usable even if /api/status briefly hiccups (e.g., during the
    // 10s service restart window in update.sh Phase 7).
    buildDom();
    mock.register(/\/api\/status$/, Promise.reject(new Error("network down")));

    loadScript("settings.js");
    await flushInitialPoll();

    const banner = document.querySelector("[data-settings-update-banner]");
    expect(banner).not.toBeNull();
    // Banner started hidden on SSR and stays hidden — no spurious show.
    expect(banner.hidden).toBe(true);
  });

  it("does not throw when the banner element is missing (no-op gate)", async () => {
    // If a future template refactor drops the banner element, settings.js
    // must not throw — the early `if (banner)` gate handles this.
    document.body.innerHTML = `<form class="settings-form"></form>`;
    mock.register(/\/api\/status$/, {
      status: 200,
      body: baseStatusPayload({ update_state: "running", update_phase_index: 3 }),
    });

    // No assertion failure means loadScript + the initial poll didn't throw.
    expect(() => {
      loadScript("settings.js");
    }).not.toThrow();
    await flushInitialPoll();
  });
});
