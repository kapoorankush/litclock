// Behavior coverage for the litclock-dev#607 resume-and-retry rules in updates.js.
//
// The bug (owner repro, v0.223.0 release day): tap Apply → reading list
// appears → the updater restarts litclock-control mid-run → the PWA's
// progress view dies and the page falls back to the stale "Apply update"
// card while the update marches on. Three rules fix it:
//
// 1. SERVER-RENDERED RESUME — when /updates renders with the reading list
//    already open (server saw a live run), the script must arm seenRunning
//    + schedulePoll on load, and must NOT fire refreshCheck (whose
//    state-mismatch reload would loop mid-update).
//
// 2. RETRY BEFORE RECONNECT — a single failed status poll re-arms the poll
//    (POLL_FAILURE_THRESHOLD=3); the view is not reset and no phantom tick
//    fires until three consecutive misses. A success in between resets the
//    counter.
//
// 3. STATUS-AWARE RECONNECT — in reconnect mode, a same-version /api/health
//    answer consults /api/update/status instead of beating until the 90s
//    deadline: running → resume the reading list + polling; complete →
//    reload (same-SHA content update); failed_* → the proper terminal copy
//    instead of "Couldn't reconnect".
//
// Pattern notes: state-flag mocks per tests/js house rules (the IIFE
// re-runs on every loadScript — see helpers/loadScript.js docstring).

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

// Mirrors src/control_server/templates/updates.html.j2 hooks. `inProgress`
// reproduces the litclock-dev#607 server-rendered mid-update page: card hidden, reading
// list open with the phase snapshot pre-set.
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

function phaseState(idx) {
  return document
    .querySelector(`.phase-row[data-phase-index="${idx}"]`)
    .getAttribute("data-state");
}

function statusCalls(mock) {
  return mock.calls.filter((c) => c.path === "/api/update/status").length;
}

function healthCalls(mock) {
  return mock.calls.filter((c) => c.path === "/api/health").length;
}

describe("updates.js litclock-dev#607 resume-after-restart", () => {
  let mock;
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

  it("rule 1: server-rendered in-progress page arms polling and suppresses refreshCheck", async () => {
    buildDom({ inProgress: true, phase: 3 });
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    mock.register(/\/api\/update\/status$/, {
      status: 200,
      body: { ok: true, state: "running", phase_index: 4 },
    });

    loadScript("updates.js");
    await flushMicrotasks();

    // refreshCheck must NOT have fired — its reload-on-mismatch would loop
    // while the update is rewriting the check answer underneath the page.
    expect(
      mock.calls.some((c) => c.path === "/api/update/check"),
      "refreshCheck must be suppressed on a server-rendered in-progress load"
    ).toBe(false);

    // The scheduled poll (armed at load, NOT by the probe) advances the view.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(statusCalls(mock)).toBeGreaterThanOrEqual(2); // probe + scheduled poll
    expect(phaseState(4)).toBe("active");
    expect(phaseState(3)).toBe("completed");
    expect(document.getElementById("phase-reading-list").hidden).toBe(false);
  });

  it("rule 2: one failed poll retries in place — no phantom tick, no reconnect, counter resets on success", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    // Status: cold-load probe idle → post-apply poll 1 FAILS → poll 2
    // succeeds (running/2) → polls 3+4 FAIL (counter must have reset — a
    // third consecutive-from-scratch failure is still below threshold
    // after only two misses) → poll 5 succeeds.
    const script = [
      { body: { ok: true, state: "idle" } }, // cold-load probe
      "fail", // poll 1
      { body: { ok: true, state: "running", phase_index: 2 } }, // poll 2
      "fail", // poll 3
      "fail", // poll 4
      { body: { ok: true, state: "running", phase_index: 3 } }, // poll 5
    ];
    mock.register(/\/api\/update\/status$/, () => {
      const step = script.shift() || "fail";
      if (step === "fail") throw new TypeError("network blip");
      return { ok: true, status: 200, json: async () => step.body };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    // Apply.
    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();
    expect(phaseState(1)).toBe("active");

    // Poll 1 fails → retry in place. View untouched, no reconnect.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(phaseState(1)).toBe("active");
    expect(phaseState(7)).toBe("upcoming");
    expect(healthCalls(mock)).toBe(0);
    expect(document.getElementById("phase-reading-list").hidden).toBe(false);

    // Poll 2 succeeds → phase advances, failure counter resets.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(phaseState(2)).toBe("active");

    // Polls 3+4 fail — still below the fresh threshold; poll 5 succeeds.
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(healthCalls(mock)).toBe(0);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(phaseState(3)).toBe("active");
    expect(healthCalls(mock)).toBe(0);
  });

  it("rule 3a: reconnect + same-version health + status running → resumes the reading list and polling", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    // Probe idle; 3 post-apply polls fail (enter reconnect); then the
    // pollHealth status consult + resumed polls see running/6.
    let statusFails = 0;
    const failingWindow = () => statusFails >= 1 && statusFails <= 3;
    mock.register(/\/api\/update\/status$/, () => {
      statusFails++;
      if (failingWindow()) throw new TypeError("restart window");
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, state: "running", phase_index: 6 }),
      };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } }); // SAME version

    loadScript("updates.js");
    await flushMicrotasks();
    // The cold-load probe consumed one counter slot (and failed — which
    // handleProbePayload(null) swallows without arming anything). Reset so
    // the failing window covers exactly the three post-apply polls.
    statusFails = 0;

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    // Three failed polls → reconnect mode.
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    expect(healthCalls(mock)).toBeGreaterThanOrEqual(1);
    // (The phantom tick on reconnect entry fires and is immediately
    // superseded by the resume below within the same microtask flush —
    // the tick itself is pinned by updates.optimistic-tick.test.js.)

    // Health answered with the SAME version → status consult → running/6 →
    // the view resumes at the REAL phase and polling restarts.
    await flushMicrotasks();
    expect(phaseState(6)).toBe("active");
    expect(phaseState(7)).toBe("upcoming");
    expect(document.getElementById("phase-reading-list").hidden).toBe(false);

    // Polling actually resumed: another 2s yields another status call.
    const before = statusCalls(mock);
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(statusCalls(mock)).toBeGreaterThan(before);
  });

  it("rule 3b: reconnect + same-version health + status failed_reverted → terminal copy, not 'Couldn't reconnect'", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount++;
      if (statusCallCount === 1) {
        // cold-load probe
        return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
      }
      if (statusCallCount <= 4) throw new TypeError("restart window");
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          state: "failed_reverted",
          phase_index: 5,
          error: "Smoke test failed; reverted to v0.223.0.",
        }),
      };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    await flushMicrotasks();

    const terminal = document.getElementById("phase-terminal-message");
    expect(terminal.hidden).toBe(false);
    expect(terminal.textContent).toContain("reverted");
    expect(terminal.dataset.tone).toBe("reverted");
    expect(phaseState(5)).toBe("failed");
  });

  it("rule 3d: reconnect + same-version health + status failed_unrecovered → error terminal with phase-0 default", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount++;
      if (statusCallCount === 1) {
        return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
      }
      if (statusCallCount <= 4) throw new TypeError("restart window");
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, state: "failed_unrecovered" }),
      };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    await flushMicrotasks();

    const terminal = document.getElementById("phase-terminal-message");
    expect(terminal.hidden).toBe(false);
    expect(terminal.dataset.tone).toBe("error");
    expect(terminal.textContent).toContain("did not finish");
    // phase_index absent → `|| 0` default: no row is failed-highlighted,
    // everything upcoming (updateRowStates(0, true) matches no index ≥ 1).
    expect(phaseState(1)).toBe("upcoming");
  });

  it("rule 3e: consult answering idle keeps the health loop beating, then the 90s deadline shows 'Couldn't reconnect'", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount++;
      if (statusCallCount === 1) {
        return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
      }
      if (statusCallCount <= 4) throw new TypeError("restart window");
      // Every consult thereafter: idle, never a verdict.
      return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    await flushMicrotasks();
    const healthAfterEntry = healthCalls(mock);
    expect(healthAfterEntry).toBeGreaterThanOrEqual(1);

    // The consult said idle — no verdict — so the loop must keep beating.
    await vi.advanceTimersByTimeAsync(3000);
    await flushMicrotasks();
    expect(healthCalls(mock)).toBeGreaterThan(healthAfterEntry);

    // Past the 90s deadline: the loop gives up with the reconnect terminal.
    await vi.advanceTimersByTimeAsync(95000);
    await flushMicrotasks();
    const terminal = document.getElementById("phase-terminal-message");
    expect(terminal.hidden).toBe(false);
    expect(terminal.textContent).toContain("reconnect");
    expect(terminal.dataset.tone).toBe("error");
    // And beating stopped: no further health calls after the give-up.
    const healthAtDeadline = healthCalls(mock);
    await vi.advanceTimersByTimeAsync(10000);
    await flushMicrotasks();
    expect(healthCalls(mock)).toBe(healthAtDeadline);
  });

  it("rule 3f: resume re-arms the machine — 3 fresh failures after a resume enter a SECOND reconnect", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    // probe(1) idle → polls 2-4 fail → reconnect#1 → consult(5) running/4
    // (resume) → polls 6-8 fail → reconnect#2 must fire health again.
    // Pins BOTH resume resets: a stale reconnectArmed would no-op the
    // second enterReconnectMode (polling goes silent forever), and a
    // stale failure counter would reconnect on the FIRST post-resume miss.
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount++;
      if (statusCallCount === 1) {
        return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
      }
      if (statusCallCount <= 4) throw new TypeError("restart window 1");
      if (statusCallCount === 5) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ok: true, state: "running", phase_index: 4 }),
        };
      }
      throw new TypeError("restart window 2");
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    // Reconnect #1 + resume.
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    await flushMicrotasks();
    expect(phaseState(4)).toBe("active"); // resumed at the real phase
    const healthAfterFirst = healthCalls(mock);

    // One post-resume miss must NOT reconnect (counter was reset)...
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    expect(healthCalls(mock)).toBe(healthAfterFirst);

    // ...but three misses must (reconnectArmed was reset).
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(2000);
    await flushMicrotasks();
    await flushMicrotasks();
    expect(healthCalls(mock)).toBeGreaterThan(healthAfterFirst);
  });

  it("rule 3c: reconnect + same-version health + status complete → exits the health loop (reload path)", async () => {
    mock.register(/\/api\/update\/check$/, { status: 200, body: { ok: true, available: true } });
    let statusCallCount = 0;
    mock.register(/\/api\/update\/status$/, () => {
      statusCallCount++;
      if (statusCallCount === 1) {
        return { ok: true, status: 200, json: async () => ({ ok: true, state: "idle" }) };
      }
      if (statusCallCount <= 4) throw new TypeError("restart window");
      return { ok: true, status: 200, json: async () => ({ ok: true, state: "complete" }) };
    });
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });
    mock.register(/\/api\/health$/, { status: 200, body: { version: "0.223.0" } });

    loadScript("updates.js");
    await flushMicrotasks();

    const form = document.querySelector("form[data-confirm-action='update_apply']");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    document.querySelector("[data-modal-confirm]").click();
    await flushMicrotasks();

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(2000);
      await flushMicrotasks();
    }
    await flushMicrotasks();

    // Complete branch schedules the reload behind SETTLE_DELAY_MS and stops
    // the health loop. jsdom can't observe location.reload, so pin the
    // observable: no FURTHER /api/health beats after the verdict, and no
    // error terminal shown.
    const healthAfterVerdict = healthCalls(mock);
    await vi.advanceTimersByTimeAsync(900); // < SETTLE_DELAY_MS, > nothing scheduled
    await flushMicrotasks();
    expect(healthCalls(mock)).toBe(healthAfterVerdict);
    expect(document.getElementById("phase-terminal-message").hidden).toBe(true);
  });
});
