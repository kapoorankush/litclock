// litclock-dev#627 — Factory reset powers OFF instead of rebooting. The
// terminal handoff card must say so (not "reboot"), and it must NOT poll
// /api/health — the box is gone until a physical power-on. Guards against a
// stale-copy partial revert (the most user-visible part of the change).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

describe("system.js factory reset → power off (litclock-dev#627)", () => {
  let restoreFetch;
  let restoreDialog;

  beforeEach(() => {
    document.body.innerHTML = `
      <main>
        <form data-confirm-action="factory_reset" action="/api/system/reset">
          <input type="hidden" name="token" value="tok" />
          <button type="submit">Factory reset</button>
        </form>
      </main>
      <dialog class="confirm-sheet" data-action="factory_reset">
        <button data-modal-confirm>Factory reset</button>
        <button data-modal-cancel>Cancel</button>
      </dialog>
    `;
    restoreDialog = stubDialog();
    window.requestAnimationFrame = (cb) => cb();
  });

  afterEach(() => {
    if (restoreFetch) restoreFetch();
    if (restoreDialog) restoreDialog();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  it("renders a power-off terminal card, never says reboot, never polls health", async () => {
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/reset/, { status: 200, body: { ok: true } });

    loadScript("system.js");
    document.querySelector('dialog[data-action="factory_reset"] [data-modal-confirm]').click();
    await new Promise((r) => setTimeout(r, 0));

    const html = document.querySelector("main").innerHTML;
    expect(html).toMatch(/powering off/i);
    expect(html).not.toMatch(/reboot/i);
    expect(html).not.toMatch(/restarting/i);
    // Terminal — no reconnect poll against a box that is powering off for good.
    expect(mock.calls.some((c) => c.path === "/api/health")).toBe(false);
  });
});
