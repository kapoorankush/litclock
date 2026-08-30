// litclock-dev#532 slice 8 — the SHELL blob path (data-litclock-shell-strings,
// base-level, read by drawer.js + handoff.js) must actually WIN over the
// English fallbacks when present (Codex slice-8 /review: the drawer/handoff
// fixtures omit the blob, so only fallbacks were exercised).

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

const SHELL_BLOB = {
  "handoff.fail_body_tz": "TRANSLATED {tz} BODY {tz} TWICE",
};

let mock;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\//, { status: 200, body: { ok: true } });
});

afterEach(() => {
  mock.restore();
  document.body.innerHTML = "";
});

describe("shell blob wins (litclock-dev#532 slice 8)", () => {
  it("handoff fail body uses the blob value with EVERY {tz} filled", () => {
    document.body.innerHTML = `
      <script type="application/json" data-litclock-shell-strings>${JSON.stringify(SHELL_BLOB)}</script>
      <section id="handoff-banner" data-handoff-state="failure">
        <p id="handoff-fail-body">SSR fallback body</p>
        <button type="button" id="handoff-set-tz"
                data-tz-label-template="Usar {tz} y {tz}"
                data-fallback-label="Set my timezone">Set my timezone</button>
      </section>
    `;
    loadScript("handoff.js");
    const body = document.getElementById("handoff-fail-body").textContent;
    const btn = document.getElementById("handoff-set-tz").textContent;
    // Intl in jsdom resolves a real tz — both {tz} slots must be filled in
    // the blob-sourced body AND the attr-sourced button label (split/join,
    // not single .replace — Codex slice-8 /review).
    expect(body).toContain("TRANSLATED ");
    expect(body).not.toContain("{tz}");
    expect(btn).toMatch(/^Usar .+ y .+$/);
    expect(btn).not.toContain("{tz}");
  });

  // drawer.js's blob read is covered TRANSITIVELY: the Python parity suite
  // pins drawer's accessor byte-identical to handoff's (the shell pair
  // lockstep), and handoff's accessor is behavior-tested above. A direct
  // drawer rendering test would need the full drawer DOM + SSE flow for no
  // additional accessor coverage.
});
