// Tests for the Weather "Show on display" toggle auto-save (settings.js).
//
// #346 introduced this toggle with a hand-rolled AbortController auto-save
// handler. #458 converged it onto the shared `wireBooleanToggleAutoSave`
// helper (same path as the Advanced toggles), and #457 changed that helper
// to serialize saves instead of aborting. The toggle had NO dedicated JS
// coverage before; this suite pins the convergence: the Weather toggle must
// still PATCH section="weather"/WEATHER_ENABLED, surface its own failure
// copy + the 504 lock-timeout message, and revert on failure.
//
// The serialized-coalescing behavior itself is exercised in
// settings.advanced-toggle.test.js (same helper); here we just confirm the
// Weather toggle is wired through it with the right section/key/message.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

// Mirror the Weather section markup (settings.html.j2, post-#337-A9 — the
// section is reduced to the visibility toggle; city/zip moved to Location).
function buildWeatherDom({ enabled = false } = {}) {
  const chk = enabled ? "checked" : "";
  document.body.innerHTML = `
    <form class="settings-form" data-section="weather">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="weather">
      <div class="settings-row settings-row--toggle">
        <label class="settings-row__label" for="weather_enabled">Show on display</label>
        <input class="settings-toggle" type="checkbox" id="weather_enabled"
               name="WEATHER_ENABLED" role="switch"
               aria-checked="${enabled ? "true" : "false"}" ${chk}>
      </div>
      <div class="settings-form__actions" data-no-js-only>
        <button type="submit" class="settings-button settings-button--primary">Save</button>
      </div>
    </form>
  `;
}

async function flushAutoSave() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

let mock;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh-token" } });
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

describe("Weather toggle auto-save (#458 convergence onto wireBooleanToggleAutoSave)", () => {
  it("toggling on POSTs section=weather + WEATHER_ENABLED=true to /api/settings", async () => {
    buildWeatherDom({ enabled: false });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const toggle = document.getElementById("weather_enabled");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    const calls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(calls).toHaveLength(1);
    const body = JSON.parse(calls[0].opts.body);
    expect(body.section).toBe("weather");
    expect(body.WEATHER_ENABLED).toBe(true);
    expect(body.csrf_token).toBe("fresh-token");

    expect(toggle.checked).toBe(true);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("server failure reverts the toggle (checked + aria) and surfaces the weather-specific message", async () => {
    buildWeatherDom({ enabled: false });
    mock.register(/^\/api\/settings$/, { status: 500, body: { ok: false } });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const toggle = document.getElementById("weather_enabled");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    expect(toggle.checked).toBe(false);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(alerts).toEqual(["Could not save the weather toggle. Try again."]);
    globalThis.alert = origAlert;
  });

  it("504 env_lock_timeout surfaces the server message (not the generic copy) and reverts", async () => {
    buildWeatherDom({ enabled: false });
    const serverMsg =
      "Settings file is busy — another update is in progress. Try Save again in a few seconds.";
    mock.register(/^\/api\/settings$/, {
      status: 504,
      body: { ok: false, error: { code: "env_lock_timeout", message: serverMsg } },
    });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const toggle = document.getElementById("weather_enabled");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    expect(alerts).toEqual([serverMsg]);
    expect(toggle.checked).toBe(false);
    globalThis.alert = origAlert;
  });
});
