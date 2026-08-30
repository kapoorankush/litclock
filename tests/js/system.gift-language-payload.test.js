// litclock-dev#532 pickers 5b — the gift card's recipient-language pill
// lives INSIDE the destructive prepare_for_gift form as visible radios.
// The JS confirm flow's field collector must ship the CHECKED radio's
// value in the JSON payload (it previously collected only hidden inputs +
// hidden textareas), and must NOT ship unchecked options or clobber the
// token. Uses the litclock-dev#597 harness shape (dialog stub + confirm flow).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

describe("system.js gift-language payload (litclock-dev#532 5b)", () => {
  let restoreFetch;
  let restoreDialog;

  beforeEach(() => {
    document.body.innerHTML = `
      <form data-confirm-action="prepare_for_gift" action="/api/system/prepare-for-gift">
        <input type="hidden" name="token" value="tok-1" />
        <textarea name="message" hidden data-gift-message-sync>Happy Birthday</textarea>
        <div class="settings-segmented" data-gift-language-pill>
          <label data-gift-language-opt="en">
            <input type="radio" class="settings-segmented__input" name="language" value="en" />
          </label>
          <label class="is-selected" data-gift-language-opt="es">
            <input type="radio" class="settings-segmented__input" name="language" value="es" checked />
          </label>
        </div>
        <button type="submit">Prepare for Gifting…</button>
      </form>
      <dialog class="confirm-sheet" data-action="prepare_for_gift">
        <button data-modal-confirm>Prepare</button>
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

  it("ships the CHECKED language radio in the JSON payload", async () => {
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/prepare-for-gift/, { status: 200, body: { ok: true } });
    loadScript("system.js");

    document.querySelector('form[data-confirm-action="prepare_for_gift"] button[type="submit"]').click();
    document.querySelector('dialog[data-action="prepare_for_gift"] [data-modal-confirm]').click();
    for (let i = 0; i < 10; i++) await Promise.resolve();

    const calls = mock.calls.filter((c) => c.path === "/api/system/prepare-for-gift");
    expect(calls).toHaveLength(1);
    const body = JSON.parse(calls[0].opts.body);
    expect(body.token).toBe("tok-1");
    expect(body.message).toBe("Happy Birthday");
    expect(body.language).toBe("es");
  });

  it("omits language when no radio exists (dormant fleet)", async () => {
    document.querySelector("[data-gift-language-pill]").remove();
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/prepare-for-gift/, { status: 200, body: { ok: true } });
    loadScript("system.js");

    document.querySelector('form[data-confirm-action="prepare_for_gift"] button[type="submit"]').click();
    document.querySelector('dialog[data-action="prepare_for_gift"] [data-modal-confirm]').click();
    for (let i = 0; i < 10; i++) await Promise.resolve();

    const body = JSON.parse(
      mock.calls.filter((c) => c.path === "/api/system/prepare-for-gift")[0].opts.body
    );
    expect(body.token).toBe("tok-1");
    expect("language" in body).toBe(false);
  });
});
