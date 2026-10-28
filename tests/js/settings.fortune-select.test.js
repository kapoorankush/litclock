// Select all / Select none on the fortune database list (litclock-dev#964).
//
// The bar renders `hidden` so a no-JS page never shows two dead buttons;
// settings.js reveals it. "Select all" must leave a mature database alone:
// the pool rule reaches one only when it is ticked by name.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

function buildLabDom() {
  document.body.innerHTML = `
    <form class="settings-form" data-section="lab">
      <details class="settings-details">
        <summary>Fortune databases</summary>
        <div class="settings-fortune-select" data-fortune-select hidden>
          <button type="button" data-fortune-select-all>Select all</button>
          <button type="button" data-fortune-select-none>Select none</button>
        </div>
        <input type="checkbox" name="fortune_category" value="riddles" checked>
        <input type="checkbox" name="fortune_category" value="wisdom">
        <input type="checkbox" name="fortune_category" value="off-rude" data-fortune-mature>
        <input type="checkbox" name="fortune_category" value="gone" data-fortune-missing>
      </details>
    </form>
  `;
}

function ticked() {
  return [...document.querySelectorAll('input[name="fortune_category"]')]
    .filter((b) => b.checked)
    .map((b) => b.value);
}

let mock;

beforeEach(() => {
  mock = installFetchMock();
  mock.register(/^\/api\/status$/, {
    status: 200,
    body: { ok: true, update_state: null, update_phase_index: null },
  });
});

afterEach(() => {
  mock.restore();
  document.body.innerHTML = "";
});

describe("fortune database Select all / Select none", () => {
  it("reveals the bar only once the script runs", () => {
    buildLabDom();
    const bar = document.querySelector("[data-fortune-select]");
    expect(bar.hidden).toBe(true);
    loadScript("settings.js");
    expect(bar.hidden).toBe(false);
  });

  it("Select none unticks every box, mature included", () => {
    buildLabDom();
    document.querySelector('input[value="off-rude"]').checked = true;
    loadScript("settings.js");
    document.querySelector("[data-fortune-select-none]").click();
    expect(ticked()).toEqual([]);
  });

  it("Select all ticks every installed ordinary box, not a mature or uninstalled one", () => {
    buildLabDom();
    loadScript("settings.js");
    document.querySelector("[data-fortune-select-none]").click();
    document.querySelector("[data-fortune-select-all]").click();
    expect(ticked()).toEqual(["riddles", "wisdom"]);
  });

  it("neither button submits the form", () => {
    buildLabDom();
    let submitted = false;
    document.querySelector("form").addEventListener("submit", (e) => {
      submitted = true;
      e.preventDefault();
    });
    loadScript("settings.js");
    document.querySelector("[data-fortune-select-all]").click();
    document.querySelector("[data-fortune-select-none]").click();
    expect(submitted).toBe(false);
  });
});
