// Tests for #337's settings.js additions:
//   * A12 segmented-pill click handler (Location mode + Temperature units)
//   * A14 composite-cache-key blur preview + stale-on-typing dim
//   * A17 Advanced lat/lon cleared on pill→Auto switch
//   * A10 Save-disabled rule for Specific + empty Place
//   * A13 Temperature pill auto-save (single round-trip per click)
//   * A18 browser-tz fallback (Intl detection + endpoint call)
//
// Pattern mirrors the existing settings.update-banner.test.js + #346
// Weather-toggle tests — DOM stubbed to match the post-A9 template shape,
// fetch mocked, loadScript runs the IIFE against the prepared globals.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

// Mirror the new Location + Temperature section markup (settings.html.j2
// post-#337). Only the elements settings.js queries are included — the
// banner / form-submit handlers run their own checks for unrelated DOM.
function buildDom({ mode = "auto", lat = "", lon = "", name = "" } = {}) {
  const autoHidden = mode === "auto" ? "" : "hidden";
  const specificHidden = mode === "specific" ? "" : "hidden";
  const autoChecked = mode === "auto" ? "checked" : "";
  const specificChecked = mode === "specific" ? "checked" : "";
  const autoSel = mode === "auto" ? "is-selected" : "";
  const specSel = mode === "specific" ? "is-selected" : "";
  document.body.innerHTML = `
    <form class="settings-form" data-section="location">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="location">
      <fieldset class="settings-row settings-row--pill">
        <div class="settings-segmented" role="radiogroup" data-mode-pill>
          <label class="settings-segmented__opt ${autoSel}" data-mode-opt="auto">
            <input type="radio" class="settings-segmented__input" name="WEATHER_LOCATION_MODE" value="auto" ${autoChecked}>
            <span>Automatic</span>
          </label>
          <label class="settings-segmented__opt ${specSel}" data-mode-opt="specific">
            <input type="radio" class="settings-segmented__input" name="WEATHER_LOCATION_MODE" value="specific" ${specificChecked}>
            <span>Specific</span>
          </label>
        </div>
      </fieldset>
      <div data-mode-panel="auto" ${autoHidden ? "hidden" : ""}>
        <span class="settings-currently-row__value">${name || "(not detected yet)"}</span>
      </div>
      <fieldset class="settings-mode-panel" data-mode-panel="specific" ${specificHidden ? "hidden disabled" : ""}>
        <input type="text" id="location_query" name="location_query" value="">
        <span data-current-location>${name || "—"}</span>
        <label class="settings-checkbox-row__head">
          <input type="checkbox" id="location_worldwide" name="worldwide" value="on">
        </label>
        <details class="settings-details" data-advanced>
          <summary>Advanced</summary>
          <input type="text" id="weather_latitude" name="WEATHER_LATITUDE" value="${lat}" data-advanced-lat>
          <input type="text" id="weather_longitude" name="WEATHER_LONGITUDE" value="${lon}" data-advanced-lon>
        </details>
      </fieldset>
      <div class="settings-form__actions">
        <button type="submit" data-location-save>Save</button>
      </div>
    </form>
  `;
}

function buildTempDom({ units = "imperial" } = {}) {
  const fSel = units === "imperial" ? "is-selected" : "";
  const cSel = units === "metric" ? "is-selected" : "";
  const fChk = units === "imperial" ? "checked" : "";
  const cChk = units === "metric" ? "checked" : "";
  document.body.innerHTML = `
    <form class="settings-form" data-section="units">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="units">
      <fieldset class="settings-row settings-row--pill">
        <div class="settings-segmented" role="radiogroup" data-temp-pill>
          <label class="settings-segmented__opt ${fSel}" data-units-opt="imperial">
            <input type="radio" class="settings-segmented__input" name="WEATHER_UNITS" value="imperial" ${fChk}>
            <span>Fahrenheit</span>
          </label>
          <label class="settings-segmented__opt ${cSel}" data-units-opt="metric">
            <input type="radio" class="settings-segmented__input" name="WEATHER_UNITS" value="metric" ${cChk}>
            <span>Celsius</span>
          </label>
        </div>
      </fieldset>
    </form>
  `;
}

function buildTzDom() {
  document.body.innerHTML = `
    <p class="settings-tz-fallback" data-tz-fallback hidden>
      Couldn't detect location.
      <button type="button" data-browser-tz-btn>
        Use my browser's timezone (<span data-browser-tz-label>—</span>)
      </button>
    </p>
  `;
}

let mock;

beforeEach(() => {
  mock = installFetchMock();
  // /api/csrf always succeeds in these tests unless overridden.
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh-token" } });
  // /api/status used by the always-on update-banner poller in settings.js.
  // Default to nothing-happening so the banner-wiring code doesn't interfere.
  mock.register(/^\/api\/status$/, {
    status: 200,
    body: { ok: true, update_state: null, update_phase_index: null },
  });
});

afterEach(() => {
  mock.restore();
  document.body.innerHTML = "";
  document.documentElement.classList.remove("has-js");
});

// ── A12: pill click activates the corresponding mode ────────────────────

describe("#337 A12 — Location mode pill", () => {
  it("clicking the Specific label activates that mode + shows the panel", () => {
    buildDom({ mode: "auto" });
    loadScript("settings.js");

    const specOpt = document.querySelector('[data-mode-opt="specific"]');
    const specPanel = document.querySelector('[data-mode-panel="specific"]');
    const autoPanel = document.querySelector('[data-mode-panel="auto"]');

    expect(specPanel.hidden).toBe(true);
    expect(autoPanel.hidden).toBe(false);

    specOpt.click();

    expect(specOpt.classList.contains("is-selected")).toBe(true);
    expect(specPanel.hidden).toBe(false);
    expect(autoPanel.hidden).toBe(true);
  });

  it("A17: switching to Automatic clears the Advanced lat/lon inputs visually", () => {
    buildDom({ mode: "specific", lat: "28.62", lon: "77.22" });
    loadScript("settings.js");

    const lat = document.querySelector("[data-advanced-lat]");
    const lon = document.querySelector("[data-advanced-lon]");
    const autoOpt = document.querySelector('[data-mode-opt="auto"]');

    expect(lat.value).toBe("28.62");
    expect(lon.value).toBe("77.22");

    autoOpt.click();

    expect(lat.value).toBe("");
    expect(lon.value).toBe("");
  });

  it("/review P0 (Codex): switching to Automatic also DISABLES the specific fieldset so hidden form fields don't submit", () => {
    // Regression for the Codex-found bug where the <div hidden> approach
    // submitted empty WEATHER_LATITUDE / WEATHER_LONGITUDE on Auto save,
    // triggering the server's all-or-none guard and 422-ing the save.
    // The fix wraps the Specific panel in <fieldset disabled> when in Auto
    // mode — disabled form controls are EXCLUDED from native submission.
    buildDom({ mode: "specific" });
    loadScript("settings.js");

    const specPanel = document.querySelector('[data-mode-panel="specific"]');
    const autoOpt = document.querySelector('[data-mode-opt="auto"]');
    expect(specPanel.disabled).toBe(false);
    expect(specPanel.hidden).toBe(false);

    autoOpt.click();

    expect(specPanel.disabled).toBe(true);
    expect(specPanel.hidden).toBe(true);

    // Also verify: form serialization treats the inner inputs as excluded.
    // FormData skips form controls inside a disabled fieldset.
    const form = document.querySelector("form");
    const fd = new FormData(form);
    expect(fd.has("WEATHER_LATITUDE")).toBe(false);
    expect(fd.has("WEATHER_LONGITUDE")).toBe(false);
    expect(fd.has("location_query")).toBe(false);
  });
});

// ── A10 + A14: Save disabled when Specific + empty Place ────────────────

describe("#337 A10 — Save-disabled rule", () => {
  it("disables Save when MODE=specific AND Place is empty AND Advanced is empty", () => {
    buildDom({ mode: "specific" });
    loadScript("settings.js");

    const save = document.querySelector("[data-location-save]");
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("aria-disabled")).toBe("true");
  });

  it("enables Save when MODE=specific AND user types in Place", () => {
    buildDom({ mode: "specific" });
    loadScript("settings.js");

    const query = document.getElementById("location_query");
    const save = document.querySelector("[data-location-save]");
    expect(save.disabled).toBe(true);

    query.value = "Mumbai";
    query.dispatchEvent(new Event("input", { bubbles: true }));

    expect(save.disabled).toBe(false);
    expect(save.hasAttribute("aria-disabled")).toBe(false);
  });

  it("enables Save when MODE=specific AND Advanced lat/lon are filled (Place stays empty)", () => {
    buildDom({ mode: "specific" });
    loadScript("settings.js");

    const lat = document.querySelector("[data-advanced-lat]");
    const lon = document.querySelector("[data-advanced-lon]");
    const save = document.querySelector("[data-location-save]");
    expect(save.disabled).toBe(true);

    lat.value = "28.62";
    lat.dispatchEvent(new Event("input", { bubbles: true }));
    lon.value = "77.22";
    lon.dispatchEvent(new Event("input", { bubbles: true }));

    expect(save.disabled).toBe(false);
  });

  it("Save is always enabled in Automatic mode", () => {
    buildDom({ mode: "auto" });
    loadScript("settings.js");

    const save = document.querySelector("[data-location-save]");
    expect(save.disabled).toBe(false);
  });
});

// ── A14: composite cache key + stale-on-typing dim ──────────────────────

describe("#337 A14 — blur preview composite cache key + stale dim", () => {
  async function flush() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  }

  it("blur fires /api/geocode with worldwide=1 when checkbox is checked", async () => {
    buildDom({ mode: "specific" });
    mock.register(/^\/api\/geocode$/, {
      status: 200,
      body: { ok: true, short_name: "London, England", display_name: "Buckingham Palace, London, England" },
    });
    loadScript("settings.js");

    const query = document.getElementById("location_query");
    const ww = document.getElementById("location_worldwide");
    query.value = "SW1A 1AA";
    ww.checked = true;
    query.dispatchEvent(new Event("blur"));

    await flush();
    const geocodeCalls = mock.calls.filter((c) => c.path === "/api/geocode");
    expect(geocodeCalls).toHaveLength(1);
    expect(geocodeCalls[0].url).toContain("worldwide=1");
    expect(geocodeCalls[0].url).toContain("q=SW1A");
  });

  it("checkbox flip re-fires preview when Place input is non-empty (composite cache key)", async () => {
    buildDom({ mode: "specific" });
    let counter = 0;
    mock.register(/^\/api\/geocode$/, () => {
      counter += 1;
      return { ok: true, status: 200, json: async () => ({ ok: true, short_name: `result-${counter}` }) };
    });
    loadScript("settings.js");

    const query = document.getElementById("location_query");
    const ww = document.getElementById("location_worldwide");
    query.value = "SW1A 1AA";
    query.dispatchEvent(new Event("blur"));
    await Promise.resolve();
    await Promise.resolve();

    // Same query, different worldwide flag → cache invalidates, preview fires again.
    ww.checked = true;
    ww.dispatchEvent(new Event("change"));
    await Promise.resolve();
    await Promise.resolve();

    const geocodeCalls = mock.calls.filter((c) => c.path === "/api/geocode");
    expect(geocodeCalls).toHaveLength(2);
    expect(geocodeCalls[0].url).not.toContain("worldwide=1");
    expect(geocodeCalls[1].url).toContain("worldwide=1");
  });

  it("input event after blur preview adds the 'is-stale' class to Currently sublabel", async () => {
    buildDom({ mode: "specific" });
    mock.register(/^\/api\/geocode$/, {
      status: 200,
      body: { ok: true, short_name: "Mumbai, India" },
    });
    loadScript("settings.js");

    const query = document.getElementById("location_query");
    const current = document.querySelector("[data-current-location]");
    query.value = "Mumbai";
    query.dispatchEvent(new Event("blur"));
    await Promise.resolve();
    await Promise.resolve();

    expect(current.classList.contains("is-stale")).toBe(false);

    // User types more → Currently goes stale.
    query.value = "Mumbai123";
    query.dispatchEvent(new Event("input"));
    expect(current.classList.contains("is-stale")).toBe(true);
  });
});

// ── A13: Temperature pill auto-saves on click (no Save button) ──────────

describe("#337 A13 — Temperature pill auto-save", () => {
  async function flushAutoSave() {
    // CSRF + settings POST + .then chains. A few await Promise.resolve() ticks
    // is enough since installFetchMock returns already-resolved promises.
    for (let i = 0; i < 10; i++) await Promise.resolve();
  }

  it("clicking Celsius POSTs WEATHER_UNITS=metric to /api/settings", async () => {
    buildTempDom({ units: "imperial" });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const cOpt = document.querySelector('[data-units-opt="metric"]');
    const cRadio = cOpt.querySelector(".settings-segmented__input");
    cRadio.checked = true;
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    const settingsCalls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(settingsCalls).toHaveLength(1);
    const body = JSON.parse(settingsCalls[0].opts.body);
    expect(body.section).toBe("units");
    expect(body.WEATHER_UNITS).toBe("metric");
    expect(body.csrf_token).toBe("fresh-token");

    // Visual reflects the click.
    expect(cOpt.classList.contains("is-selected")).toBe(true);
    expect(document.querySelector('[data-units-opt="imperial"]').classList.contains("is-selected")).toBe(false);
  });

  it("server failure reverts the pill visual to the previous selection", async () => {
    buildTempDom({ units: "imperial" });
    mock.register(/^\/api\/settings$/, { status: 500, body: { ok: false } });
    // Suppress jsdom alert.
    const origAlert = globalThis.alert;
    globalThis.alert = () => {};
    loadScript("settings.js");

    const cOpt = document.querySelector('[data-units-opt="metric"]');
    const fOpt = document.querySelector('[data-units-opt="imperial"]');
    const cRadio = cOpt.querySelector(".settings-segmented__input");
    cRadio.checked = true;
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    // Reverted.
    expect(fOpt.classList.contains("is-selected")).toBe(true);
    expect(cOpt.classList.contains("is-selected")).toBe(false);
    globalThis.alert = origAlert;
  });

  // #415 /review (testing specialist): the autoSavePatch helper extracted in
  // #414 item #3 preserves a special path for 504 env_lock_timeout — when
  // the server returns 504 with {error:{code:'env_lock_timeout', message:...}}
  // the helper extracts that server message into err.timeoutMsg, and the
  // call site surfaces it via alert() instead of the generic "Couldn't save"
  // fallback. Without coverage, a future refactor that drops the 504
  // envelope-parsing block silently breaks BOTH call sites (Weather toggle +
  // Temperature pill share the helper).
  it("504 env_lock_timeout surfaces the server message via timeoutMsg (not the generic alert)", async () => {
    buildTempDom({ units: "imperial" });
    const serverMsg =
      "Settings file is busy — another update (weekly auto-update, Reset WiFi, or Prepare-for-Gifting) is in progress. Try Save again in a few seconds.";
    mock.register(/^\/api\/settings$/, {
      status: 504,
      body: { ok: false, error: { code: "env_lock_timeout", message: serverMsg } },
    });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const cOpt = document.querySelector('[data-units-opt="metric"]');
    const fOpt = document.querySelector('[data-units-opt="imperial"]');
    const cRadio = cOpt.querySelector(".settings-segmented__input");
    cRadio.checked = true;
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    // The actionable server message lands in the alert, not the generic copy.
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toBe(serverMsg);
    // Visual still reverts on failure (this is the helper's caller-side revert,
    // unchanged by the 504 path — confirms the failure flow still runs).
    expect(fOpt.classList.contains("is-selected")).toBe(true);
    expect(cOpt.classList.contains("is-selected")).toBe(false);
    globalThis.alert = origAlert;
  });

  // #457/#458: the Temperature pill is wired via the serialized
  // `wireSegmentedAutoSave` helper. A rapid metric→imperial flip during an
  // in-flight save must not open a concurrent second request; the second
  // save fires only after the first settles, so the persisted units converge
  // to the value the user landed on (imperial).
  it("a rapid metric→imperial flip serializes: second save fires only after the first settles", async () => {
    function okResponse() {
      return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => "{}" };
    }
    function deferred() {
      let resolve;
      const promise = new Promise((res) => {
        resolve = res;
      });
      return { promise, resolve };
    }
    buildTempDom({ units: "imperial" });
    const d1 = deferred();
    let call = 0;
    mock.register(/^\/api\/settings$/, () => {
      call += 1;
      return call === 1 ? d1.promise : okResponse();
    });
    loadScript("settings.js");

    const fOpt = document.querySelector('[data-units-opt="imperial"]');
    const cOpt = document.querySelector('[data-units-opt="metric"]');
    const fRadio = fOpt.querySelector(".settings-segmented__input");
    const cRadio = cOpt.querySelector(".settings-segmented__input");

    // Click Celsius → save #1 (metric), held open.
    cRadio.checked = true;
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));
    // Flip back to Fahrenheit → coalesced, NOT sent yet.
    fRadio.checked = true;
    fRadio.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    const before = mock.calls.filter((c) => c.path === "/api/settings");
    expect(before).toHaveLength(1);
    expect(JSON.parse(before[0].opts.body).WEATHER_UNITS).toBe("metric");

    // Let save #1 land; the coalesced Fahrenheit fires as save #2.
    d1.resolve(okResponse());
    await flushAutoSave();

    const after = mock.calls.filter((c) => c.path === "/api/settings");
    expect(after).toHaveLength(2);
    expect(JSON.parse(after[1].opts.body).WEATHER_UNITS).toBe("imperial");
    // Final visual reflects the value the user landed on.
    expect(fOpt.classList.contains("is-selected")).toBe(true);
    expect(cOpt.classList.contains("is-selected")).toBe(false);
  });

  // #457: the load-bearing "revert to the last CONFIRMED value, not the failed
  // target" branch for the SEGMENTED helper. The boolean toggle can't pin this
  // (for a 2-state control confirmed and !target coincide), so the segmented
  // pill is the authoritative guard: save #1 (metric) succeeds → confirmedOpt
  // moves to metric; the coalesced save #2 (imperial) FAILS → the pill must
  // revert to metric (what's actually on disk), NOT imperial (the failed target).
  it("when a coalesced follow-up save fails, the pill reverts to the confirmed opt, not the failed target", async () => {
    function okResponse() {
      return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => "{}" };
    }
    function errResponse(status) {
      return { ok: false, status, json: async () => ({ ok: false }), text: async () => "{}" };
    }
    function deferred() {
      let resolve;
      const promise = new Promise((res) => {
        resolve = res;
      });
      return { promise, resolve };
    }
    buildTempDom({ units: "imperial" });
    const d1 = deferred();
    let call = 0;
    mock.register(/^\/api\/settings$/, () => {
      call += 1;
      return call === 1 ? d1.promise : errResponse(500);
    });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const fOpt = document.querySelector('[data-units-opt="imperial"]');
    const cOpt = document.querySelector('[data-units-opt="metric"]');
    const fRadio = fOpt.querySelector(".settings-segmented__input");
    const cRadio = cOpt.querySelector(".settings-segmented__input");

    cRadio.checked = true; // Celsius → save #1 (metric), held open
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));
    fRadio.checked = true; // back to Fahrenheit → coalesced
    fRadio.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    d1.resolve(okResponse()); // save #1 confirms metric; save #2 (imperial) then fails
    await flushAutoSave();

    expect(mock.calls.filter((c) => c.path === "/api/settings").length).toBe(2);
    // Reverted to metric (confirmed / on disk), NOT imperial (the failed target).
    expect(cOpt.classList.contains("is-selected")).toBe(true);
    expect(fOpt.classList.contains("is-selected")).toBe(false);
    expect(cRadio.checked).toBe(true);
    expect(alerts).toEqual(["Couldn't save temperature units. Try again."]);
    globalThis.alert = origAlert;
  });

  // #457: SSR-seed edge — a radiogroup with NO `is-selected` opt (server
  // rendered no default). confirmedOpt seeds null; the first click must still
  // fire exactly one save carrying that opt's value (desiredOpt !== null).
  it("with no opt pre-selected, the first click still saves the chosen value", async () => {
    buildTempDom({ units: "imperial" });
    // Strip the server-rendered selection so confirmedOpt seeds null.
    document.querySelectorAll(".settings-segmented__opt").forEach((o) => {
      o.classList.remove("is-selected");
    });
    document.querySelectorAll(".settings-segmented__input").forEach((r) => {
      r.checked = false;
    });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const cRadio = document.querySelector('[data-units-opt="metric"] .settings-segmented__input');
    cRadio.checked = true;
    cRadio.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    const calls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0].opts.body).WEATHER_UNITS).toBe("metric");
  });
});

// ── A18: browser-tz fallback button ─────────────────────────────────────

describe("#337 A18 — browser-tz fallback", () => {
  it("populates the label with the detected tz + unhides the row", () => {
    buildTzDom();
    // jsdom returns an actual Intl object; verify the row becomes visible.
    loadScript("settings.js");

    const row = document.querySelector("[data-tz-fallback]");
    const label = document.querySelector("[data-browser-tz-label]");
    expect(row.hidden).toBe(false);
    // jsdom resolved tz varies by host, but the label should NOT be the default em-dash.
    expect(label.textContent).not.toBe("—");
    expect(label.textContent.length).toBeGreaterThan(0);
  });

  it("button click POSTs detected tz to /api/handoff/set-timezone", async () => {
    buildTzDom();
    // Skip the reload-mock — jsdom marks window.location.reload as
    // non-configurable, and the production reload happens AFTER the POST
    // succeeds. What matters is that the POST lands with the detected
    // tz; the reload is an unconditional side-effect we don't need to
    // simulate to verify the behavior contract.
    // #337 /review — endpoint changed from /api/handoff/set-timezone (which
    // no-ops post-handoff) to the new always-on /api/system/set-timezone.
    mock.register(/^\/api\/system\/set-timezone$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const btn = document.querySelector("[data-browser-tz-btn]");
    btn.click();

    // Wait for the CSRF + endpoint POST chain to settle.
    for (let i = 0; i < 10; i++) await Promise.resolve();

    const tzCalls = mock.calls.filter((c) => c.path === "/api/system/set-timezone");
    expect(tzCalls).toHaveLength(1);
    // Regression guard: must NOT call the gated handoff endpoint.
    expect(mock.calls.filter((c) => c.path === "/api/handoff/set-timezone")).toHaveLength(0);
    const body = JSON.parse(tzCalls[0].opts.body);
    expect(typeof body.timezone).toBe("string");
    expect(body.timezone.length).toBeGreaterThan(0);
    expect(body.csrf_token).toBe("fresh-token");
  });
});

// ── Always-on: has-js class added so [data-no-js-only] hides ────────────

describe("#337 has-js class for no-JS-only CSS hide", () => {
  it("settings.js adds 'has-js' to <html> on load", () => {
    buildDom();
    expect(document.documentElement.classList.contains("has-js")).toBe(false);
    loadScript("settings.js");
    expect(document.documentElement.classList.contains("has-js")).toBe(true);
  });
});
