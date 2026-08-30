// Tests for the Settings Language pill (litclock-dev#532 pickers 5b).
//
// The pill reuses wireSegmentedAutoSave (the litclock-dev#337 A13 Temperature path) with
// section="language" / LITCLOCK_LANGUAGE, plus the one behavior unique to
// language: a full page reload once the save COMMITS (so catalog-routed SSR
// copy re-renders in the saved language). The reload must fire only after a
// success with no pending re-tap, and never on failure (failure reverts the
// pill and alerts, same as Temperature).
//
// The pill is dormant (no DOM at all) on a single-language fleet — the
// wiring's null-guard path is pinned here too so settings.js never throws on
// today's English-only page.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

// Mirror the Language section markup (settings.html.j2, 5b) with two active
// languages, English currently selected.
function buildLanguageDom({ current = "en" } = {}) {
  const sel = (code) => (current === code ? "is-selected" : "");
  const chk = (code) => (current === code ? "checked" : "");
  document.body.innerHTML = `
    <form class="settings-form" data-section="language">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="language">
      <fieldset class="settings-row settings-row--pill">
        <div class="settings-segmented" role="radiogroup" aria-label="Device language" data-language-pill>
          <label class="settings-segmented__opt ${sel("en")}" data-language-opt="en">
            <input type="radio" class="settings-segmented__input" name="LITCLOCK_LANGUAGE" value="en" ${chk("en")}>
            <span>English</span>
          </label>
          <label class="settings-segmented__opt ${sel("es")}" data-language-opt="es">
            <input type="radio" class="settings-segmented__input" name="LITCLOCK_LANGUAGE" value="es" ${chk("es")}>
            <span>Español</span>
          </label>
        </div>
      </fieldset>
      <div class="settings-form__actions" data-no-js-only>
        <button type="submit" class="settings-button settings-button--primary">Save</button>
      </div>
    </form>
  `;
}

async function flushAutoSave() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function clickOpt(code) {
  const radio = document.querySelector(`[data-language-opt="${code}"] .settings-segmented__input`);
  radio.checked = true;
  radio.dispatchEvent(new Event("change", { bubbles: true }));
  return radio;
}

let mock;
let reloadSpy;
let alertSpy;

// Capture the REAL jsdom location/alert once at module scope — capturing
// inside beforeEach would grab the previous test's stub from the second
// test on (testing-specialist /review), and never restoring would leak the
// stubs into any future test in this file that touches location.href.
const realLocation = window.location;
const realAlert = window.alert;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh-token" } });
  mock.register(/^\/api\/status$/, {
    status: 200,
    body: { ok: true, update_state: null, update_phase_index: null },
  });

  // Stub location.reload — jsdom's throws "Not implemented".
  reloadSpy = vi.fn();
  delete window.location;
  window.location = { ...realLocation, reload: reloadSpy };

  alertSpy = vi.fn();
  window.alert = alertSpy;
});

afterEach(() => {
  mock.restore();
  document.body.innerHTML = "";
  document.documentElement.classList.remove("has-js");
  delete window.location;
  window.location = realLocation;
  window.alert = realAlert;
});

describe("Language pill auto-save (litclock-dev#532 pickers 5b)", () => {
  it("selecting a language PATCHes section=language + LITCLOCK_LANGUAGE, then reloads", async () => {
    buildLanguageDom({ current: "en" });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    clickOpt("es");
    await flushAutoSave();

    const calls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(calls).toHaveLength(1);
    const body = JSON.parse(calls[0].opts.body);
    expect(body.section).toBe("language");
    expect(body.LITCLOCK_LANGUAGE).toBe("es");

    // Reload fires exactly once, AFTER the save committed.
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(alertSpy).not.toHaveBeenCalled();
    expect(document.querySelector('[data-language-opt="es"]').classList.contains("is-selected")).toBe(true);
  });

  it("failure reverts the pill, alerts, and does NOT reload", async () => {
    buildLanguageDom({ current: "en" });
    mock.register(/^\/api\/settings$/, {
      status: 422,
      body: { ok: false, error: { code: "validation", message: "nope" } },
    });
    loadScript("settings.js");

    clickOpt("es");
    await flushAutoSave();

    expect(reloadSpy).not.toHaveBeenCalled();
    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(alertSpy.mock.calls[0][0]).toMatch(/language/i);
    // Reverted to the server-confirmed selection.
    expect(document.querySelector('[data-language-opt="en"]').classList.contains("is-selected")).toBe(true);
    expect(document.querySelector('[data-language-opt="en"] .settings-segmented__input').checked).toBe(true);
  });

  it("a re-tap mid-save is chased before the reload fires", async () => {
    buildLanguageDom({ current: "en" });
    // Function handlers are passed straight through by installFetchMock (no
    // makeResponse wrap) — return Response-like objects directly.
    const okResponse = () => ({ ok: true, status: 200, json: async () => ({ ok: true }), text: async () => "{}" });
    let resolveFirst;
    let settled = 0;
    mock.register(/^\/api\/settings$/, () => {
      settled += 1;
      if (settled === 1) {
        return new Promise((resolve) => {
          resolveFirst = () => resolve(okResponse());
        });
      }
      return okResponse();
    });
    loadScript("settings.js");

    clickOpt("es");
    await flushAutoSave();
    // First save in flight; user re-taps back to English.
    clickOpt("en");
    await flushAutoSave();
    expect(reloadSpy).not.toHaveBeenCalled(); // nothing committed yet

    resolveFirst();
    await flushAutoSave();

    const calls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(calls).toHaveLength(2);
    expect(JSON.parse(calls[1].opts.body).LITCLOCK_LANGUAGE).toBe("en");
    // Reload only once the FINAL state committed.
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("chased save failure: no reload, alert, revert to first-committed option", async () => {
    // First save (es) succeeds; the chased re-tap (en) fails. onSaved must
    // NOT fire — es stays persisted server-side, the pill reverts to es,
    // the alert is the only signal (pins the new pump() branch composition).
    buildLanguageDom({ current: "en" });
    // Hold the first save in flight so the re-tap lands MID-save — flushing
    // between clicks would let the first commit legitimately reload.
    const okResponse = () => ({ ok: true, status: 200, json: async () => ({ ok: true }), text: async () => "{}" });
    const failResponse = () => ({ ok: false, status: 422, json: async () => ({ ok: false }), text: async () => "{}" });
    let resolveFirst;
    let n = 0;
    mock.register(/^\/api\/settings$/, () => {
      n += 1;
      if (n === 1) {
        return new Promise((resolve) => {
          resolveFirst = () => resolve(okResponse());
        });
      }
      return failResponse();
    });
    loadScript("settings.js");

    clickOpt("es");
    await flushAutoSave(); // save #1 (es) now in flight
    clickOpt("en"); // re-tap lands mid-save
    await flushAutoSave();
    resolveFirst(); // es commits; the chase (en) then fails
    await flushAutoSave();

    expect(reloadSpy).not.toHaveBeenCalled();
    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-language-opt="es"]').classList.contains("is-selected")).toBe(true);
  });

  it("pill radios are disabled before the reload fires (mid-unload tap guard)", async () => {
    buildLanguageDom({ current: "en" });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    clickOpt("es");
    await flushAutoSave();

    expect(reloadSpy).toHaveBeenCalledTimes(1);
    document.querySelectorAll("[data-language-pill] .settings-segmented__input").forEach((r) => {
      expect(r.disabled).toBe(true);
    });
  });

  it("settings.js loads cleanly with no language pill in the DOM (dormant fleet)", () => {
    document.body.innerHTML = `
      <form class="settings-form" data-section="units">
        <input type="hidden" name="csrf_token" value="render-token">
      </form>
    `;
    expect(() => loadScript("settings.js")).not.toThrow();
  });
});
