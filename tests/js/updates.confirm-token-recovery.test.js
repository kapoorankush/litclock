// litclock-dev#597 — update_apply is the sixth confirm-token-gated destructive
// action, driven by updates.js (not system.js). Its stale/unrecognised
// confirmation must offer the same reload-to-recover path the System tab
// actions do, instead of a dead-end alert.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

function buildDom() {
  // Mirrors the update-card hooks updates.js queries (see updates.html.j2).
  document.body.innerHTML = `
    <section id="updates-card" data-state="available" data-current-version="0.211.4">
      <form action="/api/update/apply" data-confirm-action="update_apply">
        <input type="hidden" name="token" value="stale-token" />
        <button type="submit">Apply</button>
      </form>
    </section>
    <dialog class="confirm-sheet" data-action="update_apply">
      <button type="button" data-modal-cancel>Cancel</button>
      <button type="button" data-modal-confirm>Confirm</button>
    </dialog>
    <ol id="phase-reading-list" hidden></ol>
    <p id="phase-terminal-message" hidden></p>
  `;
}

async function flushMicrotasks() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe("updates.js confirm-token recovery (litclock-dev#597)", () => {
  let mock;
  let reloadSpy;
  let restoreDialog = () => {};

  beforeEach(() => {
    buildDom();
    restoreDialog = stubDialog();
    mock = installFetchMock();
    // Cold-load probes fired by updates.js on load — keep them benign so they
    // don't reach the real (missing) fetch. 'available' matches the card state
    // so refreshCheck does not force a reload.
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/update\/status$/, () => {
      throw new TypeError("network blip");
    });

    reloadSpy = vi.fn();
    const originalLocation = window.location;
    delete window.location;
    window.location = { ...originalLocation, reload: reloadSpy };
  });

  afterEach(() => {
    mock.restore();
    restoreDialog();
    restoreDialog = () => {};
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  it("offers reload-to-recover on confirm_token_invalid instead of a dead-end alert", async () => {
    mock.register(/\/api\/update\/apply$/, {
      status: 401,
      body: { error: { code: "confirm_token_invalid", message: "Couldn't verify that action. Reload the page and try again." } },
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    loadScript("updates.js");
    await flushMicrotasks();
    document.querySelector('dialog[data-action="update_apply"] [data-modal-confirm]').click();
    await flushMicrotasks();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toMatch(/reload/i);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it("does not reload when the user cancels the recovery prompt", async () => {
    mock.register(/\/api\/update\/apply$/, {
      status: 401,
      body: { error: { code: "confirm_token_expired", message: "This confirmation timed out for safety. Reload the page and try again." } },
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    loadScript("updates.js");
    await flushMicrotasks();
    document.querySelector('dialog[data-action="update_apply"] [data-modal-confirm]').click();
    await flushMicrotasks();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(reloadSpy).not.toHaveBeenCalled();
  });
});
