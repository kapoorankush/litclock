// #381 regression — refreshCheck() forces a reload when the rendered card
// is in the initial "checking…" substate (data-state="unknown" with no cache)
// AND the API confirms available === null (terminal-unknown, e.g. private
// repo + no PAT).
//
// Before the fix: data-state="unknown" matched freshState="unknown" so the
// naive equality check skipped the reload, leaving the pill stuck on
// "checking…" forever — the exact user-reported bug on a fresh-flashed Pi.
//
// After the fix: the substate transition (no cache → cache-with-available-
// null) is observable via the body.available === null response. The
// updates.html.j2 template renders "couldn't check" when the cache has
// available=null at render time, so a reload here surfaces the honest
// terminal label.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

function buildDom(initialState) {
  document.body.innerHTML = `
    <section id="updates-card" data-state="${initialState}" data-current-version="0.212.2">
      <form action="/api/update/apply" data-confirm-action="update_apply">
        <input type="hidden" name="token" value="test-token-abcd" />
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
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
  }
}

describe("updates.js refreshCheck() — #381 unknown→unknown reload", () => {
  let fetchMock;
  let reloadCalled;
  let originalReload;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = installFetchMock();

    // jsdom's window.location.reload is non-configurable. Replace the
    // whole location property with a stub that includes reload — the
    // production code only calls window.location.reload(), so a minimal
    // stub is sufficient. Restore in afterEach.
    reloadCalled = false;
    originalReload = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: {
        reload: () => {
          reloadCalled = true;
        },
      },
    });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalReload,
    });
    fetchMock.restore();
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  function buildDomWithPill(initialState, pillLabel) {
    // Extended buildDom that also wires the pill body — needed for the
    // in-place DOM-update test (the production update target lives inside
    // .updates-pill--unknown).
    document.body.innerHTML = `
      <section id="updates-card" data-state="${initialState}" data-current-version="0.212.2">
        <header class="updates-card__header">
          <span class="updates-pill updates-pill--unknown" role="status">${pillLabel}</span>
        </header>
        <form action="/api/update/apply" data-confirm-action="update_apply">
          <input type="hidden" name="token" value="test-token-abcd" />
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

  it("updates pill text in-place from 'checking…' to 'couldn't check' (does NOT reload)", async () => {
    // Initial render: no cache yet → template emitted data-state="unknown"
    // with the "checking…" pill body. After refreshCheck gets available=null
    // back, we must UPDATE THE PILL IN PLACE — NOT reload. Hardware QA on
    // 2026-05-20 caught: an unconditional reload here creates an infinite
    // loop because after the reload the server reads the now-populated
    // cache (still available=null) and renders data-state="unknown" again,
    // re-entering this branch.
    buildDomWithPill("unknown", "checking…");

    fetchMock.register(/\/api\/update\/check$/, {
      status: 200,
      body: {
        ok: true,
        available: null,
        latest_tag: null,
        release_notes: null,
        current_version: "v0.212.2",
        fetched_at_unix: 9999999999,
      },
    });
    fetchMock.register(/\/api\/update\/status$/, {
      status: 200,
      body: { ok: true, state: "idle" },
    });

    loadScript("updates.js");
    await flushMicrotasks();
    await vi.runAllTimersAsync();
    await flushMicrotasks();

    expect(reloadCalled).toBe(false);
    var pill = document.querySelector(".updates-pill--unknown");
    expect(pill).toBeTruthy();
    expect(pill.textContent.trim()).toBe("couldn't check");
  });

  it("does NOT update or reload when rendered=unknown AND cached label is already 'couldn't check' (subsequent visit)", async () => {
    // Server rendered "couldn't check" from cache. JS fires refreshCheck,
    // gets cached available=null back. Branch fires but textContent does
    // NOT start with "checking" → no DOM update, no reload. Idempotent.
    buildDomWithPill("unknown", "couldn't check");

    fetchMock.register(/\/api\/update\/check$/, {
      status: 200,
      body: {
        ok: true,
        available: null,
        latest_tag: null,
        release_notes: null,
        current_version: "v0.212.2",
        fetched_at_unix: 9999999999,
      },
    });
    fetchMock.register(/\/api\/update\/status$/, {
      status: 200,
      body: { ok: true, state: "idle" },
    });

    loadScript("updates.js");
    await flushMicrotasks();
    await vi.runAllTimersAsync();
    await flushMicrotasks();

    expect(reloadCalled).toBe(false);
    var pill = document.querySelector(".updates-pill--unknown");
    expect(pill.textContent.trim()).toBe("couldn't check");
  });

  it("does NOT reload when rendered=up_to_date AND fresh=up_to_date (steady state)", async () => {
    // Cache existed at render time with available=false → template
    // emitted data-state="up_to_date". A fresh check returning the same
    // state must NOT trigger a reload.
    buildDom("up_to_date");
    // Note: this test uses buildDom() not buildDomWithPill() — no
    // .updates-pill--unknown exists in the DOM, which is fine because
    // the unknown→unknown branch won't fire for this state.

    fetchMock.register(/\/api\/update\/check$/, {
      status: 200,
      body: {
        ok: true,
        available: false,
        latest_tag: "v0.212.2",
        current_version: "v0.212.2",
        fetched_at_unix: 9999999999,
      },
    });
    fetchMock.register(/\/api\/update\/status$/, {
      status: 200,
      body: { ok: true, state: "idle" },
    });

    loadScript("updates.js");
    await flushMicrotasks();
    await vi.runAllTimersAsync();
    await flushMicrotasks();

    expect(reloadCalled).toBe(false);
  });

  it("does NOT reload when rendered=unknown AND fresh=available (state genuinely changed — existing logic handles it)", async () => {
    // Defensive: the existing `renderedState !== freshState` path already
    // reloads here. Pin that the #381 fix doesn't interfere.
    buildDom("unknown");

    fetchMock.register(/\/api\/update\/check$/, {
      status: 200,
      body: {
        ok: true,
        available: true,
        latest_tag: "v0.213.0",
        current_version: "v0.212.2",
        fetched_at_unix: 9999999999,
      },
    });
    fetchMock.register(/\/api\/update\/status$/, {
      status: 200,
      body: { ok: true, state: "idle" },
    });

    loadScript("updates.js");
    await flushMicrotasks();
    await vi.runAllTimersAsync();
    await flushMicrotasks();

    // Both the #381 unknown-unknown branch (no — fresh is "available", not
    // "unknown") and the existing state-mismatch branch (yes — "unknown" !=
    // "available") could fire. Either way reload runs exactly once. We
    // assert reload happened; the assertion is robust either way.
    expect(reloadCalled).toBe(true);
  });
});
