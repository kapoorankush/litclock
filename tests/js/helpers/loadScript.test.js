// Self-test for tests/js/helpers/loadScript.js (#338 D6).
//
// Pins the contract that lets every other JS test in the project fail loudly
// when a developer forgets to register a fetch mock: the mock MUST throw with
// a message that names the URL that didn't match. Without this pin a future
// refactor could silently swap the throw for a 404-style stub and every
// future test would crash with a confusing 'cannot read property ok of
// undefined' instead of 'fetch mock: no entry matches /api/foo'.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { installFetchMock } from "./loadScript.js";

describe("installFetchMock", () => {
  let mock;

  beforeEach(() => {
    mock = installFetchMock();
  });

  afterEach(() => {
    mock.restore();
  });

  it("throws with the pathname when no pattern matches", async () => {
    mock.register(/\/api\/known$/, { status: 200, body: { ok: true } });

    await expect(globalThis.fetch("/api/unknown")).rejects.toThrow(
      /fetch mock: no entry matches \/api\/unknown/
    );
  });

  it("matches registered patterns against URL pathname only", async () => {
    mock.register(/\/api\/update\/apply$/, { status: 202, body: { ok: true } });

    // Absolute URL — the production fireApply() resolves form.action against
    // window.location which yields an absolute URL. The mock MUST normalize
    // to pathname so this still matches the relative-style registration.
    const r = await globalThis.fetch("http://litclock.local:8443/api/update/apply", {
      method: "POST",
    });
    expect(r.ok).toBe(true);
    expect(r.status).toBe(202);
  });

  it("returns a Promise.reject response as-is (for network-failure simulation)", async () => {
    const blip = Promise.reject(new TypeError("network"));
    // .catch to suppress unhandled rejection from the registration line.
    blip.catch(() => {});
    mock.register(/\/api\/blip$/, blip);

    await expect(globalThis.fetch("/api/blip")).rejects.toThrow(/network/);
  });

  it("records every call with url, path, and opts", async () => {
    mock.register(/\/api\/echo$/, { status: 200, body: {} });
    await globalThis.fetch("/api/echo", { method: "POST" });
    await globalThis.fetch("http://localhost/api/echo");

    expect(mock.calls).toHaveLength(2);
    expect(mock.calls[0]).toMatchObject({ path: "/api/echo", opts: { method: "POST" } });
    expect(mock.calls[1].path).toBe("/api/echo");
  });
});
