// Behavior coverage for the #329 optimistic-tick rule in updates.js (#338).
//
// The rule (lifted verbatim from updates.js:34-41):
//
//   track whether we've ever seen state=running in this session. When a
//   single /api/update/status poll fails after we've seen running, Phase 7's
//   litclock-control restart is the most likely cause — optimistically tick
//   all 7 phases visually before entering reconnect mode, instead of leaving
//   the user staring at a Phase 4 spinner for 30-60s while the page reloads.
//   Page-load probes (no update in flight) keep their existing behavior so
//   a fresh /updates load doesn't phantom-tick.
//
// Three scenarios pin this:
//
// 1. Positive: user taps Apply → POST returns 202 → seenRunning=true via
//    fireApply → next scheduled /api/update/status rejects (Phase 7 restart
//    is killing waitress mid-fetch) → handleStatusPayload(null) hits the
//    `if (seenRunning)` branch → updateRowStates(7, false) AND
//    enterReconnectMode → /api/health fetch fires.
//
// 2. Negative (the rule): cold page load with no apply → first probe rejects
//    → seenRunning stays false → no phantom tick.
//
// 3. Probe-running (#342 I10): cold-load probe SEES state=running →
//    enterReadingList AND schedulePoll fire → a subsequent /api/update/status
//    poll happens (proves the probe path arms the cycle, not just fireApply).
//
// Source-pin tests in tests/test_control_server.py:875-921 stay as belt-and-
// suspenders for the literal `seenRunning = true` assignment; this file
// covers the actual state machine.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

// Mirrors src/control_server/templates/updates.html.j2:120-160 (phase-row
// markup) + the form/dialog hooks updates.js queries. If a future template
// rename or restructure breaks the production card, this test's synthetic
// DOM will stay green — keep this scaffold in lockstep with the template
// hooks. (#338, codex maintainability finding.)
function buildDom() {
  document.body.innerHTML = `
    <section id="updates-card" data-state="available" data-current-version="0.211.4">
      <form action="/api/update/apply" data-confirm-action="update_apply">
        <input type="hidden" name="token" value="test-token-abcd" />
        <button type="submit">Apply</button>
      </form>
    </section>

    <dialog class="confirm-sheet" data-action="update_apply">
      <button type="button" data-modal-cancel>Cancel</button>
      <button type="button" data-modal-confirm>Confirm</button>
    </dialog>

    <ol id="phase-reading-list" hidden>
      <li class="phase-row" data-phase-index="1" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="2" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="3" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="4" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="5" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="6" data-state="upcoming"></li>
      <li class="phase-row" data-phase-index="7" data-state="upcoming"></li>
    </ol>
    <p id="phase-terminal-message" hidden></p>
  `;
}

// Drains pending microtasks AND any 0-ms timers. Vitest's fake timers leave
// microtasks alone, so this just `await Promise.resolve()`s a couple of
// times to let `.then` chains settle.
async function flushMicrotasks() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
  }
}

function phaseState(idx) {
  return document
    .querySelector(`.phase-row[data-phase-index="${idx}"]`)
    .getAttribute("data-state");
}

describe("updates.js #329 optimistic-tick", () => {
  let mock;
  // No-op default so afterEach can't crash if beforeEach throws before
  // stubDialog() runs — the original error would otherwise be masked by
  // "restoreDialog is not a function". Codex adversarial finding.
  let restoreDialog = () => {};

  beforeEach(() => {
    buildDom();
    restoreDialog = stubDialog();
    vi.useFakeTimers();
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
    restoreDialog();
    restoreDialog = () => {};
    vi.useRealTimers();
  });

  it("positive: Apply → 202 → status rejects → ticks all 7 phases + enters reconnect", async () => {
    // refreshCheck must NOT trigger window.location.reload — card state
    // 'available' must match `available: true` here.
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });

    // Track /api/update/status calls; first hit (cold-load probe) returns a
    // network failure that handleProbePayload's `if (!payload) return;`
    // swallows without arming seenRunning. Subsequent hits (the post-apply
    // scheduled polls) also reject — that's the Phase 7 waitress restart
    // window where seenRunning is true and the optimistic tick must fire.
    mock.register(/\/api\/update\/status$/, () => {
      throw new TypeError("network blip");
    });

    // The apply POST returns 202 — this is the line that sets seenRunning=true
    // via fireApply.then.
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });

    // Health probe in reconnect mode.
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.212.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    // Cold-load probe should have returned null and exited early — reading
    // list still hidden, seenRunning still false at this point.
    expect(document.getElementById("phase-reading-list").hidden).toBe(true);

    // Submit the form → openConfirmSheet → dialog.showModal (stubbed).
    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    const dialog = document.querySelector("dialog.confirm-sheet[data-action='update_apply']");
    expect(dialog.open).toBe(true);

    // Click confirm — dialog.close('confirm') + fireApply().
    document.querySelector("[data-modal-confirm]").click();
    expect(dialog.open).toBe(false);

    // Let the apply POST settle (fireApply → 202 → seenRunning=true →
    // enterReadingList({state:'running', phase_index:1}) + schedulePoll).
    await flushMicrotasks();

    expect(document.getElementById("phase-reading-list").hidden).toBe(false);
    expect(phaseState(1)).toBe("active");

    // Advance the 2s status poll. Status fetch will reject → cb(null) →
    // handleStatusPayload(null). seenRunning is true so updateRowStates(7,
    // false) fires AND cancelPolling + enterReconnectMode → /api/health.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();

    // The rule: rows 1-6 became 'completed', row 7 became 'active'.
    // updateRowStates(7, false) marks idx<7 as completed and idx===7 as
    // active (see updates.js:467-481).
    for (let i = 1; i <= 6; i++) {
      expect(phaseState(i), `phase ${i} should be completed after optimistic tick`).toBe(
        "completed"
      );
    }
    expect(phaseState(7)).toBe("active");

    // Reconnect mode armed: /api/health was hit at least once.
    const healthCalls = mock.calls.filter((c) => c.path === "/api/health");
    expect(healthCalls.length).toBeGreaterThanOrEqual(1);
  });

  it("negative (the rule): cold-load probe failure WITHOUT prior Apply must NOT phantom-tick", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    // The cold-load probe rejects. The probe's handleProbePayload(null)
    // returns early — seenRunning is NEVER armed. No phantom tick.
    mock.register(/\/api\/update\/status$/, () => {
      throw new TypeError("network blip");
    });

    loadScript("updates.js");
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(2500);
    await flushMicrotasks();

    // Prove the cold-load probe actually fired before asserting no-phantom-
    // tick. If a future refactor moves the probe behind a delay so it never
    // fires in 2.5s, the assertions below pass for the wrong reason
    // (probe never ran ≠ probe ran and didn't tick). Codex adversarial
    // finding.
    expect(
      mock.calls.some((c) => c.path === "/api/update/status"),
      "cold-load probe must hit /api/update/status — otherwise no-phantom-tick is unverified"
    ).toBe(true);

    // Reading list must still be hidden, phase rows untouched.
    expect(document.getElementById("phase-reading-list").hidden).toBe(true);
    for (let i = 1; i <= 7; i++) {
      expect(phaseState(i), `phase ${i} should stay 'upcoming' on cold load`).toBe(
        "upcoming"
      );
    }
  });

  it("probe-running (#342 I10): cold-load probe sees state=running → reading list + poll cycle armed", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });

    // Multi-shot: first /api/update/status call (cold-load probe) returns
    // running:phase_index=3; subsequent calls return running:phase_index=4.
    // Proves both that the probe enterReadingList'd AND that schedulePoll
    // followed (a second call landed via the 2s setTimeout cycle).
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount += 1;
      const phase = statusCallCount === 1 ? 3 : 4;
      return {
        ok: true,
        status: 200,
        json: async () => ({ state: "running", phase_index: phase }),
        text: async () => "",
      };
    });

    loadScript("updates.js");
    await flushMicrotasks();

    // After the probe lands the reading list should be visible and phase 3
    // is active.
    expect(document.getElementById("phase-reading-list").hidden).toBe(false);
    expect(phaseState(3)).toBe("active");

    // Advance the 2s poll. handleStatusPayload sees state='running' →
    // updateRowStates(payload.phase_index=4) → schedulePoll AGAIN.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();

    expect(statusCallCount).toBeGreaterThanOrEqual(2);
    expect(phaseState(4)).toBe("active");
    expect(phaseState(3)).toBe("completed");
  });
});
