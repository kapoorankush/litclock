"""Settings tab + edit endpoints (M3).

Routes:

- GET  /settings              — HTML render of all 5 sections, current values
                                 pre-filled from env.sh, single CSRF token shared
                                 across the section forms.
- POST /settings              — No-JS form action. PRG: success → 303 to
                                 ``/settings?saved=<section>[&name=<resolved>]``;
                                 failure → 200 with re-render + field-error banner.
- POST /api/settings          — JSON path. PATCH-merge semantics; only keys
                                 present in body are written. Returns the
                                 project-wide envelope per #254.
- GET  /api/geocode?q=...     — City/zip preview lookup; returns resolved
                                 name + lat/lon for live UI feedback before save.

Locked decisions (PLAN-LitClock-Control-PWA.md M3 D1–D8):

- D1: NO `systemctl restart`. After every successful save, fire
      ``systemctl start --no-block litclock.service`` so the next minute-tick
      lands within ~3 s instead of waiting up to 60 s for the OnCalendar fire.
- D2: Single ``POST /api/settings`` with PATCH-merge — only keys present in
      the body are written.
- D3: City/zip resolved server-side on save AND surfaced via /api/geocode for
      live preview. Reuses ``geocoding.geocode_location`` with IP-country bias.
- D4 + D5: CSRF guard — multi-use synchronizer token (action="settings",
      TTL 30 min) plus reflexive Origin/Referer match against ``request.host``.
- D6: One writer — ``config.atomic_update``. Same flock as setup_server.
- D7: ``GIFT_MODE_MESSAGE`` writer wraps via ``shlex.quote()`` (in
      ``config._serialize_value``) and content-allowlists backtick + ``$``.
- D8: Split URLs — ``/settings`` is HTML PRG; ``/api/settings`` is JSON.
      Both call a shared ``_save_and_apply()`` helper, which orchestrates
      three phase helpers (#414 item #1):
      ``_apply_clear_or_geocode`` → ``_validate_payload`` →
      ``_run_sync_quick_if_needed``.

Bundled into M3: timezone update via ``geocoding.timezone_from_coords`` +
``geocoding.set_system_timezone`` when city/zip edit changes coordinates
(#414 item #5 — was ``setup_server.set_system_timezone`` pre-extraction).
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from typing import Any, Final

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request

from .. import handoff
from ..csrf import CSRF_ACTION, CsrfTokenStore, origin_matches_host
from ..errors import envelope

bp = Blueprint("settings", __name__)

log = logging.getLogger(__name__)

SYSTEMCTL: Final[str] = "/usr/bin/systemctl"
SYSTEMCTL_TIMEOUT_S: Final[int] = 5

# The section identifiers a save can target. Used by the PRG redirect
# (`?saved=<section>`) and as keys for per-section error rendering.
SECTIONS: Final[tuple[str, ...]] = ("weather", "units", "gift", "location", "advanced")

# Map section -> set of env keys that section is allowed to write. Belt
# alongside SETTINGS_ALLOWLIST: the allowlist enforces shell-safety; this
# table enforces UI scoping (a "weather" form can't sneak in
# ALLOW_NSFW_QUOTES). The union of values must be a subset of the
# config.SETTINGS_ALLOWLIST keys — guarded by a unit test.
SECTION_KEYS: Final[dict[str, frozenset[str]]] = {
    # #337 A9: Weather section reduced to the visibility toggle only —
    # the city/zip + radio + worldwide checkbox + raw coords all moved to
    # the Location section below. Pre-#337 layout had `WEATHER_LATITUDE`
    # etc. listed here because the Weather section's "City or zip" input
    # wrote them; after the IA shift those keys are owned by Location.
    "weather": frozenset({"WEATHER_ENABLED"}),
    # #337 A11: section identifier stays "units" (the env key is still
    # WEATHER_UNITS) but the rendered title is "Temperature". A13 makes
    # the control auto-save on click — no Save button in this section.
    "units": frozenset({"WEATHER_UNITS"}),
    # #280: gift section is now compose-only. GIFT_MODE_MESSAGE persists as
    # a transient draft of the welcome message; the actual "trigger gift
    # mode" action is /api/system/prepare-for-gift on the system blueprint.
    # GIFT_MODE_ENABLED dropped — the M3 toggle had no runtime semantics
    # and the new design treats gift mode as a one-shot action, not a
    # persistent state.
    "gift": frozenset({"GIFT_MODE_MESSAGE"}),
    # #337 A9 + A10: Location section is the new dominant section — owns
    # the entire location picker (radio pill, Place input, worldwide
    # checkbox, raw lat/lon `<details>`). Per A16 the section is also
    # allowed to write WEATHER_UNITS so the unified country-change reset
    # rule can flip units when a location save crosses a country boundary.
    # WEATHER_IP_COUNTRY is server-derived (never user-typed) but listed
    # so the section's _coerce_payload can write it after Specific
    # geocoding extracts the country from Nominatim.
    "location": frozenset(
        {
            "WEATHER_LOCATION_MODE",
            "WEATHER_LATITUDE",
            "WEATHER_LONGITUDE",
            "WEATHER_LOCATION_NAME",
            "WEATHER_IP_COUNTRY",
            "WEATHER_UNITS",
        }
    ),
    # #416 PR3c (F31) — SHOW_DIAGNOSTICS_SHORTCUT is the opt-in for the
    # full-label diagnostics ribbon (dots-three default per OV-D-C).
    # Lives in Advanced to keep it out of the owner-persona's primary
    # surface; helper persona can flip it via the section toggle.
    "advanced": frozenset({"ALLOW_NSFW_QUOTES", "SHOW_DIAGNOSTICS_SHORTCUT"}),
}


# ─── helpers ────────────────────────────────────────────────────────────────


def _csrf_store() -> CsrfTokenStore:
    return current_app.extensions["csrf_tokens"]


def _env_file() -> str:
    return current_app.config["ENV_FILE"]


def _load_settings() -> dict[str, str]:
    """Read current env.sh into a flat dict of allowlisted keys only.
    Unknown keys (DISPLAY_CLEAR_HOUR, OPENWEATHERMAP_APIKEY, ...) are
    preserved on disk by ``config.atomic_update`` but not surfaced to the
    UI — keeps the M3 surface focused on what's actually editable."""
    import config as _config  # noqa: PLC0415

    raw = _config.load_config(_env_file())
    return {k: v for k, v in raw.items() if k in _config.SETTINGS_ALLOWLIST}


_AD_HOC_TICK_RENDER_BUDGET_S: Final[float] = 15.0
_AD_HOC_TICK_POLL_INTERVAL_S: Final[float] = 0.5


# Per `systemctl(1)` + verified on Pi Bookworm 2026-04-29: "activating",
# "deactivating", "reloading" all share the "the unit is busy doing
# something, fire requests get coalesced" property with "active". Don't
# rely on `is-active --quiet`'s exit code — on this systemd version
# `--quiet` returns 3 for "activating", which would let the polling
# thread fire mid-render and have the start coalesced into the
# in-flight oneshot run. Parse the state string directly instead.
_BUSY_STATES = frozenset({"active", "activating", "deactivating", "reloading"})


def _service_is_active(unit: str) -> bool:
    """Return True iff ``systemctl is-active`` reports the unit in any
    state that would coalesce a fresh ``start`` request.

    is-active is unprivileged on systemd (no sudo needed). Returns False
    on any subprocess failure — better to fire a possibly-redundant tick
    than to hang the background thread.
    """
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-active", unit],
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return (result.stdout or "").strip() in _BUSY_STATES


def _fire_ad_hoc_tick_blocking() -> None:
    """Wait for any in-flight render to finish, then fire ``systemctl start
    --no-block litclock.service``.

    Background thread body. Why polling: ``litclock.service`` is
    ``Type=oneshot``, and ``systemctl start`` while the unit is already
    activating is silently coalesced into the in-flight run. So calling
    start mid-render is a no-op, and the user waits up to 60 s for the
    next ``OnCalendar`` fire (caught on test Pi 2026-04-29: render
    takes ~9 s, so ~15% of saves landed during a render and looked like
    "wait for the minute to change").

    We poll ``is-active`` (unprivileged) for up to ``_AD_HOC_TICK_RENDER_BUDGET_S``
    before firing. If the unit is still active after the budget, fire
    anyway — better a coalesced no-op than no fire at all.
    """
    import time as _time  # noqa: PLC0415

    # #362 D7 — lazy import to avoid a circular import between settings.py
    # and system.py (both blueprints live under control_server.routes). The
    # ad-hoc tick fires rarely enough that the per-call import cost is
    # invisible.
    from .system import shutdown_imminent_check  # noqa: PLC0415

    deadline = _time.monotonic() + _AD_HOC_TICK_RENDER_BUDGET_S
    while _time.monotonic() < deadline:
        if not _service_is_active("litclock.service"):
            break
        _time.sleep(_AD_HOC_TICK_POLL_INTERVAL_S)
    # #362 D7 (codex post-review TOCTOU fix) — hold the shutdown-imminent
    # lock for the duration of the check+act block. Without atomicity, the
    # naive shape (`if is_shutdown_imminent(): return`) had a race: this
    # thread could check the flag, get preempted, then have
    # /api/system/{reboot,poweroff} set the flag AND run pre-stop, then
    # this thread could resume and fire `systemctl start litclock.service`
    # AFTER the pre-stop completed — re-opening the very race the pre-stop
    # was meant to close. shutdown_imminent_check() holds
    # _SHUTDOWN_IMMINENT_LOCK for the whole with-block, so a concurrent
    # _execute_action blocks on the lock until we exit. By the time it
    # acquires the lock, our start call (if it landed) has already been
    # enqueued, and the subsequent pre-stop will cancel/stop it.
    try:
        with shutdown_imminent_check() as imminent:
            if imminent:
                log.info("ad-hoc tick aborted: shutdown imminent")
                return
            subprocess.run(
                ["sudo", SYSTEMCTL, "start", "--no-block", "litclock.service"],
                check=True,
                timeout=SYSTEMCTL_TIMEOUT_S,
                capture_output=True,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("ad-hoc systemctl start litclock.service failed: %r", exc)


def _ad_hoc_tick() -> None:
    """Fire an ad-hoc render in the background (D1).

    Returns immediately so the HTTP response flushes within Flask's
    normal response cycle (~ms). The actual fire happens on a daemon
    thread that respects in-flight renders — see
    ``_fire_ad_hoc_tick_blocking`` for why polling matters.
    """
    import threading as _threading  # noqa: PLC0415

    t = _threading.Thread(target=_fire_ad_hoc_tick_blocking, daemon=True, name="ad-hoc-tick")
    t.start()


def _enforce_csrf() -> None:
    """Validate Origin/Referer + token. Aborts 403 on failure with the
    project envelope per ``errors.py``. Pulls the token from form-encoded
    ``csrf_token`` (no-JS POST) or from a JSON body's ``csrf_token`` field
    (PWA fetch path)."""
    if not origin_matches_host(request):
        abort(403, description="Origin or Referer header missing or mismatched.")

    token = request.form.get("csrf_token")
    if not token:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            candidate = body.get("csrf_token")
            if isinstance(candidate, str):
                token = candidate

    if not token or not _csrf_store().validate(CSRF_ACTION, token):
        abort(403, description="CSRF token missing, expired, or invalid.")


def _resolve_location(query: str, worldwide: bool = False) -> tuple[Mapping[str, str] | None, str | None]:
    """Geocode ``query`` and return either the resolved fields or an error
    string.

    By default uses IP-country biasing so bare zip codes resolve to the
    user's country, matching first-boot behavior. When ``worldwide=True``
    (#337 A5/A12 — the "Search worldwide (ignore autodetected location)"
    checkbox), skips the IP-geo lookup and passes ``country_code=None`` to
    Nominatim, letting a US-WiFi user resolve UK postcodes like SW1A 1AA.

    Result dict carries the geocoder's resolved ``country_code`` (upper-cased
    ISO 3166-1 alpha-2) under the same key when ``geocoding.geocode_location``
    parsed it from Nominatim's ``address`` sub-dict (#337 A16). Callers thread
    that value into the A6 country-change UNITS-reset logic.
    """
    if not query.strip():
        return None, "Location cannot be empty."
    from geocoding import geocode_location, ip_geolocate  # noqa: PLC0415

    country_code: str | None = None
    if not worldwide:
        try:
            ip_geo = ip_geolocate()
            if ip_geo:
                country_code = ip_geo.get("country_code")
        except Exception:
            country_code = None

    try:
        result = geocode_location(query, country_code=country_code)
    except Exception as exc:
        log.warning("geocode_location raised: %r", exc)
        return None, "Location lookup failed."

    if not isinstance(result, dict) or "lat" not in result or "lon" not in result:
        msg = (result or {}).get("error") if isinstance(result, dict) else None
        return None, msg or "Location not found."

    return result, None


def _maybe_set_timezone(lat: str, lon: str) -> str | None:
    """Best-effort timezone update when coordinates change. Returns the new
    timezone string on success, None on failure (logged but non-fatal)."""
    try:
        from geocoding import (
            set_system_timezone,  # noqa: PLC0415  (#414 item #5: extracted from setup_server)
            timezone_from_coords,  # noqa: PLC0415
        )

        tz = timezone_from_coords(lat, lon)
        if not tz:
            return None
        ok, err = set_system_timezone(tz)
        if not ok:
            log.warning("set_system_timezone(%s) failed: %s", tz, err)
            return None
        return tz
    except Exception as exc:  # noqa: BLE001
        log.warning("timezone update failed: %r", exc)
        return None


def _short_location_name(display_name: str) -> str:
    """Reduce Nominatim's verbose ``display_name`` (e.g.
    "Austin, Travis County, Texas, United States") to a tidy short form
    ("Austin, Texas") suitable for the Status tab and the
    ``WEATHER_LOCATION_NAME`` env var.

    Defensive guarantees:
    - Strips characters the env.sh validator would reject (backtick, ``$``,
      newlines, NUL) before assignment so a legitimate Nominatim result
      can never produce a value the writer then fails to validate.
    - Truncates to ``WEATHER_LOCATION_NAME_MAX_LEN`` (validator cap) so an
      unusually long administrative cascade doesn't 422 the save AFTER
      the geocode succeeded.
    Returns an empty string when the input is empty or the post-sanitize
    result is empty.
    """
    if not display_name:
        return ""
    import config as _config  # noqa: PLC0415

    raw = display_name.replace("\n", " ").replace("\r", " ").replace("\x00", "")
    for ch in ("`", "$"):
        raw = raw.replace(ch, "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 3:
        # Drop the trailing country segment if present, then keep the
        # leading place + the most-specific administrative region. For
        # "Austin, Travis County, Texas, United States" → "Austin, Texas".
        short = f"{parts[0]}, {parts[-2]}"
    else:
        short = ", ".join(parts)
    cap = _config.WEATHER_LOCATION_NAME_MAX_LEN
    if len(short) > cap:
        short = short[:cap].rstrip().rstrip(",")
    return short


def _normalize_bool(value: Any) -> str | None:
    """Translate the various truthy/falsy representations the UI sends
    (HTML checkbox: missing key = false, "on" = true; JSON: true/false/strings)
    into the canonical "true"/"false" strings the validator expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "false"
    s = str(value).strip().lower()
    if s in ("true", "on", "1", "yes"):
        return "true"
    if s in ("false", "off", "0", "no", ""):
        return "false"
    return None


def _coerce_payload(
    raw: Mapping[str, Any],
    section: str | None,
    *,
    is_form: bool,
) -> tuple[dict[str, str], dict[str, str], str | None, bool, bool]:
    """Translate a request payload (form OR JSON) into ``(env_updates,
    field_errors, location_query, clear_weather, worldwide)``.

    - ``section`` (when set) restricts the writable keys to that section's
      ``SECTION_KEYS`` entry — the no-JS form path includes a hidden
      ``section`` field; the JSON path may omit it (PATCH-merge across
      whatever the caller sent).
    - ``is_form`` distinguishes the two writers. HTML checkboxes that aren't
      checked don't appear in the payload at all, so for an HTML form POST
      with a named section we synthesise the missing booleans as ``"false"``
      (required for unchecked-checkbox UX). JSON PATCH callers (D2) get
      strict "only keys present in body are written" semantics — we never
      synthesise booleans for them, otherwise a JSON caller sending
      ``{"section":"weather","WEATHER_LATITUDE":"33.0"}`` would silently
      flip ``WEATHER_ENABLED`` to false even though they never sent it.
    - ``location_query`` is captured separately — it's not an env key, but
      a city/zip string that the route resolves into lat/lon via geocoding.
    - ``clear_weather`` (issue #325): pre-#337 only. Retained as the
      "section=weather + clear=1" Clear button used to fire it — the
      button is removed in A10 but the server-side handling stays as a
      defensive backstop for any orphaned client still POSTing it.
    - ``worldwide`` (#337 A5/A12): form/JSON-supplied flag indicating the
      user checked "Search worldwide" on the Location section. Threaded
      through to ``_resolve_location`` so Nominatim is called without an
      IP-country bias. Per A5 the checkbox is form-state only, not
      persisted — we don't write it to env.sh.
    """
    field_errors: dict[str, str] = {}
    env_updates: dict[str, str] = {}

    allowed_keys = SECTION_KEYS.get(section, set()) if section else None

    location_query = raw.get("location_query")
    if isinstance(location_query, str):
        location_query = location_query.strip() or None
    else:
        location_query = None

    # #325 — explicit Clear affordance for the weather section. Only the
    # weather Clear form sets clear=1; any other section ignores it. We
    # don't accept clear=true / yes / etc. — it's a hidden field set by
    # the template, so a strict "1" match avoids accidental triggers
    # from a misbehaving client.
    # #337 A10: the Clear button is removed in the new IA (Automatic radio
    # IS the reset). Defensive backstop kept for any orphaned client
    # still POSTing clear=1; new templates never send it.
    clear_weather = False
    if section in ("weather", "location"):
        clear_raw = raw.get("clear")
        if isinstance(clear_raw, str) and clear_raw.strip() == "1":
            clear_weather = True
        elif clear_raw is True:  # JSON callers may send a bool
            clear_weather = True

    # #337 A5/A12: worldwide checkbox flag. Form checkboxes send "on" when
    # checked; JSON callers may send a bool or "1". Absent = unchecked
    # (today's IP-country-bias behavior).
    worldwide = False
    if section == "location":
        ww_raw = raw.get("worldwide")
        if isinstance(ww_raw, bool):
            worldwide = ww_raw
        elif isinstance(ww_raw, str) and ww_raw.strip().lower() in ("on", "true", "1", "yes"):
            worldwide = True

    bool_keys = {"WEATHER_ENABLED", "ALLOW_NSFW_QUOTES", "SHOW_DIAGNOSTICS_SHORTCUT"}

    # Synthesis is form-only. JSON PATCH preserves D2 semantics strictly.
    # #325 — when clearing, DON'T synthesise WEATHER_ENABLED to false even
    # on the form path. Tension 7 was rejected: the locked decision is
    # that WEATHER_ENABLED stays unchanged on clear (the honest label on
    # the Clear button informs the user that weather will pause). If we
    # synthesised here, the Clear form (which doesn't render a toggle
    # checkbox) would silently flip the toggle to false, contradicting
    # the locked decision.
    if is_form and allowed_keys is not None and not clear_weather:
        for k in bool_keys & set(allowed_keys):
            if k not in raw:
                env_updates[k] = "false"

    for key, value in raw.items():
        if key in {"csrf_token", "section", "location_query", "clear", "worldwide"}:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            # Silently ignore — caller might send extra keys we'll surface as
            # a 422 below if they're invalid. For section-scoped requests we
            # just don't write keys outside the section.
            continue
        if key in bool_keys:
            normalised = _normalize_bool(value)
            if normalised is None:
                field_errors[key] = "must be 'true' or 'false'"
            else:
                env_updates[key] = normalised
        else:
            if value is None:
                continue
            env_updates[key] = str(value)

    return env_updates, field_errors, location_query, clear_weather, worldwide


def _apply_clear_or_geocode(
    env_updates: dict[str, str],
    location_query: str,
    *,
    worldwide: bool,
    clear_weather: bool,
    existing: Mapping[str, str],
    field_errors: dict[str, str],
) -> tuple[str | None, str | None]:
    """#414 item #1 helper: handle the Clear-or-geocode branch and the
    follow-on country-change UNITS-flip.

    Mutates ``env_updates`` and ``field_errors`` in place. Returns
    ``(resolved_name, resolved_country)`` — both ``None`` unless a geocode
    succeeded. ``existing`` is the hoisted ``_load_settings()`` snapshot
    (#414 item #2) so the country lookup doesn't re-read env.sh.
    """
    resolved_name: str | None = None
    resolved_country: str | None = None
    if clear_weather:
        # #337 A10: Clear button removed from the new IA but the codepath
        # is retained as a defensive backstop. Zeros the three location keys
        # and the country bookkeeping so the next on-boot reresolve has
        # nothing to preserve and writes fresh defaults.
        env_updates["WEATHER_LOCATION_NAME"] = ""
        env_updates["WEATHER_LATITUDE"] = ""
        env_updates["WEATHER_LONGITUDE"] = ""
        env_updates["WEATHER_IP_COUNTRY"] = ""
        return resolved_name, resolved_country

    if location_query:
        # If a city/zip was supplied, resolve it server-side and merge the
        # results into the env updates. This runs BEFORE atomic_update so a
        # failed geocode doesn't half-write coords (A15 atomicity contract).
        resolved, err_msg = _resolve_location(location_query, worldwide=worldwide)
        if err_msg or not resolved:
            field_errors["location_query"] = err_msg or "Location not found."
        else:
            env_updates["WEATHER_LATITUDE"] = str(resolved["lat"])
            env_updates["WEATHER_LONGITUDE"] = str(resolved["lon"])
            display = str(resolved.get("display_name") or "")
            short = _short_location_name(display)
            if short:
                env_updates["WEATHER_LOCATION_NAME"] = short
                resolved_name = short
            # #337 A16: Nominatim returned a country_code (via the
            # addressdetails=1 query param). Stash it so the country-change
            # UNITS-reset block below can decide whether to flip UNITS.
            geocoded_country = resolved.get("country_code")
            if isinstance(geocoded_country, str) and geocoded_country:
                resolved_country = geocoded_country.upper()

    # #337 A16: country-change UNITS-flip for Specific saves with a
    # geocoded country. Mirrors the on-boot reresolve's A6 rule so the
    # behavior is uniform across all save paths. If the new country
    # differs from persisted WEATHER_IP_COUNTRY (or persisted is empty —
    # first-resolve bootstrap), write the new country's default UNITS
    # and update IP_COUNTRY. Else preserve the user's manual Temperature
    # override (same-country city change must not reset units — e.g.
    # Austin TX → Boston MA shouldn't wipe a user's Celsius pick).
    if resolved_country:
        from location_resolver import (
            country_default_units,  # noqa: PLC0415  (#414: now public — no longer a private import)
        )

        persisted_country = (existing.get("WEATHER_IP_COUNTRY") or "").upper().strip()
        if not persisted_country or persisted_country != resolved_country:
            env_updates["WEATHER_UNITS"] = country_default_units(resolved_country)
            env_updates["WEATHER_IP_COUNTRY"] = resolved_country

    return resolved_name, resolved_country


def _validate_payload(
    env_updates: dict[str, str],
    *,
    clear_weather: bool,
    existing: Mapping[str, str],
    field_errors: dict[str, str],
) -> None:
    """#414 item #1 helper: per-key ``validate_setting`` sweep + the I7
    all-or-none triplet guard. Mutates ``field_errors``."""
    import config as _config  # noqa: PLC0415

    # Validate up-front so we surface every field error in one shot rather
    # than 422-ing on whichever happens to be first. config.atomic_update
    # would itself fail-fast, but we want the per-field error map.
    #
    # #325 — empty WEATHER_LATITUDE / WEATHER_LONGITUDE / WEATHER_LOCATION_NAME
    # are valid "unset" states (matching env.sh.sample's shipped empty
    # WEATHER_LOCATION_NAME=) per the lat/lon validators' empty-string
    # short-circuit. The Clear affordance relies on this.
    for key, value in env_updates.items():
        ok, err = _config.validate_setting(key, value)
        if not ok:
            field_errors[key] = err or "invalid value"

    # Review I7 — route-level all-or-none guard on the three weather
    # location keys. #325 widened the lat/lon validators to accept empty
    # strings globally so the Clear path works; that widening lets the
    # regular Location form persist incoherent partial states (e.g.
    # WEATHER_LATITUDE="30.27" with WEATHER_LONGITUDE=""). literary_clock
    # skips weather gracefully on a partial pair, but env state being
    # dishonest is a sharp edge that surfaces as "I set my latitude and
    # weather still doesn't work" support drift.
    #
    # Compute the post-save state by merging env_updates on top of existing
    # env.sh values. Reject only when the save WORSENS the partial state —
    # specifically: a save that writes at least one of the three keys AND
    # leaves the merged result at set_count 1 or 2 AND the user actually
    # blanked or partially-set a key (i.e. the save isn't a pure pass-through
    # of pre-existing partial state). Pre-existing partial state from
    # env.sh.sample (lat+lon set, name="" — set_count==2) must NOT 422 on
    # unrelated saves; that would regress D2's strict PATCH-merge contract
    # and break units-only / advanced-only saves on every fresh install.
    # Skip when clear_weather fired — Clear's whole purpose is to land at
    # set_count == 0.
    #
    # The triplet here is the location subset of geocoding.LOCATION_ENV_KEYS
    # (which adds WEATHER_UNITS for the provisioning resolver). The
    # all-or-none guard applies only to lat/lon/name; WEATHER_UNITS is
    # independently editable from the PWA Units control. Sourced from the
    # constant so a future addition to LOCATION_ENV_KEYS automatically
    # surfaces here if it belongs in the triplet.
    from geocoding import LOCATION_ENV_KEYS  # noqa: PLC0415

    weather_loc_keys = LOCATION_ENV_KEYS[:3]
    touched_weather_loc = bool(set(weather_loc_keys) & env_updates.keys())
    if not clear_weather and touched_weather_loc:
        existing_state = tuple(bool(existing.get(k, "")) for k in weather_loc_keys)
        final_lat = env_updates.get("WEATHER_LATITUDE", existing.get("WEATHER_LATITUDE", ""))
        final_lon = env_updates.get("WEATHER_LONGITUDE", existing.get("WEATHER_LONGITUDE", ""))
        final_name = env_updates.get("WEATHER_LOCATION_NAME", existing.get("WEATHER_LOCATION_NAME", ""))
        final_state = (bool(final_lat), bool(final_lon), bool(final_name))
        final_set_count = sum(final_state)
        existing_set_count = sum(existing_state)
        # Reject only if the save introduces/worsens partial state. A pure
        # pass-through that leaves the state identical to existing partial
        # state is accepted (D2 PATCH-merge contract). A save that improves
        # toward set_count==3 from pre-existing partial state is accepted.
        # A save that DROPS to a smaller non-zero set_count or stays at the
        # same partial count while changing which key is unset is rejected.
        if final_set_count not in (0, 3):
            is_worsening = (
                final_set_count < existing_set_count  # blanked a key
                or existing_set_count == 0  # added partial from clean state
                or final_state != existing_state  # shifted which key is blank
            )
            if is_worsening:
                field_errors["location_query"] = (
                    "Location must be all-or-none: provide a city/zip (which fills "
                    "latitude, longitude, and name) or use the Clear button to remove "
                    "all three."
                )


def _run_sync_quick_if_needed(
    env_updates: Mapping[str, str],
    resolved_country: str | None,
    previous_mode: str,
    env_file: str,
) -> tuple[bool, bool]:
    """#414 item #1 helper: #337 A12 Specific→Auto switch sync-quick + the
    Auto-Save-as-refresh path. Returns ``(attempted, succeeded)``.

    Detects transitions from the incoming MODE vs the persisted MODE
    (passed in from the hoisted ``existing`` snapshot — #414 item #2). On
    hard-fail the resolver writes nothing; the caller's subsequent
    ``atomic_update`` still lands MODE=auto, and lat/lon stays at the prior
    Specific values until the next on-boot reresolve. The caller surfaces
    the bool in the response body so the PWA can render "city couldn't
    auto-detect; next reboot will retry" per A7.
    """
    incoming_mode_for_switch = env_updates.get("WEATHER_LOCATION_MODE", "").strip()
    is_specific_to_auto = incoming_mode_for_switch == "auto" and previous_mode == "specific"
    # Also fire sync-quick on a same-mode Auto save (A12 "Save = refresh
    # detection" in Auto mode). The user tapping Save while already in
    # Auto means "refresh the IP-geo result"; that maps cleanly onto the
    # same sync-quick call.
    is_auto_save_refresh = (
        incoming_mode_for_switch == "auto"
        and previous_mode == "auto"
        and not resolved_country  # Don't fire when a Specific Place was also typed in same payload
        and not env_updates.get("WEATHER_LATITUDE")  # Or when Advanced raw coords were sent
    )
    if not (is_specific_to_auto or is_auto_save_refresh):
        return False, False

    try:
        from location_resolver import resolve_location_from_ip as _sync_quick  # noqa: PLC0415

        # Use the resolver's explicit success bool (#337 A12 + /review
        # fix). Earlier draft snapshotted env.sh before/after lat/lon,
        # which gave a false "failed" hint when IP-geo legitimately
        # returned the same coords twice (Auto save where the user
        # hadn't moved — re-confirms the previous result but lat/lon
        # don't visibly change). `resolve_location_from_ip` now returns
        # True iff IP-geo + write both succeeded.
        return True, bool(_sync_quick(retries=False, env_file=env_file))
    except Exception as exc:  # noqa: BLE001 — best-effort per A7/A8
        log.warning("sync-quick IP-geo resolver failed: %r", exc)
        return True, False


def _save_and_apply(
    raw_payload: Mapping[str, Any],
    section: str | None,
    *,
    is_form: bool,
) -> tuple[dict[str, Any], int]:
    """Shared writer for both the JSON (D2) and HTML PRG (D8) paths.

    Returns ``(envelope_body, status_code)``. On success the body carries
    ``ok=True`` plus a snapshot of the post-save settings; on validation /
    geocode failure the body carries ``ok=False`` + a 422 envelope with
    ``error.fields`` populated. Both call sites translate the result into
    their own response shape. ``is_form`` controls whether unchecked HTML
    checkboxes are synthesised to ``"false"`` (form path) or treated as
    "key not sent" (JSON PATCH path).

    #414 item #1: orchestrator only; the three logical phases live in
    ``_apply_clear_or_geocode``, ``_validate_payload``, and
    ``_run_sync_quick_if_needed``.

    #414 item #2: ``_load_settings()`` is read once up front and threaded
    through the helpers. The post-write response body snapshot is the only
    additional read (it has to be — that's what the snapshot shows). Net:
    5 reads pre-#414 → 2 reads.
    """
    import config as _config  # noqa: PLC0415

    env_updates, field_errors, location_query, clear_weather, worldwide = _coerce_payload(
        raw_payload, section, is_form=is_form
    )

    # #414 item #2: single pre-write read. Feeds the country-change check
    # (_apply_clear_or_geocode), the all-or-none guard (_validate_payload),
    # the previous-MODE detect for sync-quick, and the no-op return body.
    existing = _load_settings()

    # #337 A14 + A10 + A12: Specific-mode Save without a typed Place value
    # is rejected at the route. The template's Save button is also
    # JS-disabled in this state (A10), so this server-side guard is the
    # backstop for no-JS clients and JSON callers. The radio-flip-only case
    # (user switched Auto→Specific without typing) gets a clear inline
    # error rather than silently persisting MODE=specific with empty coords.
    incoming_mode = env_updates.get("WEATHER_LOCATION_MODE", "").strip()
    if incoming_mode == "specific" and not clear_weather and not location_query:
        # Also accept the Advanced raw-coords path (A17): if either
        # WEATHER_LATITUDE or WEATHER_LONGITUDE was sent in the payload,
        # the user typed coords in the Advanced details; let the
        # validators below catch any range/format errors.
        if not (env_updates.get("WEATHER_LATITUDE") or env_updates.get("WEATHER_LONGITUDE")):
            field_errors["location_query"] = "Type a place or pick Automatic."

    resolved_name, resolved_country = _apply_clear_or_geocode(
        env_updates,
        location_query,
        worldwide=worldwide,
        clear_weather=clear_weather,
        existing=existing,
        field_errors=field_errors,
    )

    _validate_payload(
        env_updates,
        clear_weather=clear_weather,
        existing=existing,
        field_errors=field_errors,
    )

    if field_errors:
        return (
            {
                "ok": False,
                "error": {
                    "code": "validation_failed",
                    "message": "One or more fields are invalid.",
                    "fields": field_errors,
                },
            },
            422,
        )

    if not env_updates:
        # Nothing to write. Treat as a no-op success so the UI's "Saved." toast
        # still fires — the user submitted; we just had nothing to update.
        # Reuses the hoisted ``existing`` snapshot — no fresh read needed
        # since by definition nothing was written (#414 item #2).
        return ({"ok": True, "saved": [], "settings": dict(existing)}, 200)

    previous_mode = (existing.get("WEATHER_LOCATION_MODE") or "auto").strip() or "auto"
    sync_quick_attempted, sync_quick_succeeded = _run_sync_quick_if_needed(
        env_updates,
        resolved_country,
        previous_mode,
        _env_file(),
    )

    coords_changed = ("WEATHER_LATITUDE" in env_updates) or ("WEATHER_LONGITUDE" in env_updates)

    try:
        _config.atomic_update(env_updates, _env_file())
    except ValueError as exc:
        # Defensive — should already be caught by the per-key validation
        # loop above, but if validate_setting and atomic_update ever drift
        # we want the error surfaced rather than 500-ing.
        return (
            {
                "ok": False,
                "error": {
                    "code": "validation_failed",
                    "message": str(exc),
                },
            },
            422,
        )
    except FileNotFoundError as exc:
        log.error("env.sh missing during save: %s", exc)
        return (
            {
                "ok": False,
                "error": {
                    "code": "config_missing",
                    "message": "Settings file not found.",
                },
            },
            500,
        )
    except TimeoutError as exc:
        # #274 follow-up #4 — `_exclusive_lock` raises TimeoutError if the
        # env.sh sidecar flock is held by another writer for > 30s (default,
        # overridable via LITCLOCK_ENV_LOCK_WAIT). Without bounded wait, the
        # Flask request thread would block indefinitely on a stuck shell
        # writer, accumulating stuck threads in waitress on every Save retry
        # until OOM. 504 is the right surface — we waited on an upstream
        # resource and gave up. settings.js handles 504 with a retry toast.
        log.warning("env.sh lock timeout during save: %s", exc)
        return (
            {
                "ok": False,
                "error": {
                    # snake_case to match the rest of the envelope contract
                    # (errors.py:_DEFAULTS, errors.py:_slug_from_name).
                    # Adversarial /review on PR-1b caught the original
                    # SCREAMING_SNAKE outlier — a future global slug-
                    # normalization would silently break the settings.js
                    # retry-toast that gates on this exact string.
                    "code": "env_lock_timeout",
                    "message": (
                        "Settings file is busy — another update (weekly "
                        "auto-update, Reset WiFi, or Prepare-for-Gifting) "
                        "is in progress. Try Save again in a few seconds."
                    ),
                },
            },
            504,
        )

    new_timezone: str | None = None
    if coords_changed:
        lat = env_updates.get("WEATHER_LATITUDE", "")
        lon = env_updates.get("WEATHER_LONGITUDE", "")
        if lat and lon:
            new_timezone = _maybe_set_timezone(lat, lon)

    _ad_hoc_tick()

    # EPIC #383 PR2 (#388): a successful Settings save during the handoff window
    # is an implicit "Done" — but only complete if the timezone is now known
    # (e.g. the user just set a city, resolving coords + tz). No-op outside the
    # handoff phase. Never starts a wrong-time clock (design-review A2).
    handoff.complete_if_timezone_known(current_app)

    body: dict[str, Any] = {
        "ok": True,
        "saved": sorted(env_updates.keys()),
        "settings": _load_settings(),
    }
    if resolved_name:
        body["location_resolved"] = resolved_name
    if new_timezone:
        body["timezone"] = new_timezone
    # #337 A7: surface the sync-quick outcome so the PWA can render the
    # right hint. Three states: not attempted (Auto save with no transition),
    # attempted and succeeded (fresh city in `body["settings"]`), attempted
    # and failed (PWA shows "city couldn't auto-detect; next reboot will retry").
    if sync_quick_attempted:
        body["sync_quick"] = "succeeded" if sync_quick_succeeded else "failed"
    return body, 200


# ─── routes ─────────────────────────────────────────────────────────────────


@bp.route("/settings")
def settings_tab() -> str:
    """GET /settings — HTML render. PRG redirect lands here with
    ``?saved=<section>&name=<resolved>``; the template renders a transient
    success banner from those query params on next load."""
    saved_section = request.args.get("saved")
    if saved_section not in SECTIONS:
        saved_section = None
    saved_name = request.args.get("name") or None

    csrf_token, _expires_at = _csrf_store().issue(CSRF_ACTION)
    # #317 item 7 — prepare_for_gift confirm token now minted on /system
    # (where the action card lives). The /settings page no longer renders
    # the destructive form.
    return render_template(
        "settings.html.j2",
        active_tab="settings",
        settings=_load_settings(),
        csrf_token=csrf_token,
        saved_section=saved_section,
        saved_name=saved_name,
        field_errors={},
        form_error=None,
    )


@bp.route("/settings", methods=["POST"])
def settings_post() -> Any:
    """POST /settings — no-JS form fallback (PRG)."""
    _enforce_csrf()
    section = request.form.get("section")
    if section not in SECTIONS:
        # Section-less form POSTs aren't part of the M3 contract; reject so
        # malformed clients don't accidentally write across boundaries.
        abort(400, description="Missing or invalid `section` field.")

    body, status = _save_and_apply(request.form.to_dict(flat=True), section, is_form=True)

    # #317 item 7 — the gift card lives on /system now, but the writer
    # endpoint stays here (centralised persistence). PRG-redirect the
    # success path to /system?saved=gift so the user lands back on the
    # tab they submitted from.
    pr_destination = "/system" if section == "gift" else "/settings"

    if status == 200 and body.get("ok"):
        # PRG redirect — survives a refresh without re-POSTing.
        location = f"{pr_destination}?saved={section}"
        if body.get("location_resolved"):
            from urllib.parse import quote  # noqa: PLC0415

            location += f"&name={quote(str(body['location_resolved']))}"
        return redirect(location, code=303)

    err = body.get("error", {}) if isinstance(body, dict) else {}
    field_errors = err.get("fields", {}) if isinstance(err, dict) else {}
    form_error = err.get("message") if isinstance(err, dict) else None

    # #317 item 7 — gift failure renders on /system so the user sees the
    # field error next to the textarea they were typing in. Other sections
    # keep re-rendering /settings as before. Pass the rejected value
    # through so the textarea reflects what the user actually typed —
    # otherwise the re-render reverts to the last-saved env.sh draft and
    # the inline error talks about input the user can't see (adversarial
    # /review finding).
    if section == "gift":
        from .system import _render_system_tab  # noqa: PLC0415

        return _render_system_tab(
            field_errors=field_errors,
            form_error=form_error,
            submitted_gift_message=request.form.get("GIFT_MODE_MESSAGE"),
            status_code=status,
        )

    # Re-render the page with the error banner + per-field errors.
    csrf_token, _expires_at = _csrf_store().issue(CSRF_ACTION)
    return (
        render_template(
            "settings.html.j2",
            active_tab="settings",
            settings=_load_settings(),
            csrf_token=csrf_token,
            saved_section=None,
            saved_name=None,
            field_errors=field_errors,
            form_error=form_error,
        ),
        status,
    )


@bp.route("/api/settings", methods=["POST"])
def api_settings_post() -> Any:
    """POST /api/settings — JSON path. PATCH-merge."""
    _enforce_csrf()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return envelope(
            "invalid_request",
            "Request body must be a JSON object.",
            400,
        )

    section = payload.get("section")
    if section is not None and section not in SECTIONS:
        return envelope(
            "invalid_section",
            f"`section` must be one of {list(SECTIONS)} or omitted.",
            400,
        )

    body, status = _save_and_apply(
        payload,
        section if isinstance(section, str) else None,
        is_form=False,
    )
    return jsonify(body), status


@bp.route("/api/csrf")
def api_csrf() -> Any:
    """GET /api/csrf — issue a fresh Settings CSRF synchronizer token.

    M6 adversarial /review caught: when the M6 service worker caches the
    rendered ``/settings`` HTML for offline rendering, the embedded
    ``csrf_token`` goes stale on Pi restart (in-memory CsrfTokenStore wipes)
    or after the 30-min TTL elapses. The next no-JS POST returns 403; the
    user has to refresh the page to mint a new token. JS-enabled clients
    can do better — fetch this endpoint right before submit, swap the
    hidden input value, then send. The cached HTML is fresh enough; the
    token within it gets refreshed at submit time.

    Caller flow (settings.js):
        fetch('/api/csrf') → {ok: true, csrf_token: "..."}
        form.querySelector('[name=csrf_token]').value = body.csrf_token
        form.submit()

    No-JS clients keep the original render-time token. Risk: a no-JS user
    on a cached page after Pi restart sees 403 on first save and must
    reload. Acceptable for the no-JS edge case.

    Response: ``{ok: true, csrf_token: "<urlsafe>", expires_at: <unix-seconds>}``.
    """
    token, expires_at = _csrf_store().issue(CSRF_ACTION)
    return jsonify({"ok": True, "csrf_token": token, "expires_at": expires_at}), 200


@bp.route("/api/geocode")
def api_geocode() -> Any:
    """GET /api/geocode?q=...[&worldwide=1] — preview lookup, no env.sh write.

    The Settings UI calls this on blur of the city/zip input so the user
    sees the resolved location before tapping Save. Same geocoding stack
    as the save path (D3) for consistency.

    #337 A5/A12 — ``worldwide=1`` skips the IP-country bias so a US-WiFi
    user can preview UK postcodes like SW1A 1AA. Any other value (absent,
    "0", "false", etc.) keeps today's behavior (bias to IP country). The
    composite cache key in settings.js (`q + "|" + worldwide`) ensures the
    blur preview re-fires when the user toggles the checkbox without
    changing the query text.
    """
    query = (request.args.get("q") or "").strip()
    if not query:
        return envelope(
            "invalid_request",
            "Query parameter `q` is required.",
            400,
        )
    worldwide_raw = (request.args.get("worldwide") or "").strip().lower()
    worldwide = worldwide_raw in ("1", "true", "on", "yes")
    resolved, err_msg = _resolve_location(query, worldwide=worldwide)
    if err_msg or not resolved:
        return envelope(
            "geocode_failed",
            err_msg or "Location not found.",
            422,
            fields={"location_query": err_msg or "Location not found."},
        )

    display = str(resolved.get("display_name") or "")
    short_name = _short_location_name(display)

    return jsonify(
        {
            "ok": True,
            "lat": str(resolved["lat"]),
            "lon": str(resolved["lon"]),
            "display_name": display,
            "short_name": short_name,
            "timezone": resolved.get("timezone"),
            # #337 A16 — bubble the resolved country (uppercase ISO 3166-1
            # alpha-2) up to the client. PWA can show country context in
            # the preview ("Currently: London, England") and the save path
            # uses it for the unified country-change UNITS rule.
            "country_code": resolved.get("country_code"),
        }
    ), 200


@bp.route("/api/system/set-timezone", methods=["POST"])
def api_system_set_timezone() -> Any:
    """POST /api/system/set-timezone — steady-state timezone setter (#337 A18).

    Companion to ``/api/handoff/set-timezone`` which is GATED on
    ``is_handoff_active`` and returns 200 no-op outside the handoff window
    (per its narrow contract: only the handoff completer needs an
    unauthenticated tz setter). The Settings tab's browser-tz fallback
    button (rendered when location is unresolvable) needs an always-on
    endpoint, hence this one — CSRF-guarded like every other Settings
    write, validates against the IANA tz db via
    ``geocoding.set_system_timezone`` (moved out of setup_server in #414
    item #5; the old import path still resolves via the re-export shim),
    returns 422 on invalid value.

    Body: ``{"timezone": "America/Chicago", "csrf_token": "..."}``.

    On success: 200 with ``{"ok": true, "timezone": "<set value>"}``.
    On invalid tz: 422 with ``invalid_timezone`` envelope code (matches
    the handoff endpoint's failure code so client error-handling can
    pivot between the two routes interchangeably).
    On missing tz: 422 with ``timezone_required`` envelope code.
    """
    _enforce_csrf()

    body = request.get_json(silent=True)
    timezone = body.get("timezone") if isinstance(body, dict) else None
    if not isinstance(timezone, str) or not timezone.strip():
        return envelope(
            "timezone_required",
            "A timezone is required.",
            422,
        )

    from geocoding import set_system_timezone  # noqa: PLC0415  (#414 item #5: extracted from setup_server)

    ok, err = set_system_timezone(timezone.strip())
    if not ok:
        log.warning("steady-state set-timezone failed for %r: %s", timezone, err)
        return envelope(
            "invalid_timezone",
            "That timezone isn't recognized. Pick one from a standard IANA list.",
            422,
        )
    return jsonify({"ok": True, "timezone": timezone.strip()}), 200
