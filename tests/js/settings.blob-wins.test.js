// litclock-dev#532 slice 7 — "blob wins" for settings.js (the status/
// diagnostics copies have this pin; settings lacked it, which let the
// hardcoded disabled-Save tooltip ship rewriting English over a translated
// page — Codex slice-7 /review). A translated blob value must reach both
// the failure alert and the tooltip rewrite; fallbacks apply only blob-less.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

const BLOB = {
  "settings.js.save_failed.units": "TRANSLATED-UNITS-FAIL",
  "settings.location.save_disabled_tooltip": "TRANSLATED-TOOLTIP",
};

function buildDom() {
  document.body.innerHTML = `
    <script type="application/json" data-litclock-strings>${JSON.stringify(BLOB)}</script>
    <form class="settings-form" data-section="location">
      <input type="hidden" name="csrf_token" value="render-token">
      <div class="settings-segmented" role="radiogroup" data-mode-pill>
        <label class="settings-segmented__opt is-selected" data-mode-opt="auto">
          <input type="radio" class="settings-segmented__input" name="WEATHER_LOCATION_MODE" value="auto" checked>
        </label>
        <label class="settings-segmented__opt" data-mode-opt="specific">
          <input type="radio" class="settings-segmented__input" name="WEATHER_LOCATION_MODE" value="specific">
        </label>
      </div>
      <div data-mode-panel="auto"></div>
      <fieldset data-mode-panel="specific" hidden disabled>
        <input type="text" id="location_query" name="location_query" value="">
        <span data-current-location>—</span>
        <details data-advanced>
          <input type="text" id="weather_latitude" data-advanced-lat value="">
          <input type="text" id="weather_longitude" data-advanced-lon value="">
        </details>
      </fieldset>
      <button type="submit" data-location-save>Save</button>
    </form>
    <form class="settings-form" data-section="units">
      <input type="hidden" name="csrf_token" value="render-token">
      <fieldset class="settings-row settings-row--pill">
        <div class="settings-segmented" role="radiogroup" data-temp-pill>
          <label class="settings-segmented__opt is-selected" data-units-opt="imperial">
            <input type="radio" class="settings-segmented__input" name="WEATHER_UNITS" value="imperial" checked>
          </label>
          <label class="settings-segmented__opt" data-units-opt="metric">
            <input type="radio" class="settings-segmented__input" name="WEATHER_UNITS" value="metric">
          </label>
        </div>
      </fieldset>
    </form>
  `;
}

let mock;
let alertSpy;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh" } });
  mock.register(/^\/api\/status$/, { status: 200, body: { ok: true, update_state: null } });
  alertSpy = vi.fn();
  window.alert = alertSpy;
});

afterEach(() => {
  mock.restore();
  document.body.innerHTML = "";
  document.documentElement.classList.remove("has-js");
});

describe("settings.js blob-wins (litclock-dev#532 slice 7)", () => {
  it("a failed auto-save alerts the BLOB value, not the English fallback", async () => {
    buildDom();
    mock.register(/^\/api\/settings$/, { status: 422, body: { ok: false, error: {} } });
    loadScript("settings.js");

    const metric = document.querySelector('[data-units-opt="metric"] .settings-segmented__input');
    metric.checked = true;
    metric.dispatchEvent(new Event("change", { bubbles: true }));
    for (let i = 0; i < 10; i++) await Promise.resolve();

    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(alertSpy.mock.calls[0][0]).toBe("TRANSLATED-UNITS-FAIL");
  });

  it("the disabled-Save tooltip rewrite uses the BLOB value", () => {
    buildDom();
    loadScript("settings.js");

    // Switch to Specific with an empty Place — JS disables Save + sets title.
    const specific = document.querySelector('[data-mode-opt="specific"] .settings-segmented__input');
    specific.checked = true;
    specific.dispatchEvent(new Event("change", { bubbles: true }));

    const btn = document.querySelector("[data-location-save]");
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("title")).toBe("TRANSLATED-TOOLTIP");
  });
});
