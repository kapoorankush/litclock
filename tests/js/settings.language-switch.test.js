// litclock-dev#532 RT5 (design D11, eng R17, Codex O2): what a language
// save does after it commits.
//
// 1. The service worker caches pages per language, so the reload waits for
//    the NEW worker (registration.update() -> controllerchange), at once when
//    nothing new is installing, and after 10s whatever happens.
// 2. The reloaded page — rendered in the new language — shows a one-shot
//    "switching now" toast (the save has already repainted the panel, R17),
//    flagged through sessionStorage and cleared on display.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFetchMock, loadScript } from "./helpers/loadScript.js";

function buildLanguageDom({ current = "en" } = {}) {
  const sel = (code) => (current === code ? "is-selected" : "");
  const chk = (code) => (current === code ? "checked" : "");
  document.body.innerHTML = `
    <form class="settings-form" data-section="language">
      <input type="hidden" name="csrf_token" value="render-token">
      <input type="hidden" name="section" value="language">
      <fieldset class="settings-row settings-row--pill">
        <div class="settings-segmented" role="radiogroup" aria-label="Device language" data-language-pill>
          <label class="settings-segmented__opt ${sel("en")}" data-language-opt="en">
            <input type="radio" class="settings-segmented__input" name="LITCLOCK_LANGUAGE" value="en" ${chk("en")}>
            <span>English</span>
          </label>
          <label class="settings-segmented__opt ${sel("es")}" data-language-opt="es">
            <input type="radio" class="settings-segmented__input" name="LITCLOCK_LANGUAGE" value="es" ${chk("es")}>
            <span>Español</span>
          </label>
        </div>
      </fieldset>
      <div class="settings-form__actions" data-no-js-only>
        <button type="submit" class="settings-button settings-button--primary">Save</button>
      </div>
    </form>
  `;
}

async function flush() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function clickOpt(code) {
  const radio = document.querySelector(`[data-language-opt="${code}"] .settings-segmented__input`);
  radio.checked = true;
  radio.dispatchEvent(new Event("change", { bubbles: true }));
}

const realLocation = window.location;
const flag = (code, extra = {}) => JSON.stringify({ code, at: Date.now(), ...extra });
const readFlag = () => JSON.parse(window.sessionStorage.getItem("litclock.languageSwitched"));
const realSW = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");
let mock;
let reloadSpy;

function fakeServiceWorker({ installs = true } = {}) {
  const listeners = {};
  const reg = {
    installing: null,
    waiting: null,
    update: vi.fn(async () => {
      if (installs) reg.installing = {};
      return reg;
    }),
  };
  const sw = {
    controller: {},
    addEventListener: (type, fn) => {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    getRegistration: vi.fn(async () => reg),
    fire: (type) => (listeners[type] || []).forEach((fn) => fn()),
    reg,
  };
  Object.defineProperty(navigator, "serviceWorker", { value: sw, configurable: true });
  return sw;
}

beforeEach(() => {
  vi.useFakeTimers();
  mock = installFetchMock();
  mock.register(/^\/api\/csrf$/, { status: 200, body: { ok: true, csrf_token: "fresh-token" } });
  mock.register(/^\/api\/status$/, { status: 200, body: { ok: true, update_state: null } });
  reloadSpy = vi.fn();
  delete window.location;
  window.location = { ...realLocation, reload: reloadSpy };
  window.sessionStorage.clear();
});

afterEach(() => {
  mock.restore();
  vi.useRealTimers();
  document.body.innerHTML = "";
  delete window.location;
  window.location = realLocation;
  if (realSW) Object.defineProperty(navigator, "serviceWorker", realSW);
  else delete navigator.serviceWorker;
  window.sessionStorage.clear();
});

async function saveSpanish() {
  buildLanguageDom({ current: "en" });
  mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
  loadScript("settings.js");
  clickOpt("es");
  await flush();
}

describe("after a language save (RT5)", () => {
  it("waits for the new service worker to take control, then reloads", async () => {
    const sw = fakeServiceWorker({ installs: true });
    await saveSpanish();
    expect(sw.reg.update).toHaveBeenCalledTimes(1);
    expect(reloadSpy).not.toHaveBeenCalled();
    sw.fire("controllerchange");
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10000);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("reloads at once when no new worker is installing", async () => {
    const sw = fakeServiceWorker({ installs: false });
    await saveSpanish();
    expect(sw.reg.update).toHaveBeenCalledTimes(1);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("reloads after 10s when the new worker never takes control", async () => {
    fakeServiceWorker({ installs: true });
    await saveSpanish();
    vi.advanceTimersByTime(9999);
    expect(reloadSpy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("reloads at once without a service worker (iOS)", async () => {
    Object.defineProperty(navigator, "serviceWorker", { value: undefined, configurable: true });
    await saveSpanish();
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("leaves the one-shot flag, naming the language switched to", async () => {
    Object.defineProperty(navigator, "serviceWorker", { value: undefined, configurable: true });
    await saveSpanish();
    expect(readFlag().code).toBe("es");
  });

  it("an uncontrolled page (no active worker yet) reloads at once", async () => {
    const sw = fakeServiceWorker({ installs: true });
    sw.controller = null;
    await saveSpanish();
    expect(sw.reg.update).not.toHaveBeenCalled();
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("a failed save leaves no flag and does not reload", async () => {
    buildLanguageDom({ current: "en" });
    window.alert = vi.fn();
    mock.register(/^\/api\/settings$/, { status: 422, body: { ok: false } });
    loadScript("settings.js");
    clickOpt("es");
    await flush();
    expect(window.sessionStorage.getItem("litclock.languageSwitched")).toBeNull();
    expect(reloadSpy).not.toHaveBeenCalled();
  });
});

describe("the reloaded page (RT5)", () => {
  function reloadedInSpanish(current = "es") {
    buildLanguageDom({ current });
    const region = document.createElement("div");
    region.setAttribute("data-toast-region", "");
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
    const blob = document.createElement("script");
    blob.type = "application/json";
    blob.setAttribute("data-litclock-strings", "");
    blob.textContent = JSON.stringify({ "settings.language.switching": "El reloj cambia a {language} ahora." });
    document.body.appendChild(blob);
  }

  it("says the clock is switching now, in the new language, once", () => {
    reloadedInSpanish();
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es"));
    loadScript("settings.js");
    const toast = document.querySelector("[data-toast]");
    expect(toast.textContent).toBe("El reloj cambia a Español ahora.");
    // Inside the page's persistent live region, which was there first.
    expect(toast.parentElement.getAttribute("aria-live")).toBe("polite");
    expect(window.sessionStorage.getItem("litclock.languageSwitched")).toBeNull();
    vi.advanceTimersByTime(3000 + 200);
    expect(document.querySelector("[data-toast]")).toBeNull();
  });

  it("a tap dismisses it", () => {
    reloadedInSpanish();
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es"));
    loadScript("settings.js");
    document.querySelector("[data-toast]").click();
    vi.advanceTimersByTime(200);
    expect(document.querySelector("[data-toast]")).toBeNull();
  });

  it("an ordinary load shows nothing", () => {
    reloadedInSpanish();
    loadScript("settings.js");
    expect(document.querySelector("[data-toast]")).toBeNull();
  });

  it("a page still in the OLD language reloads once instead of announcing it", () => {
    reloadedInSpanish("en"); // the old worker served its cached English page
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es"));
    loadScript("settings.js");
    expect(document.querySelector("[data-toast]")).toBeNull();
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    expect(readFlag()).toMatchObject({ code: "es", retried: true });
  });

  it("a second miss drops the note rather than looping", () => {
    reloadedInSpanish("en");
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es", { retried: true }));
    loadScript("settings.js");
    expect(document.querySelector("[data-toast]")).toBeNull();
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem("litclock.languageSwitched")).toBeNull();
  });

  it("a retried page that IS in the new language shows the note", () => {
    reloadedInSpanish();
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es", { retried: true }));
    loadScript("settings.js");
    expect(document.querySelector("[data-toast]").textContent).toBe("El reloj cambia a Español ahora.");
  });

  it("a language name with markup stays text", () => {
    reloadedInSpanish();
    document.querySelector('[data-language-opt="es"] span').textContent = "<img src=x onerror=alert(1)>";
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es"));
    loadScript("settings.js");
    expect(document.querySelector("[data-toast] img")).toBeNull();
    expect(document.querySelector("[data-toast]").textContent).toContain("<img src=x");
  });

  it("only one toast at a time", () => {
    reloadedInSpanish();
    const stale = document.createElement("div");
    stale.setAttribute("data-toast", "");
    document.querySelector("[data-toast-region]").appendChild(stale);
    window.sessionStorage.setItem("litclock.languageSwitched", flag("es"));
    loadScript("settings.js");
    expect(document.querySelectorAll("[data-toast]")).toHaveLength(1);
  });

  it("a stale flag from a reload that never happened is ignored", () => {
    reloadedInSpanish("en");
    window.sessionStorage.setItem("litclock.languageSwitched", JSON.stringify({ code: "es", at: Date.now() - 31000 }));
    loadScript("settings.js");
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(document.querySelector("[data-toast]")).toBeNull();
    expect(window.sessionStorage.getItem("litclock.languageSwitched")).toBeNull();
  });
});

describe("keyboard navigation through the language rows (DT7 review)", () => {
  function threeLanguages() {
    document.body.innerHTML = `
      <form class="settings-form" data-section="language">
        <input type="hidden" name="csrf_token" value="t">
        <div class="settings-segmented settings-segmented--rows" role="radiogroup" data-language-pill>
          ${["en", "es", "fr"]
            .map(
              (c) => `<label class="settings-segmented__opt ${c === "en" ? "is-selected" : ""}" data-language-opt="${c}">
                <input type="radio" class="settings-segmented__input" name="LITCLOCK_LANGUAGE" value="${c}" ${c === "en" ? "checked" : ""}>
                <span>${c}</span></label>`
            )
            .join("")}
        </div>
        <button type="button" id="after">after</button>
      </form>`;
  }
  function arrowTo(code) {
    const pill = document.querySelector("[data-language-pill]");
    pill.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    clickOpt(code);
  }
  function saves() {
    return mock.calls.filter((c) => c.path === "/api/settings").map((c) => JSON.parse(c.opts.body).LITCLOCK_LANGUAGE);
  }

  beforeEach(() => {
    Object.defineProperty(navigator, "serviceWorker", { value: undefined, configurable: true });
    mock.register(/^\/api\/settings$/, { status: 200, body: { ok: true } });
  });

  it("arrowing past a language saves only the one the reader stops on", async () => {
    threeLanguages();
    loadScript("settings.js");
    arrowTo("es");
    await flush();
    arrowTo("fr");
    await flush();
    expect(saves()).toEqual([]);
    vi.advanceTimersByTime(1000);
    await flush();
    expect(saves()).toEqual(["fr"]);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("leaving the group commits at once", async () => {
    threeLanguages();
    loadScript("settings.js");
    arrowTo("es");
    const pill = document.querySelector("[data-language-pill]");
    pill.dispatchEvent(new FocusEvent("focusout", { relatedTarget: document.getElementById("after") }));
    await flush();
    expect(saves()).toEqual(["es"]);
  });

  it("a click still commits at once", async () => {
    threeLanguages();
    loadScript("settings.js");
    clickOpt("fr");
    await flush();
    expect(saves()).toEqual(["fr"]);
  });
});
