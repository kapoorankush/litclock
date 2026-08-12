// litclock-dev#597 — a stale/unrecognised confirm token must offer the user a
// reload-to-recover path (window.confirm → location.reload), not a dead-end
// alert. Server-side reclassifies most sat-on tokens as `expired`, but a
// genuinely unknown token still lands on `confirm_token_invalid`; both must be
// recoverable. This is the first vitest coverage of system.js, so it also
// polyfills jsdom's missing <dialog> API to let the confirm flow wire up.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

describe("system.js confirm-token recovery (litclock-dev#597)", () => {
  let reloadSpy;
  let restoreFetch;
  let restoreDialog;

  beforeEach(() => {
    document.body.innerHTML = `
      <form data-confirm-action="factory_reset" action="/api/system/reset">
        <input type="hidden" name="token" value="stale-token" />
        <button type="submit">Factory reset</button>
      </form>
      <dialog class="confirm-sheet" data-action="factory_reset">
        <button data-modal-confirm>Reset</button>
        <button data-modal-cancel>Cancel</button>
      </dialog>
    `;
    // system.js early-returns unless <dialog>.showModal is a function.
    restoreDialog = stubDialog();

    // Stub location.reload — jsdom's throws "Not implemented".
    reloadSpy = vi.fn();
    const originalLocation = window.location;
    delete window.location;
    window.location = { ...originalLocation, reload: reloadSpy };

    // requestAnimationFrame is used by the sheet-open animation; jsdom has it,
    // but make it deterministic so loadScript doesn't leave timers dangling.
    window.requestAnimationFrame = (cb) => cb();
  });

  afterEach(() => {
    if (restoreFetch) restoreFetch();
    if (restoreDialog) restoreDialog();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  function fireConfirm() {
    document.querySelector('dialog[data-action="factory_reset"] [data-modal-confirm]').click();
  }

  it("offers reload-to-recover on confirm_token_invalid instead of a dead-end alert", async () => {
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/reset/, {
      status: 401,
      body: { error: { code: "confirm_token_invalid", message: "Couldn't verify that action. Reload the page and try again." } },
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    loadScript("system.js");
    fireConfirm();
    await new Promise((r) => setTimeout(r, 0));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toMatch(/reload/i);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    // The dead-end alert path must NOT fire for a recoverable token failure.
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it("prepare_for_gift + expired takes the auto-retry path, not the reload branch", async () => {
    // Regression guard for the branch ordering: the pre-existing auto-retry
    // (mint a fresh token, replay once) must win over the litclock-dev#597 reload branch
    // for prepare_for_gift, so a reload never discards the typed gift message.
    document.body.innerHTML = `
      <form data-confirm-action="prepare_for_gift" action="/api/system/prepare-for-gift">
        <input type="hidden" name="token" value="stale-token" />
        <textarea name="message" hidden>Happy birthday</textarea>
        <button type="submit">Prepare</button>
      </form>
      <dialog class="confirm-sheet" data-action="prepare_for_gift">
        <button data-modal-confirm>Prepare</button>
      </dialog>
    `;
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/prepare-for-gift/, {
      status: 401,
      body: { error: { code: "confirm_token_expired", message: "This confirmation timed out for safety. Reload the page and try again." } },
    });
    // Make the token refresh fail so the auto-retry falls to its alert fallback
    // — we only need to prove the refresh endpoint was reached (auto-retry
    // fired) and the reload branch did NOT preempt it.
    mock.register(/\/api\/system\/confirm-token/, { status: 500, body: {} });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "alert").mockImplementation(() => {});

    loadScript("system.js");
    document.querySelector('dialog[data-action="prepare_for_gift"] [data-modal-confirm]').click();
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    const hitRefresh = mock.calls.some((c) => c.path === "/api/system/confirm-token");
    expect(hitRefresh).toBe(true); // auto-retry fired
    expect(confirmSpy).not.toHaveBeenCalled(); // reload branch did NOT preempt
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("does not reload when the user cancels the recovery prompt", async () => {
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/reset/, {
      status: 401,
      body: { error: { code: "confirm_token_expired", message: "This confirmation timed out for safety. Reload the page and try again." } },
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    loadScript("system.js");
    fireConfirm();
    await new Promise((r) => setTimeout(r, 0));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(reloadSpy).not.toHaveBeenCalled();
  });
});
