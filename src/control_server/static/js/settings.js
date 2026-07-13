/* Settings tab — progressive enhancement.
 *
 * Without this script the no-JS form POST works fine (PRG redirect on
 * success, re-render with field errors on failure). With it, the Weather
 * section's city/zip input fires a /api/geocode preview on blur so the
 * user sees the resolved location BEFORE tapping Save.
 *
 * Locked decision M3-D3: same geocoding stack on both /api/geocode (preview)
 * and /api/settings (save) so the preview can never disagree with what
 * actually gets written.
 */
(function () {
  "use strict";

  // #317 item 7 — Prepare-for-Gifting moved to /system. The textarea→hidden
  // mirror that lived here is now in system.js, scoped to the System tab.

  // #274 follow-up #2 — Update-in-progress banner. Polls /api/status every
  // 15s when the Settings tab is visible and toggles a hidden banner when
  // update.sh is mid-flight in Phase 3 or 4. Phase 3 is when the env.sh
  // sidecar flock is actually held; Phase 4 is the long-running pip-install
  // where users are most likely to be staring at the page anyway. Other
  // phases are too short to be worth surfacing.
  //
  // Wired BEFORE the weather-preview early-return guard below so this code
  // path is independent — a missing weather form (e.g., minimal test DOM,
  // or a future template variant) must not gate the banner.
  //
  // Architecture note: settings.js doesn't otherwise poll. Adding a
  // dedicated 15s poll for this is cheaper than coordinating with
  // status.js (which only loads on the Status tab). Burden is one GET
  // per 15s while the user is on /settings — trivial relative to the
  // 30s poll on /status. visibilitychange gates it so a backgrounded
  // tab doesn't keep pulling.
  (function wireUpdateInProgressBanner() {
    var SETTINGS_UPDATE_BANNER_POLL_MS = 15000;
    var SETTINGS_UPDATE_BANNER_PHASES = { 3: true, 4: true };
    var banner = document.querySelector("[data-settings-update-banner]");
    if (!banner) return;

    var pollHandle = null;
    // Adversarial-review P2 — AbortController for the in-flight poll so
    // stop() can cancel the pending fetch instead of letting a late
    // response land on a now-hidden tab and toggle the banner against
    // stale data. Mirrors the pattern at settings.js's save flow + the
    // status.js poll's abort handling.
    var inFlight = null;

    var setBannerVisible = function (visible) {
      if (visible) {
        banner.removeAttribute("hidden");
      } else {
        banner.setAttribute("hidden", "");
      }
    };

    var checkStatus = function () {
      if (inFlight) return;
      var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      inFlight = controller;
      var fetchOpts = { headers: { Accept: "application/json" } };
      if (controller) {
        fetchOpts.signal = controller.signal;
      }
      try {
        fetch("/api/status", fetchOpts)
          .then(function (resp) {
            return resp.ok ? resp.json() : null;
          })
          .then(function (data) {
            // Only honor the response if the controller we kicked off
            // with is still the active one. stop() nulls inFlight on
            // abort, so a stale response from a backgrounded tab can't
            // toggle the banner.
            if (controller && inFlight !== controller) return;
            if (!data) return;
            var running = data.update_state === "running";
            var phase = data.update_phase_index;
            var show = running && SETTINGS_UPDATE_BANNER_PHASES[phase] === true;
            setBannerVisible(show);
          })
          .catch(function () {
            // Silent on network blip / AbortError — keep current visibility state.
          })
          .finally(function () {
            if (inFlight === controller) {
              inFlight = null;
            }
          });
      } catch (_e) {
        if (inFlight === controller) {
          inFlight = null;
        }
      }
    };

    var start = function () {
      if (pollHandle !== null) return;
      checkStatus();
      pollHandle = setInterval(checkStatus, SETTINGS_UPDATE_BANNER_POLL_MS);
    };
    var stop = function () {
      if (pollHandle !== null) {
        clearInterval(pollHandle);
        pollHandle = null;
      }
      // Abort any in-flight poll so its late response doesn't land on
      // a now-hidden tab. Catches the case where the user backgrounds
      // the tab mid-fetch.
      if (inFlight && typeof inFlight.abort === "function") {
        try {
          inFlight.abort();
        } catch (_e) {
          /* AbortController may throw if already aborted; ignore. */
        }
      }
      inFlight = null;
    };

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        stop();
      } else {
        start();
      }
    });
    if (document.visibilityState !== "hidden") {
      start();
    }
  })();

  // #337 design-review: mark <html> as JS-enabled so the no-JS Save buttons
  // hide via the [data-no-js-only] CSS rule. Without this, JS users would
  // see redundant Save buttons next to the auto-save Weather toggle and
  // Temperature pill.
  document.documentElement.classList.add("has-js");

  // ─── #414 item #3 / #458: shared auto-save POST helper ────────────────
  // All three auto-save Settings controls — the Weather toggle (#346), the
  // Temperature pill (#337 A13), and the Advanced toggles (#456) — PATCH
  // /api/settings with the same scaffolding: refresh CSRF, POST JSON,
  // surface 504 env_lock_timeout's structured message, throw on failure.
  // Pre-#414 the block was duplicated ~70 lines per call site; #458
  // converged the remaining hand-rolled call sites onto the two wiring
  // helpers below (wireBooleanToggleAutoSave / wireSegmentedAutoSave) so
  // there's a single code path to fix.
  //
  // #457 — the wiring helpers SERIALIZE saves per control (at most one
  // request in flight, rapid taps coalesced to the latest desired value)
  // rather than the old AbortController "abort the in-flight fetch on
  // re-tap" strategy. Aborting only cancels the client's read of the
  // response; a POST that already reached the Pi still completes its
  // env.sh write, and because writes serialize on the flock by acquisition
  // order (not send order) an aborted older save could land AFTER a newer
  // one and persist state opposite the UI. Never aborting + one-in-flight
  // removes the concurrency that caused that reorder, so this helper no
  // longer takes a signal.
  //
  // On success: resolves to undefined. On non-2xx: throws an Error with
  // ``.timeoutMsg`` populated when the response was a 504 + env_lock_timeout
  // envelope (caller surfaces that message instead of the generic copy).
  async function autoSavePatch({ section, fields }) {
    const csrfResp = await fetch("/api/csrf", {
      headers: { Accept: "application/json" },
    });
    const csrfBody = await csrfResp.json();
    const csrfToken = csrfBody && csrfBody.ok ? csrfBody.csrf_token : null;
    if (!csrfToken) throw new Error("csrf token unavailable");
    // #415 /review (maintainability): merge `fields` FIRST, then overlay the
    // safety-critical keys (section, csrf_token). Without this ordering, a
    // future caller that forwards a raw form-data object containing a
    // `section` or `csrf_token` key would silently overwrite the helper's
    // own values — a footgun on a security-critical request shape. Putting
    // {section, csrf_token} last makes the helper's invariant the authority.
    const payload = Object.assign({}, fields, { section: section, csrf_token: csrfToken });
    const resp = await fetch("/api/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      // #274 follow-up #4 — on HTTP 504 with env_lock_timeout, surface
      // the server's structured message so the user understands the
      // Save was rejected because another writer is mid-flight (and
      // a retry will work once the lock releases). Other failure
      // statuses fall through to the generic alert at the call site.
      let timeoutMsg = null;
      if (resp.status === 504) {
        try {
          const errBody = await resp.json();
          if (errBody && errBody.error && errBody.error.code === "env_lock_timeout") {
            timeoutMsg = errBody.error.message;
          }
        } catch (_e) {
          /* malformed envelope — fall through to generic alert */
        }
      }
      const err = new Error("save failed (HTTP " + resp.status + ")");
      err.timeoutMsg = timeoutMsg;
      throw err;
    }
  }

  const queryInput = document.getElementById("location_query");
  const currentSpan = document.querySelector("[data-current-location]");

  // Blur preview (#337 A14). Composite cache key (q + "|" + worldwide) so
  // toggling the worldwide checkbox without changing text invalidates the
  // cache and re-fires the preview. Stale-on-typing dimming handled below.
  let lastQuery = "";
  const worldwideInput = document.getElementById("location_worldwide");
  async function preview() {
    if (!queryInput || !currentSpan) return;
    const q = queryInput.value.trim();
    const ww = worldwideInput && worldwideInput.checked ? "1" : "0";
    const cacheKey = q + "|" + ww;
    if (!q || cacheKey === lastQuery) return;
    lastQuery = cacheKey;
    try {
      const url =
        "/api/geocode?q=" +
        encodeURIComponent(q) +
        (ww === "1" ? "&worldwide=1" : "");
      const resp = await fetch(url, { headers: { Accept: "application/json" } });
      const body = await resp.json();
      if (body && body.ok) {
        currentSpan.textContent = body.short_name || body.display_name || q;
        currentSpan.classList.remove("is-stale");
      } else {
        currentSpan.textContent = "Couldn't find that location.";
        currentSpan.classList.remove("is-stale");
      }
    } catch (_e) {
      // Network failure — silently keep the previous value. No-JS users
      // will hit the same error on Save and see it surfaced via the
      // re-rendered field-error banner.
    }
  }

  if (queryInput) {
    queryInput.addEventListener("blur", preview);
    // #337 A14: dim the Currently sublabel on any keystroke after the last
    // preview, signalling that the displayed value is stale. The next blur
    // (or successful Save) restores it. Cheap visual cue that prevents the
    // false-confidence trap of "Currently says X but I just typed Y."
    queryInput.addEventListener("input", function () {
      if (!currentSpan) return;
      currentSpan.classList.add("is-stale");
    });
  }
  if (worldwideInput) {
    // #337 A14: re-fire blur preview when the user flips the worldwide
    // checkbox if the Place input has content. Without this the composite
    // cache key would invalidate but the preview wouldn't actually run
    // until the next blur — leaving the user staring at a "Couldn't find"
    // from the previous (IP-country-biased) attempt.
    worldwideInput.addEventListener("change", function () {
      if (queryInput && queryInput.value.trim()) {
        preview();
      }
    });
  }
  // Live aria-checked sync on toggles (CSS handles the visual state, but
  // assistive tech needs the attribute updated when the checkbox flips).
  const toggles = document.querySelectorAll('input.settings-toggle[role="switch"]');
  toggles.forEach(function (t) {
    t.addEventListener("change", function () {
      t.setAttribute("aria-checked", t.checked ? "true" : "false");
    });
  });

  // ─── #337 A12: Location mode pill (Automatic | Specific) ─────────────
  // Click handler on the segmented pill labels. The radio inputs under
  // each label provide the actual form-submit semantics + a11y; this just
  // syncs the .is-selected visual class and shows/hides the per-mode
  // panels. A17: switching to Automatic clears the Advanced lat/lon
  // inputs visually (transient form-state only — no server write).
  const modePill = document.querySelector("[data-mode-pill]");
  if (modePill) {
    const modeOpts = modePill.querySelectorAll(".settings-segmented__opt");
    const autoPanel = document.querySelector('[data-mode-panel="auto"]');
    const specificPanel = document.querySelector('[data-mode-panel="specific"]');
    const advancedLat = document.querySelector("[data-advanced-lat]");
    const advancedLon = document.querySelector("[data-advanced-lon]");
    const locationSaveBtn = document.querySelector("[data-location-save]");

    function updateSaveDisabled() {
      // #337 A10 + A14: Save disabled when MODE=specific AND Place is empty
      // (treat raw-coords Advanced as filling Specific too — if either lat
      // or lon has text, the form is non-empty for Specific purposes).
      if (!locationSaveBtn) return;
      const selected = modePill.querySelector(".settings-segmented__opt.is-selected");
      const mode = selected && selected.getAttribute("data-mode-opt");
      if (mode !== "specific") {
        locationSaveBtn.disabled = false;
        locationSaveBtn.removeAttribute("aria-disabled");
        locationSaveBtn.removeAttribute("title");
        return;
      }
      const placeFilled = queryInput && queryInput.value.trim() !== "";
      const advFilled =
        (advancedLat && advancedLat.value.trim() !== "") ||
        (advancedLon && advancedLon.value.trim() !== "");
      if (placeFilled || advFilled) {
        locationSaveBtn.disabled = false;
        locationSaveBtn.removeAttribute("aria-disabled");
        locationSaveBtn.removeAttribute("title");
      } else {
        locationSaveBtn.disabled = true;
        locationSaveBtn.setAttribute("aria-disabled", "true");
        locationSaveBtn.setAttribute("title", "Type a place or pick Automatic");
      }
    }

    modeOpts.forEach(function (opt) {
      const radio = opt.querySelector(".settings-segmented__input");
      function activate() {
        if (radio && !radio.checked) {
          radio.checked = true;
        }
        modeOpts.forEach(function (o) {
          o.classList.toggle("is-selected", o === opt);
          const r = o.querySelector(".settings-segmented__input");
          if (r) r.setAttribute("aria-selected", o === opt ? "true" : "false");
        });
        const mode = opt.getAttribute("data-mode-opt");
        if (autoPanel) autoPanel.hidden = mode !== "auto";
        if (specificPanel) {
          specificPanel.hidden = mode !== "specific";
          // #337 /review P0 (Codex): toggle the `disabled` attribute on the
          // <fieldset> alongside `hidden`. Hidden alone doesn't prevent
          // form submission — the inner Place/Advanced inputs would still
          // post as empty strings, triggering the all-or-none guard and
          // 422-ing the Auto save with an existing Specific city.
          // <fieldset disabled> excludes all inner controls from submission.
          specificPanel.disabled = mode !== "specific";
        }
        if (mode === "auto") {
          // #337 A17: switching to Automatic clears the Advanced lat/lon
          // inputs visually. Transient form-state only — no env write
          // until Save. Restores empty inputs if the user toggles back.
          if (advancedLat) advancedLat.value = "";
          if (advancedLon) advancedLon.value = "";
        }
        updateSaveDisabled();
      }
      opt.addEventListener("click", function (event) {
        // Let the inner radio fire its own change event naturally — only
        // intercept when the click landed on the label/span, not on the
        // radio itself (which would double-trigger).
        if (event.target !== radio) activate();
      });
      if (radio) {
        radio.addEventListener("change", activate);
      }
    });

    if (queryInput) queryInput.addEventListener("input", updateSaveDisabled);
    if (advancedLat) advancedLat.addEventListener("input", updateSaveDisabled);
    if (advancedLon) advancedLon.addEventListener("input", updateSaveDisabled);
    updateSaveDisabled();
  }

  // #337 A13 Temperature pill auto-save + #346 Weather toggle auto-save are
  // both wired below via the shared serialized helpers (#458 convergence).

  // ─── #337 A18: Browser-timezone fallback button ──────────────────────
  // Shows the row + populates the detected tz label when:
  //   (a) the row exists in the DOM (server renders it only when no coords)
  //   (b) Intl.DateTimeFormat is available (effectively all phones 2017+)
  // Tapping POSTs the detected tz to /api/handoff/set-timezone — same
  // endpoint #388 uses during the first-boot handoff splash.
  const tzRow = document.querySelector("[data-tz-fallback]");
  if (tzRow) {
    let detectedTz = null;
    try {
      if (typeof Intl !== "undefined" && typeof Intl.DateTimeFormat === "function") {
        const opts = Intl.DateTimeFormat().resolvedOptions();
        if (opts && typeof opts.timeZone === "string" && opts.timeZone) {
          detectedTz = opts.timeZone;
        }
      }
    } catch (_e) {
      // Intl unavailable or threw — leave detectedTz null and keep row hidden.
    }
    if (detectedTz) {
      const label = tzRow.querySelector("[data-browser-tz-label]");
      if (label) label.textContent = detectedTz;
      tzRow.hidden = false;
      const btn = tzRow.querySelector("[data-browser-tz-btn]");
      if (btn) {
        btn.addEventListener("click", async function () {
          btn.disabled = true;
          try {
            const csrfResp = await fetch("/api/csrf", {
              headers: { Accept: "application/json" },
            });
            const csrfBody = await csrfResp.json();
            const csrfToken = csrfBody && csrfBody.ok ? csrfBody.csrf_token : null;
            if (!csrfToken) throw new Error("csrf token unavailable");
            // #337 /review — POST to /api/system/set-timezone (CSRF-guarded,
            // always-on). The earlier draft posted to /api/handoff/set-timezone
            // which is gated on is_handoff_active and silently no-ops post-
            // handoff — fake success that left the tz unchanged. The new
            // endpoint always sets the tz and rejects bad values with 422.
            const resp = await fetch("/api/system/set-timezone", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify({ timezone: detectedTz, csrf_token: csrfToken }),
            });
            if (resp.ok) {
              // Reload so the user sees the new state (Location section may
              // hide this row + re-enable normal behavior).
              window.location.reload();
            } else {
              btn.disabled = false;
              const msg = "Couldn't set timezone (HTTP " + resp.status + "). Try again.";
              if (typeof window.alert === "function") window.alert(msg);
            }
          } catch (e) {
            btn.disabled = false;
            const msg = "Couldn't set timezone. Check your network and try again.";
            if (typeof window.alert === "function") window.alert(msg);
          }
        });
      }
    }
    // If Intl unavailable, leave the row hidden — graceful degradation. The
    // user can still use Specific + Advanced raw coords if they know coords
    // for any place in their tz.
  }


  // ─── #456 / #458: shared auto-save wiring helpers ─────────────────────
  // DESIGN.md "Save-button rule" (#337 A13): discrete controls (boolean
  // toggles + segmented pills) auto-save on change with no Save button (the
  // no-JS fallback Save is CSS-hidden under html.has-js). Two helpers cover
  // every auto-save control on the Settings tab:
  //   • wireBooleanToggleAutoSave — checkbox toggles (Weather "Show on
  //     display" #346; Advanced NSFW + diagnostics-shortcut #456)
  //   • wireSegmentedAutoSave     — segmented radio pills (Temperature #337 A13)
  // #458 converged the previously hand-rolled Weather-toggle + Temperature-
  // pill bodies onto these so there's a single code path to fix.
  //
  // Each save is a JSON PATCH of ONLY the control's own key. That's
  // load-bearing: a section may render sibling controls in one form, and the
  // JSON PATCH path (routes/settings.py) writes only keys present in the body
  // and never synthesises a missing boolean to "false" (that synthesis is
  // form-path-only) — so saving one control can't silently flip a sibling.
  //
  // #457 — SERIALIZED, COALESCING saves (replaces the old AbortController
  // "abort the in-flight fetch on re-tap" strategy). Each helper keeps at
  // most ONE save in flight per control. While a save is in flight, further
  // taps only update `desired`; when the in-flight save settles, if `desired`
  // no longer matches the last server-CONFIRMED value the helper fires
  // exactly one more save carrying the latest `desired`. Two consequences:
  //   1. The helper never issues two overlapping requests for one control, so
  //      the flock can't apply an older save after a newer one — persisted
  //      state always converges to the value the user landed on (the #457
  //      bug). A burst of N taps costs at most 2 round-trips.
  //   2. There is no AbortError path anymore (we never abort). On a real
  //      failure the helper drops any queued intent, reverts the visual to
  //      the last server-confirmed value (NOT `!newValue`: under coalescing
  //      an earlier save in the same burst may already have moved what's
  //      persisted, so `!newValue` could show a state the server never held),
  //      and alerts.
  //
  // Both helpers share the IDENTICAL serialization contract (`confirmed` /
  // `desired` / `saving` + the recursive `pump()` + revert-on-failure). They're
  // kept as two functions only because checkbox state (a boolean) and segmented
  // state (which opt element) read/apply differently — if you fix a bug in one
  // pump(), fix the same bug in the other.
  //
  // Known residual divergences (all self-heal on the next page load, which
  // re-seeds every control from env.sh — same "heals on reload" class as the
  // original #457 bug; full convergence would need server read-back, the
  // reconcile alternative #457 weighed and set aside):
  //   • Multi-client: two *different* devices editing the same control at the
  //     same instant is still last-flock-wins — inherent to a shared LAN device.
  //   • Lost response after commit: if a save commits server-side but its
  //     response is dropped, the helper can't know it landed, so it reverts the
  //     visual while env.sh holds the new value. (The old abort path had the
  //     same exposure.)
  //   • Stalled request: `saving` stays true until the in-flight save settles,
  //     so taps during a stall only update `desired` and aren't sent until it
  //     resolves. /api/settings is server-bounded (~30s flock wait → 504) and
  //     /api/csrf is in-memory, so this is bounded, not an indefinite wedge.
  //
  // Keeping the control ENABLED in flight is load-bearing (codex F2, #346): a
  // disabled control is dropped from native form submission. The Advanced
  // section renders its two toggles in ONE form, so disabling a toggle mid-save
  // would drop its key from a no-JS fallback submit and the form path would
  // synthesise the missing boolean to "false" — silently flipping it. These
  // helpers never disable the control.

  function wireBooleanToggleAutoSave(toggle, section, key, failMsg) {
    if (!toggle) return;
    let confirmed = toggle.checked; // last server-confirmed value (SSR seed).
    let desired = toggle.checked; // latest user-intended value.
    let saving = false; // a save round-trip is in flight.

    function revertVisual() {
      // Assigning .checked doesn't fire `change`, so sync aria-checked too
      // (no re-trigger of the aria-sync listener wired earlier).
      toggle.checked = confirmed;
      toggle.setAttribute("aria-checked", confirmed ? "true" : "false");
    }

    async function pump() {
      // One in-flight save per control; nothing to do if already at target.
      if (saving || desired === confirmed) return;
      saving = true;
      const target = desired;
      const fields = {};
      fields[key] = target;
      try {
        await autoSavePatch({ section: section, fields: fields });
        confirmed = target;
        saving = false;
        // A tap may have landed while this save was in flight — if `desired`
        // moved on, fire exactly one more (the final, coalesced) save.
        pump();
      } catch (e) {
        saving = false;
        // Real failure (no abort path). Abandon any queued intent and revert
        // to the last value the server actually confirmed.
        desired = confirmed;
        revertVisual();
        if (typeof window !== "undefined" && typeof window.alert === "function") {
          window.alert((e && e.timeoutMsg) || failMsg);
        }
      }
    }

    toggle.addEventListener("change", function () {
      desired = toggle.checked;
      pump();
    });
  }

  function wireSegmentedAutoSave(pill, section, key, failMsg) {
    if (!pill) return;
    const opts = pill.querySelectorAll(".settings-segmented__opt");
    if (!opts.length) return;
    // Seed `confirmed` from the server-rendered selected opt (SSR seed).
    let confirmedOpt = null;
    opts.forEach(function (o) {
      if (o.classList.contains("is-selected")) confirmedOpt = o;
    });
    let desiredOpt = confirmedOpt;
    let saving = false;

    function selectVisual(target) {
      opts.forEach(function (o) {
        o.classList.toggle("is-selected", o === target);
        const r = o.querySelector(".settings-segmented__input");
        // Keep the radio coherent with the visual — matters on revert, where
        // assigning .checked programmatically doesn't fire `change` (no
        // re-entrancy into the handler below).
        if (r) r.checked = o === target;
      });
    }

    async function pump() {
      if (saving || desiredOpt === confirmedOpt) return;
      const target = desiredOpt;
      const radio = target && target.querySelector(".settings-segmented__input");
      const value = radio ? radio.value : null;
      if (value === null) return;
      saving = true;
      const fields = {};
      fields[key] = value;
      try {
        await autoSavePatch({ section: section, fields: fields });
        confirmedOpt = target;
        saving = false;
        pump();
      } catch (e) {
        saving = false;
        desiredOpt = confirmedOpt;
        selectVisual(confirmedOpt);
        if (typeof window !== "undefined" && typeof window.alert === "function") {
          window.alert((e && e.timeoutMsg) || failMsg);
        }
      }
    }

    opts.forEach(function (opt) {
      const radio = opt.querySelector(".settings-segmented__input");
      if (!radio) return;
      radio.addEventListener("change", function () {
        if (!radio.checked) return;
        selectVisual(opt); // optimistic — the radio click IS the commit.
        desiredOpt = opt;
        pump();
      });
    });
  }

  wireBooleanToggleAutoSave(
    document.getElementById("allow_nsfw_quotes"),
    "advanced",
    "ALLOW_NSFW_QUOTES",
    "Couldn't save the NSFW-quotes setting. Try again."
  );
  wireBooleanToggleAutoSave(
    document.getElementById("show_diagnostics_shortcut"),
    "advanced",
    "SHOW_DIAGNOSTICS_SHORTCUT",
    "Couldn't save the diagnostics-shortcut setting. Try again."
  );
  // #346 — Weather "Show on display" toggle (converged from inline in #458).
  wireBooleanToggleAutoSave(
    document.getElementById("weather_enabled"),
    "weather",
    "WEATHER_ENABLED",
    "Could not save the weather toggle. Try again."
  );
  // #337 A13 — Temperature pill (converged from inline in #458).
  wireSegmentedAutoSave(
    document.querySelector("[data-temp-pill]"),
    "units",
    "WEATHER_UNITS",
    "Couldn't save temperature units. Try again."
  );

  // M6 adversarial /review fix — refresh the CSRF token at submit time so
  // a cached /settings HTML (from the SW navigation cache) doesn't carry a
  // 30-min-stale or post-Pi-restart token into the POST. The render-time
  // token stays in the form for no-JS users; this wraps it for JS users.
  // Async: we suspend the native submit, fetch /api/csrf, write the fresh
  // token into the hidden input, then re-fire submit. On fetch failure we
  // submit anyway with the existing (possibly stale) token — server's 403
  // path lands the user on a re-rendered form with a fresh token; no
  // worse than the no-JS path.
  document.querySelectorAll("form.settings-form").forEach(function (form) {
    let armed = false;
    form.addEventListener("submit", async function (event) {
      if (armed) return; // re-fire after refresh — let it through.
      const tokenInput = form.querySelector('input[name="csrf_token"]');
      if (!tokenInput) return; // no token field → not a CSRF-guarded form.
      event.preventDefault();
      try {
        const resp = await fetch("/api/csrf", { headers: { Accept: "application/json" } });
        const body = await resp.json();
        if (body && body.ok && body.csrf_token) {
          tokenInput.value = body.csrf_token;
        }
      } catch (_e) {
        // Network blip — submit with the render-time token; server's 403
        // path re-renders with a fresh one.
      }
      armed = true;
      form.submit();
    });
  });
})();
