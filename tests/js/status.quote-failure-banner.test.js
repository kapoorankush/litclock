// litclock-dev#532 D8/R14 — the Status tab's quote-failure banner. The server
// owns the threshold (`quote_failure`, true from the third consecutive failed
// quote frame); status.js only toggles [hidden] on each poll.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { loadScript, installFetchMock } from "./helpers/loadScript.js";

function payload(extras = {}) {
  return {
    ok: true,
    stale: false,
    picked_at_age_s: 5,
    quote: null,
    author: null,
    title: null,
    time: "12:04",
    wifi_ssid: null,
    weather_city: null,
    version: null,
    uptime_human: "0s",
    last_update_version: null,
    last_update_at_relative: "—",
    phase3_skipped_at_unix: null,
    update_state: null,
    update_phase_index: null,
    consecutive_failures: 0,
    quote_failure: false,
    ...extras,
  };
}

function buildDom({ hidden }) {
  document.body.innerHTML = `
    <div data-status-stale-banner hidden><span data-status-stale-text></span></div>
    <div data-status-quote-failure-banner${hidden ? " hidden" : ""}>
      <span>Your clock hasn’t been able to show a book quote for the last few minutes.</span>
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
      <p data-status-hero-fallback hidden><em data-status-hero-fallback-text></em></p>
    </section>
  `;
}

async function flushRefresh() {
  await new Promise((r) => setTimeout(r, 60));
  for (let i = 0; i < 4; i++) {
    await Promise.resolve();
  }
}

describe("status.js — quote-failure banner (litclock-dev#532 D8)", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
  });

  it("shows the banner when the server says quote_failure", async () => {
    buildDom({ hidden: true });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: payload({ consecutive_failures: 3, quote_failure: true }),
    });
    loadScript("status.js");
    await flushRefresh();
    expect(document.querySelector("[data-status-quote-failure-banner]").hidden).toBe(false);
  });

  it("hides it again once a quote frame resets the run", async () => {
    buildDom({ hidden: false });
    mock.register(/\/api\/status$/, { status: 200, body: payload() });
    loadScript("status.js");
    await flushRefresh();
    expect(document.querySelector("[data-status-quote-failure-banner]").hidden).toBe(true);
  });

  it("trusts the server's threshold, not the raw count", async () => {
    buildDom({ hidden: true });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: payload({ consecutive_failures: 7, quote_failure: false }),
    });
    loadScript("status.js");
    await flushRefresh();
    expect(document.querySelector("[data-status-quote-failure-banner]").hidden).toBe(true);
  });

  it("shows the panel's own line in the hero on a plain-time frame, not 'Starting up'", async () => {
    buildDom({ hidden: true });
    mock.register(/\/api\/status$/, {
      status: 200,
      body: payload({ quote: "", fallback_reason: "failure" }),
    });
    loadScript("status.js");
    await flushRefresh();
    expect(document.querySelector("[data-status-hero-fallback]").hidden).toBe(false);
    expect(document.querySelector("[data-status-hero-empty]").hidden).toBe(true);
    expect(document.querySelector("[data-status-hero-fallback-text]").textContent).toBe(
      "Book quotes are unavailable right now.",
    );
  });

  it("keeps 'Starting up' when nothing says why there is no quote", async () => {
    buildDom({ hidden: true });
    mock.register(/\/api\/status$/, { status: 200, body: payload({ quote: "", fallback_reason: null }) });
    loadScript("status.js");
    await flushRefresh();
    expect(document.querySelector("[data-status-hero-fallback]").hidden).toBe(true);
    expect(document.querySelector("[data-status-hero-empty]").hidden).toBe(false);
  });
});
