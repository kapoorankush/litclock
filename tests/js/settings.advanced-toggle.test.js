// Tests for the Advanced section toggle auto-save (settings.js).
//
// DESIGN.md "Save-button rule" (#337 A13) requires discrete controls (toggles,
// segmented pills) to auto-save on change with no Save button. The Advanced
// section's two toggles (ALLOW_NSFW_QUOTES, SHOW_DIAGNOSTICS_SHORTCUT) were the
// last holdout still rendering a Save button; this suite pins their conformance
// to the same autoSavePatch pattern as the Weather toggle + Temperature pill.
//
// Pattern mirrors the #337 A13 Temperature-pill tests in
// settings.location-pill.test.js: DOM stubbed to the post-fix template shape,
// fetch mocked, loadScript runs the IIFE, change events drive the handler.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

// Mirror the Advanced section markup (settings.html.j2). Only the elements
// settings.js queries are included. `data-no-js-only` on the actions row is
// the no-JS Save fallback that CSS hides under html.has-js.
function buildAdvancedDom({ nsfw = false, diag = false } = {}) {
  const nsfwChk = nsfw ? "checked" : "";
  const diagChk = diag ? "checked" : "";
  document.body.innerHTML = `
    <form class="settings-form" data-section="advanced">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="advanced">
      <div class="settings-row settings-row--toggle">
        <label class="settings-row__label" for="allow_nsfw_quotes">Allow NSFW quotes</label>
        <input class="settings-toggle" type="checkbox" id="allow_nsfw_quotes"
               name="ALLOW_NSFW_QUOTES" role="switch"
               aria-checked="${nsfw ? "true" : "false"}" ${nsfwChk}>
      </div>
      <div class="settings-row settings-row--toggle">
        <label class="settings-row__label" for="show_diagnostics_shortcut">Show diagnostics shortcut</label>
        <input class="settings-toggle" type="checkbox" id="show_diagnostics_shortcut"
               name="SHOW_DIAGNOSTICS_SHORTCUT" role="switch"
               aria-checked="${diag ? "true" : "false"}" ${diagChk}>
      </div>
      <div class="settings-form__actions" data-no-js-only>
        <button type="submit" class="settings-button settings-button--primary">Save</button>
      </div>
    </form>
  `;
}

// CSRF + settings POST + .then chains resolve over a few microtask ticks since
// installFetchMock returns already-resolved promises (same helper as A13).
async function flushAutoSave() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

let mock;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh-token" } });
  // Always-on update-banner poller in settings.js hits /api/status.
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

describe("Advanced toggle auto-save (DESIGN.md Save-button rule)", () => {
  it("toggling NSFW on POSTs only ALLOW_NSFW_QUOTES=true to /api/settings", async () => {
    buildAdvancedDom({ nsfw: false, diag: false });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const toggle = document.getElementById("allow_nsfw_quotes");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    const settingsCalls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(settingsCalls).toHaveLength(1);
    const body = JSON.parse(settingsCalls[0].opts.body);
    expect(body.section).toBe("advanced");
    expect(body.ALLOW_NSFW_QUOTES).toBe(true);
    expect(body.csrf_token).toBe("fresh-token");
    // Sibling toggle MUST NOT be in the payload — JSON PATCH writes only the
    // key sent, so the diagnostics-shortcut toggle is never touched.
    expect("SHOW_DIAGNOSTICS_SHORTCUT" in body).toBe(false);

    // State persists; aria stays in sync.
    expect(toggle.checked).toBe(true);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("the diagnostics-shortcut toggle saves only its own field", async () => {
    buildAdvancedDom({ nsfw: true, diag: false });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
    loadScript("settings.js");

    const toggle = document.getElementById("show_diagnostics_shortcut");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    const settingsCalls = mock.calls.filter((c) => c.path === "/api/settings");
    expect(settingsCalls).toHaveLength(1);
    const body = JSON.parse(settingsCalls[0].opts.body);
    expect(body.section).toBe("advanced");
    expect(body.SHOW_DIAGNOSTICS_SHORTCUT).toBe(true);
    expect("ALLOW_NSFW_QUOTES" in body).toBe(false);
  });

  it("server failure reverts the toggle to its prior state (checked + aria) + uses the toggle-specific message", async () => {
    buildAdvancedDom({ nsfw: false });
    mock.register(/^\/api\/settings$/, { status: 500, body: { ok: false } });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const toggle = document.getElementById("allow_nsfw_quotes");
    // User flips it ON; the optimistic visual is ON until the save fails.
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    // Reverted to the origin state prior to the toggle.
    expect(toggle.checked).toBe(false);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    // The NSFW toggle's OWN failMsg surfaces — not the diagnostics one. Guards
    // against a copy-paste swap of the two per-toggle messages (/review).
    expect(alerts).toEqual(["Couldn't save the NSFW-quotes setting. Try again."]);
    globalThis.alert = origAlert;
  });

  it("the diagnostics-shortcut toggle surfaces ITS own failMsg on failure", async () => {
    buildAdvancedDom({ diag: false });
    mock.register(/^\/api\/settings$/, { status: 500, body: { ok: false } });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const toggle = document.getElementById("show_diagnostics_shortcut");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    expect(alerts).toEqual(["Couldn't save the diagnostics-shortcut setting. Try again."]);
    globalThis.alert = origAlert;
  });

  // ─── #457: serialized, coalescing saves (replaces the old abort path) ──
  // The helper now keeps at most ONE save in flight per control and
  // coalesces rapid taps to the latest desired value, instead of aborting
  // the in-flight fetch. These pin the convergence guarantee the old
  // AbortController strategy couldn't make: an older save can never land
  // after a newer one, because a newer save is only sent AFTER the
  // in-flight one settles (never concurrently with it).

  // Response-like objects: a function-handler's return value is passed
  // straight through by installFetchMock (no makeResponse wrap), so success
  // paths must expose .ok/.status/.json directly.
  function okResponse() {
    return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => "{}" };
  }
  function errResponse(status) {
    return { ok: false, status, json: async () => ({ ok: false }), text: async () => "{}" };
  }
  // A deferred lets a test hold a /api/settings call open, then interleave
  // more taps before resolving it — deterministic in-flight overlap.
  function deferred() {
    let resolve;
    const promise = new Promise((res) => {
      resolve = res;
    });
    return { promise, resolve };
  }

  it("coalesces a rapid on→off→on burst into a single save carrying the final value", async () => {
    buildAdvancedDom({ nsfw: false });
    const d1 = deferred();
    mock.register(/^\/api\/settings$/, () => d1.promise);
    loadScript("settings.js");

    const toggle = document.getElementById("allow_nsfw_quotes");
    // ON starts save #1; OFF then ON land while #1 is in flight (coalesced).
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    toggle.checked = false;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    // Only ONE request despite three taps — the rest coalesced into `desired`.
    expect(mock.calls.filter((c) => c.path === "/api/settings").length).toBe(1);

    // Resolve save #1 (it carried the first tap's value: true). Final desired
    // (true) now equals the confirmed value, so no follow-up save fires.
    d1.resolve(okResponse());
    await flushAutoSave();

    expect(mock.calls.filter((c) => c.path === "/api/settings").length).toBe(1);
    expect(toggle.checked).toBe(true);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("on→off during an in-flight save sends the second save only AFTER the first settles (no concurrent overlap)", async () => {
    // The headline #457 fix: because the two saves never overlap, the env.sh
    // flock applies them in send order, so the persisted state converges to
    // the value the user landed on (OFF) — it can never end up opposite.
    buildAdvancedDom({ nsfw: false });
    const d1 = deferred();
    let call = 0;
    mock.register(/^\/api\/settings$/, () => {
      call += 1;
      return call === 1 ? d1.promise : okResponse();
    });
    loadScript("settings.js");

    const toggle = document.getElementById("allow_nsfw_quotes");
    toggle.checked = true; // ON → save #1 (true), held open
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    toggle.checked = false; // OFF → coalesced, NOT sent yet
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    // Still exactly one request — the OFF did not open a concurrent save.
    const before = mock.calls.filter((c) => c.path === "/api/settings");
    expect(before).toHaveLength(1);
    expect(JSON.parse(before[0].opts.body).ALLOW_NSFW_QUOTES).toBe(true);

    // Now let save #1 land; the coalesced OFF fires as save #2.
    d1.resolve(okResponse());
    await flushAutoSave();

    const after = mock.calls.filter((c) => c.path === "/api/settings");
    expect(after).toHaveLength(2);
    expect(JSON.parse(after[1].opts.body).ALLOW_NSFW_QUOTES).toBe(false);
    expect(toggle.checked).toBe(false);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("when a coalesced follow-up save fails, reverts to the last CONFIRMED value (honest, not the failed target)", async () => {
    // Persisted false. ON (save #1, true) succeeds → confirmed true. The
    // coalesced OFF fires as save #2 (false) and FAILS. Revert is to the
    // confirmed value (true) — which is what's actually on disk — not to the
    // failed target (false), keeping the UI honest.
    //
    // NOTE: for a 2-state toggle this test can't FULLY isolate "revert to
    // confirmed" from "revert to !target" (here confirmed=true and !false=true
    // coincide). The authoritative guard for the confirmed-vs-target branch is
    // the segmented-pill coalesced-failure test in settings.location-pill.test.js,
    // where the confirmed opt and the failed target are distinct elements.
    buildAdvancedDom({ nsfw: false });
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

    const toggle = document.getElementById("allow_nsfw_quotes");
    toggle.checked = true; // ON → save #1 (true), held open
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    toggle.checked = false; // OFF → coalesced
    toggle.dispatchEvent(new Event("change", { bubbles: true }));
    await flushAutoSave();

    d1.resolve(okResponse()); // save #1 confirms true; save #2 (false) then fails
    await flushAutoSave();

    expect(mock.calls.filter((c) => c.path === "/api/settings").length).toBe(2);
    // Reverted to confirmed=true (on disk), NOT the failed target=false.
    expect(toggle.checked).toBe(true);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(alerts).toEqual(["Couldn't save the NSFW-quotes setting. Try again."]);
    globalThis.alert = origAlert;
  });

  it("504 env_lock_timeout surfaces the server message (not the generic alert) and reverts", async () => {
    buildAdvancedDom({ nsfw: false });
    const serverMsg =
      "Settings file is busy — another update is in progress. Try again in a few seconds.";
    mock.register(/^\/api\/settings$/, {
      status: 504,
      body: { ok: false, error: { code: "env_lock_timeout", message: serverMsg } },
    });
    const alerts = [];
    const origAlert = globalThis.alert;
    globalThis.alert = (msg) => alerts.push(msg);
    loadScript("settings.js");

    const toggle = document.getElementById("allow_nsfw_quotes");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change", { bubbles: true }));

    await flushAutoSave();

    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toBe(serverMsg);
    expect(toggle.checked).toBe(false);
    globalThis.alert = origAlert;
  });

  it("marks <html> has-js so the [data-no-js-only] Save button is CSS-hidden", () => {
    buildAdvancedDom();
    loadScript("settings.js");
    expect(document.documentElement.classList.contains("has-js")).toBe(true);
    // The fallback Save row is still in the DOM for no-JS; CSS (not JS) hides it.
    expect(document.querySelector("[data-no-js-only]")).not.toBeNull();
  });
});
