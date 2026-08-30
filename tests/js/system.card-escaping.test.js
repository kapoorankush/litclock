// litclock-dev#532 slice 5 — the reconnect cards build innerHTML from
// CATALOG values. The contract under test: every catalog string is escaped
// before insertion (a hostile/broken blob value renders as TEXT), and only
// the trusted <strong>{network}</strong> composition reintroduces markup.
// A regression that drops esc() on one card, substitutes {network} before
// escaping, or passes catalog HTML through would ship an XSS-shaped hole
// with all other suites green (Codex slice-5 /review).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

const HOSTILE = {
  "system.card.factory_reset.title": 'Factory <img src=x onerror="window.__pwned=1"> reset',
  "system.card.factory_reset.body": '<script>window.__pwned=2</script> erasing & powering off',
  "system.card.factory_reset.body2": 'blank {network} <b>bold?</b> "quoted"',
  "system.card.factory_reset.body3": "forget {network} & rejoin",
};

describe("system.js reconnect-card escaping contract (litclock-dev#532)", () => {
  let restoreFetch;
  let restoreDialog;

  beforeEach(() => {
    document.body.innerHTML = `
      <script type="application/json" data-litclock-strings>${
        // Mirror Jinja's tojson: escape < so the hostile </script> inside a
        // value can't terminate the blob element (the exact hazard tojson
        // exists to prevent — without this the blob truncates, JSON.parse
        // fails, and the test silently exercises the fallbacks instead).
        JSON.stringify(HOSTILE).replace(/</g, "\\u003c")
      }</script>
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
    delete window.__pwned;
    document.body.innerHTML = "";
  });

  it("catalog values render as TEXT; only the trusted strong markup survives", async () => {
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/reset/, { status: 200, body: { ok: true } });

    loadScript("system.js");
    document.querySelector('dialog[data-action="factory_reset"] [data-modal-confirm]').click();
    await new Promise((r) => setTimeout(r, 0));

    const main = document.querySelector("main");
    // Anti-vacuity: the hostile blob was actually READ (not the
    // fallbacks) — 'onerror' exists only in the injected title.
    expect(main.textContent).toContain("onerror");
    // No injected elements exist as DOM nodes.
    expect(main.querySelector("img")).toBeNull();
    expect(main.querySelector("script")).toBeNull();
    expect(main.querySelector("b")).toBeNull();
    expect(window.__pwned).toBeUndefined();
    // The hostile markup is visible as literal TEXT (escaped, not dropped).
    expect(main.textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect(main.textContent).toContain("<script>");
    expect(main.textContent).toContain("<b>bold?</b>");
    expect(main.textContent).toContain('"quoted"');
    expect(main.textContent).toContain("erasing & powering off");
    // The trusted composition still renders real markup with the slot filled.
    const strongs = Array.from(main.querySelectorAll("strong")).map((el) => el.textContent);
    expect(strongs).toContain("LitClock-Setup");
    // And the slot was substituted AFTER escaping — no stray {network} text.
    expect(main.textContent).not.toContain("{network}");
  });

  it("without a blob, the English fallbacks render with the same contract", async () => {
    document.querySelector("[data-litclock-strings]").remove();
    const mock = installFetchMock();
    restoreFetch = mock.restore;
    mock.register(/\/api\/system\/reset/, { status: 200, body: { ok: true } });

    loadScript("system.js");
    document.querySelector('dialog[data-action="factory_reset"] [data-modal-confirm]').click();
    await new Promise((r) => setTimeout(r, 0));

    const main = document.querySelector("main");
    expect(main.textContent).toMatch(/powering off/i);
    const strongs = Array.from(main.querySelectorAll("strong")).map((el) => el.textContent);
    expect(strongs.filter((t) => t === "LitClock-Setup").length).toBeGreaterThanOrEqual(2);
    expect(main.textContent).not.toContain("{network}");
  });
});
