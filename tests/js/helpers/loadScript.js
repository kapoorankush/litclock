// Test helpers for exercising the IIFE-wrapped scripts in
// src/control_server/static/js/ inside vitest's jsdom environment.
//
// All production scripts use the (function () { 'use strict'; ... })() form
// with no module exports, so the tests drive behavior through the DOM rather
// than by importing internals. `loadScript` evaluates the script in the
// current jsdom global with vm.runInThisContext so stack traces carry the
// real file name. `installFetchMock` provides a URL-pattern-matched fetch
// stand-in since jsdom does not ship fetch.

import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC_JS_DIR = resolve(HERE, "../../../src/control_server/static/js");

/**
 * Evaluate a static JS file in the current jsdom global.
 *
 * The IIFE wires its DOM listeners against whatever document/window are on
 * globalThis at evaluation time, so callers MUST set up the DOM, install fake
 * timers, and register fetch mocks BEFORE calling loadScript. Otherwise the
 * cold-load probes inside updates.js (`pollStatusOnce(handleProbePayload)`)
 * and `refreshCheck()` will fire against the real (missing) fetch and the
 * module's `setInterval(refresh, ...)` from status.js will register against
 * the real clock.
 *
 * @param {string} name — file under src/control_server/static/js/
 */
export function loadScript(name) {
  const src = readFileSync(resolve(STATIC_JS_DIR, name), "utf8");
  vm.runInThisContext(src, { filename: name });
}

/**
 * Install a URL-pattern-matched fetch mock on globalThis.
 *
 * Pattern matching runs against the request URL's pathname (extracted via
 * `new URL(url, 'http://localhost')`) so absolute URLs (e.g. `form.action`
 * resolved against window.location) match the same registration as relative
 * URLs ('/api/update/status'). Unmatched URLs return a rejected Promise —
 * the production fetch().catch(...) sites observe the rejection, and tests
 * see a clear "fetch mock: no entry matches X" message instead of silent
 * undefined that would crash production code downstream with confusing
 * errors. Use .catch() or await to observe the rejection in tests.
 *
 * Response shape: register a `{ status, body }` literal for the common case,
 * or a function `(opts) => Response-like` for per-call control, or a
 * Promise (typically rejected) to simulate network failure exercising the
 * `fetch().catch(...)` branch inside the production code.
 */
export function installFetchMock() {
  const entries = [];
  const calls = [];
  const prev = globalThis.fetch;

  function makeResponse(spec) {
    const status = spec.status ?? 200;
    const ok = status >= 200 && status < 300;
    const body = spec.body;
    return {
      ok,
      status,
      json: async () => body,
      text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
    };
  }

  globalThis.fetch = function fetchMock(url, opts) {
    const absolute = typeof url === "string" ? url : (url && url.url) || String(url);
    const path = new URL(absolute, "http://localhost").pathname;
    calls.push({ url: absolute, path, opts });
    const match = entries.find((e) => e.pattern.test(path));
    if (!match) {
      return Promise.reject(
        new Error(
          `fetch mock: no entry matches ${path} (registered patterns: ${
            entries.map((e) => String(e.pattern)).join(", ") || "<none>"
          })`
        )
      );
    }
    if (typeof match.response === "function") {
      // `new Promise(resolve => resolve(...))` catches synchronous throws
      // from the response function and converts them into a rejected promise
      // — Promise.resolve(fn()) would let the throw escape past the mock and
      // crash the caller instead of exercising the production `.catch()`
      // branch (e.g. pollStatusOnce's network-failure path in updates.js).
      return new Promise((resolve) => resolve(match.response(opts)));
    }
    if (match.response && typeof match.response.then === "function") {
      // Already a Promise (e.g. Promise.reject for network-failure tests).
      return match.response;
    }
    return Promise.resolve(makeResponse(match.response));
  };

  return {
    /**
     * @param {RegExp} pattern — matched against URL pathname.
     * @param {{status?: number, body?: any} | ((opts: any) => any) | Promise<any>} response
     *   - object literal → wrapped into a Response-like ({ ok, status, json, text })
     *   - function → invoked per-call, return Response-like directly
     *   - Promise → returned as-is (use Promise.reject for network failure)
     */
    register(pattern, response) {
      if (!(pattern instanceof RegExp)) {
        throw new Error("installFetchMock.register: pattern must be a RegExp");
      }
      entries.push({ pattern, response });
    },
    /** Drop all registrations + restore the previous fetch (or delete if none). */
    restore() {
      if (prev === undefined) {
        delete globalThis.fetch;
      } else {
        globalThis.fetch = prev;
      }
    },
    /** Array of `{ url, path, opts }` records, one per fetch call. */
    get calls() {
      return calls;
    },
  };
}

/**
 * Stub <dialog>.showModal / .close so the IIFE's `dialogSupported` check
 * (`typeof dialog.showModal === 'function'` at updates.js:44) passes inside
 * jsdom, which historically ships partial <dialog> support. Returns a
 * `restore()` that puts the original prototype back.
 */
export function stubDialog() {
  const proto = globalThis.HTMLDialogElement.prototype;
  const origShowModal = proto.showModal;
  const origClose = proto.close;

  proto.showModal = function () {
    this.open = true;
  };
  proto.close = function (returnValue) {
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.dispatchEvent(new Event("close"));
  };

  return function restore() {
    proto.showModal = origShowModal;
    proto.close = origClose;
  };
}
