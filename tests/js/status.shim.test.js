// Behavior coverage for the #335 backward-compat shim in status.js (#338).
//
// The shim (status.js:139-149) is a defensive fallback for service-worker-
// served HTML from before PR #333 added the three-span structure for the
// "Last update" row. When all three child hooks
// ([data-status-last-update-{version,sep,relative}]) are absent — the cached
// SHA from pre-#333 — patch() falls back to writing the combined "sha, rel"
// string into the parent [data-status-last-update] so the row populates
// instead of freezing silently. On the post-#333 path, the child hooks
// exist and the shim must NOT fire: the children carry mono SHA styling +
// hidden state that the shim would clobber if it ran unconditionally.
//
// Source-pin tests in tests/test_control_server.py:814-870 stay as belt-and-
// suspenders for the SSR markup contract (three-span structure exists in
// the template); this file covers the JS state machine.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

// Minimal status payload — fields not under test are stubbed with defaults
// matching the production /api/status shape so status.js's patch() doesn't
// error on unrelated rows. Only the last_update_* fields actually drive
// these assertions.
function baseStatusPayload(extras = {}) {
  return {
    ok: true,
    stale: false,
    picked_at_age_s: null,
    quote: null,
    author: null,
    title: null,
    time: null,
    wifi_ssid: null,
    weather_city: null,
    version: null,
    uptime_human: "0s",
    last_update_version: null,
    last_update_at_relative: "—",
    ...extras,
  };
}

// Mirrors src/control_server/templates/status.html.j2:104-117 (last-update
// row + the hero/banner hooks status.js queries). The withChildren=false
// variant simulates pre-#333 SW-cached HTML (just the parent dd). Keep in
// lockstep with the template hooks — a synthetic-DOM test silently passes
// against its own scaffold if production markup drifts. (#338, codex
// maintainability finding.)
function buildDom({ withChildren }) {
  const lastUpdateRow = withChildren
    ? `<dd data-status-last-update>
         <span class="mono" data-status-last-update-version hidden></span>
         <span data-status-last-update-sep hidden>,&nbsp;</span>
         <span data-status-last-update-relative>—</span>
       </dd>`
    : `<dd data-status-last-update>—</dd>`;

  document.body.innerHTML = `
    <div data-status-stale-banner hidden>
      <span data-status-stale-text></span>
    </div>
    <section>
      <div data-status-hero-full hidden>
        <blockquote data-status-quote></blockquote>
        <p>
          <span data-status-attr-prefix></span>
          <span data-status-attr-title-wrap hidden><em data-status-attr-title></em></span>
          <span data-status-attr-time-wrap hidden><span data-status-attr-time></span></span>
        </p>
      </div>
      <p data-status-hero-empty></p>
    </section>
    <dl>
      <div><dt>WiFi</dt><dd data-status-wifi>—</dd></div>
      <div><dt>Weather</dt><dd data-status-weather>—</dd></div>
      <div><dt>Version</dt><dd data-status-version>—</dd></div>
      <div><dt>Uptime</dt><dd data-status-uptime>—</dd></div>
      <div><dt>Last update</dt>${lastUpdateRow}</div>
    </dl>
  `;
}

// status.js kicks off its first refresh via requestAnimationFrame (~16ms
// under jsdom's pretendToBeVisual mode) then schedules a 30s setInterval.
// We use real timers — the 30s interval never fires in <1s test runtime,
// and fake-timer runAll would loop forever on the self-re-arming interval.
// 60ms is enough cushion for rAF + the mocked fetch's microtask chain to
// settle on any reasonable CI host.
async function flushRefresh() {
  await new Promise((r) => setTimeout(r, 60));
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("status.js #335 last-update shim", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
  });

  it("shim fires when child hooks are absent (pre-#333 SW-cached HTML)", async () => {
    buildDom({ withChildren: false });
    mock.register(
      /\/api\/status$/,
      {
        status: 200,
        body: baseStatusPayload({
          last_update_version: "5f12b8b",
          last_update_at_relative: "5 min ago",
        }),
      }
    );

    loadScript("status.js");
    await flushRefresh();

    const parent = document.querySelector("[data-status-last-update]");
    // Parent received the combined fallback string verbatim — no child
    // spans were created.
    expect(parent.children.length).toBe(0);
    expect(parent.textContent).toBe("5f12b8b, 5 min ago");
  });

  it("shim does NOT fire on the post-#333 path — children stay separate, styling preserved", async () => {
    buildDom({ withChildren: true });
    mock.register(
      /\/api\/status$/,
      {
        status: 200,
        body: baseStatusPayload({
          last_update_version: "5f12b8b",
          last_update_at_relative: "5 min ago",
        }),
      }
    );

    loadScript("status.js");
    await flushRefresh();

    const parent = document.querySelector("[data-status-last-update]");
    const versionEl = document.querySelector("[data-status-last-update-version]");
    const sepEl = document.querySelector("[data-status-last-update-sep]");
    const relEl = document.querySelector("[data-status-last-update-relative]");

    // CRITICAL: parent must still contain THREE element nodes. The codex-
    // flagged false-positive is asserting only parent.textContent — that
    // value matches "5f12b8b, 5 min ago" for BOTH the shim path and this
    // path, because textContent concatenates children. children.length is
    // what actually proves the shim did not collapse the three spans into
    // one text node.
    expect(parent.children.length).toBe(3);

    // Each child node has its own textContent + visibility state.
    expect(versionEl.textContent).toBe("5f12b8b");
    expect(versionEl.hidden).toBe(false);
    expect(sepEl.hidden).toBe(false);
    expect(relEl.textContent).toBe("5 min ago");

    // Mono styling lives on the version span — the shim writing to the
    // parent would lose this. Pin to catch the regression.
    expect(versionEl.classList.contains("mono")).toBe(true);
  });

  it("empty version: version + sep spans hidden, relative carries 'never'", async () => {
    buildDom({ withChildren: true });
    mock.register(
      /\/api\/status$/,
      {
        status: 200,
        body: baseStatusPayload({
          last_update_version: null,
          last_update_at_relative: "never",
        }),
      }
    );

    loadScript("status.js");
    await flushRefresh();

    const versionEl = document.querySelector("[data-status-last-update-version]");
    const sepEl = document.querySelector("[data-status-last-update-sep]");
    const relEl = document.querySelector("[data-status-last-update-relative]");

    expect(versionEl.hidden).toBe(true);
    expect(sepEl.hidden).toBe(true);
    expect(relEl.textContent).toBe("never");
  });
});
