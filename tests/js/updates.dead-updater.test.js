// Behavior coverage for the litclock-dev#636 dead-updater rules in updates.js.
//
// The bug (Codex, litclock-dev#634 port review): /api/update/status passes a `running`
// status file through even when the unit is idle — the dead-updater state
// (`running` file + idle unit; SIGKILL/OOM skipped update.sh's EXIT trap,
// and the file lies until reboot clears tmpfs). The JS poll loop treated
// every `running` as live and rescheduled forever, so an already-open
// Updates page spun on a corpse until a manual reload.
//
// The fix keeps the route's verdict-free posture (litclock-dev#607 review: a false
// "dead" mid-run would resurrect the stale-card bug) and splits the work:
// the route reports the evidence (`unit_busy` on running payloads), and the
// poll loop acts only on SUSTAINED evidence:
//
// 1. LIVE RUNS UNAFFECTED — unit_busy:true keeps the counter at zero; a
//    payload with NO unit_busy field (pre-#636 server) is treated as live
//    forever, never counted toward a dead verdict.
// 2. SUSTAINED DEAD → TERMINAL, NOT RELOAD — DEAD_UPDATER_POLL_THRESHOLD
//    consecutive unit_busy:false readings stop polling and (if the reading
//    list is showing) render honest terminal copy. A reload is forbidden:
//    the dead file persists, so reloading would loop.
// 3. A SINGLE IDLE READING PROVES NOTHING — the server's 2s unit memo can
//    lag the dispatch window; any unit_busy:true resets the counter.
// 4. COLD LOAD ONTO A CORPSE — the probe must NOT swap the truthful
//    server-rendered card for a reading list on the file's word alone; it
//    polls, and promotes into the reading list only once the unit is
//    confirmed busy.
//
// Pattern notes: state-flag mocks per tests/js house rules (the IIFE
// re-runs on every loadScript — see helpers/loadScript.js docstring).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

function buildDom({ inProgress = false, phase = 3 } = {}) {
  const rowState = (idx) =>
    !inProgress ? "upcoming" : idx < phase ? "completed" : idx === phase ? "active" : "upcoming";
  document.body.innerHTML = `
    <section id="updates-card" data-state="available" data-current-version="0.223.0"
             ${inProgress ? "hidden" : ""}>
      <form action="/api/update/apply" data-confirm-action="update_apply">
        <input type="hidden" name="token" value="test-token-abcd" />
        <button type="submit">Apply</button>
      </form>
    </section>

    <dialog class="confirm-sheet" data-action="update_apply">
      <button type="button" data-modal-cancel>Cancel</button>
      <button type="button" data-modal-confirm>Confirm</button>
    </dialog>

    <ol id="phase-reading-list" ${inProgress ? "" : "hidden"}>
      ${[1, 2, 3, 4, 5, 6, 7]
        .map(
          (i) =>
            `<li class="phase-row" data-phase-index="${i}" data-state="${rowState(i)}"></li>`
        )
        .join("\n")}
    </ol>
    <p id="phase-terminal-message" hidden></p>
  `;
}

async function flushMicrotasks() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
  }
}

function statusCalls(mock) {
  return mock.calls.filter((c) => c.path === "/api/update/status").length;
}

function terminalEl() {
  return document.getElementById("phase-terminal-message");
}

function readingListEl() {
  return document.getElementById("phase-reading-list");
}

// Per-call sequencer: yields each body in order, then repeats the last one.
function sequencedStatus(bodies) {
  const queue = bodies.slice();
  return () => {
    const body = queue.length > 1 ? queue.shift() : queue[0];
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  };
}

const running = (phase, unitBusy) => {
  const body = { ok: true, state: "running", phase_index: phase };
  if (unitBusy !== undefined) body.unit_busy = unitBusy;
  return body;
};

describe("updates.js litclock-dev#636 dead-updater detection", () => {
  let mock;
  let restoreDialog = () => {};

  beforeEach(() => {
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

  it("rule 1: unit_busy:true and field-absent payloads never accumulate a dead verdict", async () => {
    buildDom({ inProgress: true, phase: 3 });
    // Alternate true/absent — neither may count toward the threshold.
    mock.register(
      /\/api\/update\/status$/,
      sequencedStatus([running(3, true), running(3), running(4, true), running(4), running(4, true), running(4)])
    );

    loadScript("updates.js");
    await flushMicrotasks();
    for (let i = 0; i < 8; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }

    expect(terminalEl().hidden, "no terminal copy on a live run").toBe(true);
    // Polling is still alive well past the threshold span.
    const before = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(statusCalls(mock)).toBeGreaterThan(before);
  });

  it("rule 2: five consecutive unit_busy:false polls stop polling and show honest terminal copy", async () => {
    buildDom({ inProgress: true, phase: 4 });
    mock.register(/\/api\/update\/status$/, sequencedStatus([running(4, false)]));

    loadScript("updates.js");
    await flushMicrotasks();
    for (let i = 0; i < 7; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }

    expect(terminalEl().hidden, "terminal copy must render once the corpse is confirmed").toBe(false);
    expect(terminalEl().textContent).toMatch(/stopped before finishing/i);
    expect(terminalEl().textContent).toMatch(/previous version/i);

    // The in-flight phase row must be frozen FAILED, not left spinning: a
    // spinner animating above "the updater is no longer running" is the
    // exact contradiction the verdict's updateRowStates(..., true) fixes.
    expect(
      document.querySelector('.phase-row[data-phase-index="4"]').getAttribute("data-state"),
      "the active phase must freeze failed under the verdict copy"
    ).toBe("failed");
    expect(
      document.querySelector('.phase-row[data-phase-index="1"]').getAttribute("data-state"),
      "earlier phases stay completed"
    ).toBe("completed");

    // Polling stopped — and no reload was issued (the dead file persists;
    // a reload would loop). jsdom throws on navigation, so reaching this
    // point without an error is itself the no-reload proof; pin the
    // poll-stop explicitly:
    const settled = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(10000);
    await flushMicrotasks();
    expect(statusCalls(mock), "polling must stop at the dead verdict").toBe(settled);
  });

  it("rule 3: a unit_busy:true reading resets the counter — 4 dead + live + 4 dead stays live", async () => {
    buildDom({ inProgress: true, phase: 2 });
    const seq = [
      running(2, false), running(2, false), running(2, false), running(2, false),
      running(3, true),
      running(3, false), running(3, false), running(3, false), running(3, false),
      running(3, true),
    ];
    mock.register(/\/api\/update\/status$/, sequencedStatus(seq));

    loadScript("updates.js");
    await flushMicrotasks();
    // Probe consumes seq[0]; 9 more polls consume the rest.
    for (let i = 0; i < 9; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }

    expect(terminalEl().hidden, "4+4 non-consecutive dead readings must not verdict").toBe(true);
    const before = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(statusCalls(mock)).toBeGreaterThan(before);
  });

  it("rule 4: cold load onto a corpse keeps the card; a confirmed-busy poll promotes into the reading list", async () => {
    buildDom(); // card view — server render already applied the authoritative check
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(
      /\/api\/update\/status$/,
      sequencedStatus([running(1, false), running(2, true)])
    );

    loadScript("updates.js");
    await flushMicrotasks();

    // Probe saw running+unit_busy:false — the reading list must NOT appear.
    expect(readingListEl().hidden, "probe must not trust a corpse file on cold load").toBe(true);

    // Next poll confirms the unit busy (memo lag case) — promote.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(readingListEl().hidden, "confirmed-busy poll promotes into the in-progress view").toBe(false);
  });

  it("rule 5: a fresh Apply resets the corpse counter — one stale idle sample cannot kill the new run", async () => {
    // Codex P1 on the litclock-dev#636 review: after a corpse verdict the
    // counter sat at threshold; without the fireApply reset, the FIRST
    // running+unit_busy:false sample of the next run (entirely possible —
    // the route's 2s unit memo can still hold the corpse-era False right
    // after dispatch) instantly re-tripped the threshold and rendered a
    // false dead-updater terminal over a healthy fresh run.
    buildDom();
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    const seq = [
      // cold-load probe + 5 polls confirm the corpse (counter hits threshold)
      running(1, false),
      // post-Apply: one stale-memo idle sample, then the run is visibly live
      running(1, false), running(2, true), running(3, true),
    ];
    // First entry repeats for the corpse phase; switch the sequencer after.
    let phase2 = false;
    let i = 0;
    mock.register(/\/api\/update\/status$/, () => {
      const body = phase2 ? seq[Math.min(1 + i++, seq.length - 1)] : seq[0];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });

    loadScript("updates.js");
    await flushMicrotasks();
    for (let k = 0; k < 7; k++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    // Corpse confirmed quietly (card view), polling stopped.
    const settled = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(4000);
    await flushMicrotasks();
    expect(statusCalls(mock)).toBe(settled);

    // User taps Apply for a fresh run.
    phase2 = true;
    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();
    expect(readingListEl().hidden, "fresh Apply enters the reading list").toBe(false);

    // One stale idle sample must NOT terminal the fresh run…
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(terminalEl().hidden, "single stale sample after Apply must not verdict").toBe(true);
    // …and the run keeps advancing normally once the memo catches up.
    await vi.advanceTimersByTimeAsync(4000);
    await flushMicrotasks();
    expect(terminalEl().hidden).toBe(true);
    const before = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(statusCalls(mock), "polling continues on the live run").toBeGreaterThan(before);
  });

  it("rule 4b: cold load onto a confirmed corpse stops quietly — card stays, no terminal copy", async () => {
    buildDom();
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/update\/status$/, sequencedStatus([running(1, false)]));

    loadScript("updates.js");
    await flushMicrotasks();
    for (let i = 0; i < 7; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }

    expect(readingListEl().hidden, "card remains the surface").toBe(true);
    expect(terminalEl().hidden, "no terminal copy outside the reading list").toBe(true);
    const settled = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(10000);
    await flushMicrotasks();
    expect(statusCalls(mock), "polling stops on the confirmed corpse").toBe(settled);
  });

  it("rule 5b: the fireApply network-error catch path also resets the corpse counter", async () => {
    // The second reset site (fireApply's .catch → optimistic reading list on
    // a failed POST). A corpse-era count carried into that optimistic run
    // would false-terminal it on the first memo-lag sample, same as rule 5's
    // success-path scenario.
    buildDom();
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    // The apply POST rejects (network glitch) → catch path claims the run.
    // A throwing function (not Promise.reject at registration time, which
    // would reject before anything awaits it) — the mock turns the throw
    // into a rejected fetch, exercising fireApply's real .catch branch.
    mock.register(/\/api\/update\/apply$/, () => {
      throw new TypeError("network glitch");
    });
    let phase2 = false;
    mock.register(/\/api\/update\/status$/, () => {
      const body = phase2 ? running(2, true) : running(1, false);
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });

    loadScript("updates.js");
    await flushMicrotasks();
    for (let k = 0; k < 7; k++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    const settled = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(4000);
    await flushMicrotasks();
    expect(statusCalls(mock), "corpse confirmed, polling stopped").toBe(settled);

    // Apply → POST rejects → catch → claimOptimisticRun resets the counter.
    phase2 = true;
    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();
    expect(readingListEl().hidden, "catch path enters the optimistic reading list").toBe(false);
    // The counter is fresh, so the run advances normally, no false verdict.
    await vi.advanceTimersByTimeAsync(4000);
    await flushMicrotasks();
    expect(terminalEl().hidden, "fresh optimistic run must not false-verdict").toBe(true);
  });

  it("rule 6: the dialog-open guard defers promotion, then promotes after the sheet closes", async () => {
    // updates.js:!(dialog && dialog.open) — yanking the card into the reading
    // list under an open confirm sheet is the litclock-dev#354 race the guard
    // mirrors. Rule 4 only covers promotion with the sheet closed.
    buildDom();
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    // Probe sees a corpse candidate (card kept); every later poll is
    // confirmed-busy, which would normally promote.
    mock.register(/\/api\/update\/status$/, sequencedStatus([running(1, false), running(2, true)]));

    loadScript("updates.js");
    await flushMicrotasks();
    expect(readingListEl().hidden, "probe keeps the card").toBe(true);

    // Open the confirm sheet, THEN let a confirmed-busy poll land.
    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    const dialog = document.querySelector("dialog.confirm-sheet[data-action='update_apply']");
    expect(dialog.open).toBe(true);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(readingListEl().hidden, "must NOT yank the card under an open sheet").toBe(true);

    // Cancel the sheet; the next poll promotes.
    document.querySelector("[data-modal-cancel]").click();
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(readingListEl().hidden, "promotes once the sheet is closed").toBe(false);
  });

  it("rule 7: a reconnect resume resets the corpse counter (a pre-disconnect count can't false-verdict)", async () => {
    // updates.js reconnect-resume: the count from before the Phase-7 restart
    // is stale evidence. Accumulate 4 idle readings, drop into reconnect via
    // poll failures, resume on a live status, then one more idle sample —
    // without the reset that lone sample would be the 5th and false-verdict.
    buildDom({ inProgress: true, phase: 3 });
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } }); // same version → status consult
    let n = 0;
    mock.register(/\/api\/update\/status$/, () => {
      n++;
      // 1: cold-load probe (idle, harmless). 2-5: four idle readings in the
      // list (count → 4). 6-8: three network failures → reconnect. 9: the
      // pollHealth status consult — a LIVE resume (unit_busy:true). 10+: idle.
      if (n >= 6 && n <= 8) throw new TypeError("restart window");
      const body = n === 9 ? running(3, true) : running(3, false);
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });

    loadScript("updates.js");
    await flushMicrotasks();
    // Drive: 4 idle (count→4), 3 failures (reconnect), health+resume, then idle.
    for (let i = 0; i < 9; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    // Resume reset the counter; a handful of post-resume idle samples must
    // not have summed with the pre-disconnect 4 to reach the threshold.
    expect(terminalEl().hidden, "pre-disconnect count must not carry across a resume").toBe(true);
    expect(readingListEl().hidden, "the live resume shows the reading list").toBe(false);
  });
});
