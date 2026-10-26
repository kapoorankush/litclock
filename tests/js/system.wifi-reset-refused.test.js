// litclock-dev#939 — POST /api/wifi/reset answers 200 as soon as the job is
// queued, but litclock-wifi-reset.sh can still refuse (nmcli pre-flight, the
// litclock-dev#938 firstboot-enable guard) BEFORE it stops the control server. The PWA
// used to show "Join LitClock-Setup" regardless, for a network that never
// appears. It now asks GET /api/wifi/reset/status, which reads the reset unit,
// and swaps the card ONLY on "refused". The litclock-dev#949 review killed the first
// design (infer a refusal from the server still answering after 20s): that
// said "didn't start" after a reset that worked and was re-provisioned onto
// the same network while the tab slept, and one slow reply hid a refusal.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadScript, installFetchMock, stubDialog } from "./helpers/loadScript.js";

const WATCH_MS = 90000;
const POLL_MS = 2000;

const ok = (state) => ({
  ok: true,
  status: 200,
  json: async () => ({ state }),
  text: async () => JSON.stringify({ state }),
});

describe("system.js Reset WiFi refused (litclock-dev#939)", () => {
  let mock;
  let restoreDialog;
  let answers; // queue of status answers: a state string, "__neterr__", or a Response-like
  let lastAnswer;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = `
      <main>
        <form data-confirm-action="wifi_reset" action="/api/wifi/reset">
          <input type="hidden" name="token" value="tok" />
          <button type="submit">Reset WiFi</button>
        </form>
      </main>
      <dialog class="confirm-sheet" data-action="wifi_reset">
        <button data-modal-confirm>Reset WiFi</button>
        <button data-modal-cancel>Cancel</button>
      </dialog>
    `;
    restoreDialog = stubDialog();
    window.requestAnimationFrame = (cb) => cb();
    mock = installFetchMock();
    mock.register(/\/api\/wifi\/reset$/, { status: 200, body: { ok: true } });
    answers = [];
    lastAnswer = "pending";
    // A function response is passed through as-is, so it must be Response-shaped.
    mock.register(/\/api\/wifi\/reset\/status/, () => {
      const a = answers.length ? answers.shift() : lastAnswer;
      if (answers.length === 0) lastAnswer = a;
      if (a === "__neterr__") throw new TypeError("Failed to fetch");
      return typeof a === "string" ? ok(a) : a;
    });
    loadScript("system.js");
  });

  afterEach(() => {
    mock.restore();
    restoreDialog();
    vi.useRealTimers();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  async function confirmReset() {
    document.querySelector('dialog[data-action="wifi_reset"] [data-modal-confirm]').click();
    await vi.advanceTimersByTimeAsync(0);
  }

  const state = () => document.querySelector("main [data-state]")?.getAttribute("data-state");
  const statusCalls = () => mock.calls.filter((c) => c.path === "/api/wifi/reset/status").length;

  it("swaps to the refused card as soon as the clock reports the refusal, then stops asking", async () => {
    answers = ["pending", "pending", "refused"];
    await confirmReset();
    expect(state()).toBe("wifi-reset");
    await vi.advanceTimersByTimeAsync(2 * POLL_MS + 10);
    expect(state()).toBe("wifi-reset");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(state()).toBe("wifi-reset-refused");
    const html = document.querySelector("main").innerHTML;
    expect(html).toMatch(/didn’t start/);
    expect(html).not.toMatch(/LitClock-Setup/);
    expect(document.activeElement).toBe(document.querySelector("main .reconnect-state__retry"));
    const calls = statusCalls();
    await vi.advanceTimersByTimeAsync(WATCH_MS);
    expect(statusCalls()).toBe(calls); // the verdict is terminal
  });

  it("a dropped or slow reply is not a verdict: the watch survives it", async () => {
    answers = ["__neterr__", "__neterr__", "refused"];
    await confirmReset();
    await vi.advanceTimersByTimeAsync(3 * POLL_MS + 10);
    expect(state()).toBe("wifi-reset-refused");
  });

  it.each(["running", "done", "unknown", "pending"])(
    "%s never becomes 'didn't start', and the watch ends at the window",
    async (s) => {
      lastAnswer = s;
      await confirmReset();
      await vi.advanceTimersByTimeAsync(WATCH_MS + 4 * POLL_MS);
      expect(state()).toBe("wifi-reset");
      const calls = statusCalls();
      await vi.advanceTimersByTimeAsync(WATCH_MS);
      expect(statusCalls()).toBe(calls);
    }
  );

  it("a reset that took the server down keeps the handoff card", async () => {
    lastAnswer = "__neterr__";
    await confirmReset();
    await vi.advanceTimersByTimeAsync(WATCH_MS + 4 * POLL_MS);
    expect(state()).toBe("wifi-reset");
  });

  it("an older server without the endpoint (404) keeps the handoff card", async () => {
    lastAnswer = { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    await confirmReset();
    await vi.advanceTimersByTimeAsync(WATCH_MS + 4 * POLL_MS);
    expect(state()).toBe("wifi-reset");
  });

  it("returning to the tab after a successful re-provision does NOT say it didn't start", async () => {
    // The tab slept through the window; the clock is back on the same
    // network and answering, and its unit finished cleanly.
    lastAnswer = "done";
    await confirmReset();
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
    expect(state()).toBe("wifi-reset");
  });

  it("the refused card's button reloads the System tab", async () => {
    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, reload });
    answers = ["refused"];
    await confirmReset();
    await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    document.querySelector("main .reconnect-state__retry").click();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
