"""Geocoding + timezone utilities for LitClock setup.

Provides location lookup via Nominatim (OpenStreetMap) and IP-based
geolocation via ip-api.com, plus timezone derivation from coordinates
(``timezone_from_coords``) and timezone application to the OS
(``set_system_timezone`` — moved here from setup_server.py in #414
maintainability cleanup so location_resolver can pull it without
dragging setup_server's captive-portal / http.server imports onto the
boot-critical reresolve oneshot's startup path).
"""

import json
import logging
import subprocess
import urllib.request

log = logging.getLogger(__name__)

# Canonical set of env.sh keys touched by location-resolution writers.
# Both the provisioning resolver (setup_server._update_env_location) and the
# Settings PATCH route (control_server/routes/settings.py) write a subset of
# these keys; centralising the tuple here keeps the contract visible. The
# first three are the location triplet covered by the all-or-none coherence
# guard in settings.py; WEATHER_UNITS is set alongside the triplet by the
# provisioning resolver (derived from country_code) but evolves independently
# from the PWA Settings UI. See EPIC #383 / PR1 for the rationale.
LOCATION_ENV_KEYS = (
    "WEATHER_LATITUDE",
    "WEATHER_LONGITUDE",
    "WEATHER_LOCATION_NAME",
    "WEATHER_UNITS",
)

_tf = None


def _get_tf():
    """Return a cached TimezoneFinder instance (lazy import)."""
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder

        _tf = TimezoneFinder()
    return _tf


def geocode_location(query, country_code=None):
    """Geocode a city/zip to coordinates via Nominatim (OpenStreetMap).

    Args:
        query: Location string, e.g. "Austin, TX" or "78701"
        country_code: Optional ISO 3166-1 alpha-2 country code (e.g. "US") to
            bias results. Without this, bare zip codes like "78701" may resolve
            to the wrong country.

    Returns:
        dict with keys: lat (str), lon (str), display_name (str), timezone
        (str or None), country_code (str or None — upper-cased ISO 3166-1
        alpha-2, parsed from Nominatim's ``address.country_code`` field
        added by #337 A16 to power the unified country-change UNITS-reset
        rule across all save paths).
        On failure: dict with key: error (str)
    """
    try:
        encoded = urllib.parse.quote(query)
        # #337 A16: ``addressdetails=1`` returns an ``address`` object with
        # a ``country_code`` field (lowercase 2-char ISO), which we need so
        # PWA Specific saves can apply the A6 country-change UNITS-reset
        # rule. The response shape is otherwise unchanged — the new field
        # is additive, so existing callers keep working.
        url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1"
        if country_code:
            url += f"&countrycodes={urllib.parse.quote(country_code.lower())}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "LitClock/1.0 (https://github.com/kapoorankush/litclock)",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        if not data:
            return {"error": "Location not found"}

        result = data[0]
        lat = result["lat"]
        lon = result["lon"]
        display_name = result["display_name"]

        # #337 A16: extract country_code from the address sub-dict.
        # Nominatim returns it as lowercase 2-char (e.g., "us", "gb", "in"),
        # canonicalize to upper for consistency with ``ip_geolocate()`` +
        # ``_persisted_country()`` which both work in uppercase.
        resolved_country = None
        address = result.get("address") if isinstance(result, dict) else None
        if isinstance(address, dict):
            cc = address.get("country_code")
            if isinstance(cc, str) and cc:
                resolved_country = cc.upper()

        tz = timezone_from_coords(lat, lon)

        return {
            "lat": lat,
            "lon": lon,
            "display_name": display_name,
            "timezone": tz,
            "country_code": resolved_country,
        }
    except Exception as e:
        log.warning("Geocode failed for %r: %s", query, e)
        return {"error": str(e)}


def ip_geolocate():
    """Get approximate location from public IP via ip-api.com.

    Returns:
        dict with keys: lat (str | None), lon (str | None), city (str | None),
        country_code (str | None), timezone (str | None).
        On failure: None.

    All fields use ``.get()`` against the JSON response and skip empty/None
    components when composing ``city`` so callers never see a degenerate
    ``", "`` placeholder for IPs without granular city data (cloud-provider
    egress, satellite uplinks, some VPN exits — observed in production).
    """
    try:
        url = "http://ip-api.com/json/?fields=lat,lon,city,regionName,country,countryCode,timezone"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        def _str_or_none(v):
            return str(v) if v not in (None, "") else None

        city_parts = [p for p in (data.get("city"), data.get("regionName")) if p]
        return {
            "lat": _str_or_none(data.get("lat")),
            "lon": _str_or_none(data.get("lon")),
            "city": ", ".join(city_parts) if city_parts else None,
            "country_code": data.get("countryCode") or None,
            "timezone": data.get("timezone") or None,
        }
    except Exception as e:
        log.warning("IP geolocation failed: %s", e)
        return None


def timezone_from_coords(lat, lon):
    """Derive timezone from lat/lon using timezonefinder.

    Returns:
        timezone string (e.g. "America/Chicago") or None
    """
    try:
        tf = _get_tf()
        return tf.timezone_at(lat=float(lat), lng=float(lon))
    except Exception as e:
        log.warning("Timezone lookup failed for (%s, %s): %s", lat, lon, e)
        return None


def set_system_timezone(timezone):
    """Set the system timezone via ``timedatectl``.

    Moved here from ``setup_server.py`` in the #414 maintainability cleanup
    so callers (``location_resolver``, ``routes/settings.py``, ``routes/handoff.py``)
    can apply timezones without importing the captive-portal/http.server
    surface that lives in setup_server. Validates against
    ``timedatectl list-timezones`` before applying via ``sudo timedatectl
    set-timezone``. Returns ``(ok: bool, error_message: str | None)``.
    """
    if not timezone:
        return False, "No timezone specified"

    try:
        # Validate timezone exists
        result = subprocess.run(["timedatectl", "list-timezones"], capture_output=True, text=True)  # noqa: S603,S607
        valid_timezones = result.stdout.strip().split("\n")

        if timezone not in valid_timezones:
            return False, f"Invalid timezone: {timezone}"

        # Set the timezone via the root-owned wrapper (#387). We CANNOT call
        # `sudo timedatectl set-timezone <tz>` directly: sudoers/020 only
        # authorizes the wrapper's fixed path (a `set-timezone *` glob would be
        # a privilege hole once 010_pi-nopasswd is dropped). The wrapper
        # re-validates the tz in root-owned code — this in-process check is a
        # UX fast-path, not the security boundary.
        result = subprocess.run(  # noqa: S603,S607
            ["sudo", "/usr/local/lib/litclock/litclock-set-timezone", timezone],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False, f"Failed to set timezone: {result.stderr}"

        return True, None
    except FileNotFoundError:
        return False, "timedatectl not found"
    except Exception as e:
        return False, str(e)
