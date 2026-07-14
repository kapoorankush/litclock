"""Tests for geocoding module — geocode_location, ip_geolocate, timezone_from_coords."""

import json
from unittest.mock import MagicMock
from urllib.error import URLError

import geocoding


def _mock_urlopen(response_data):
    """Create a mock urllib response returning the given data as JSON."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# ── geocode_location ───────────────────────────────────────────────


class TestGeocodeLocation:
    def test_successful_geocode(self, mocker):
        nominatim_result = [{"lat": "30.2672", "lon": "-97.7431", "display_name": "Austin, Travis County, Texas, USA"}]
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value=None)

        result = geocoding.geocode_location("Austin, TX")

        assert result["lat"] == "30.2672"
        assert result["lon"] == "-97.7431"
        assert "Austin" in result["display_name"]

    def test_empty_results(self, mocker):
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen([]))

        result = geocoding.geocode_location("xyznonexistent")

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_network_error(self, mocker):
        mocker.patch("geocoding.urllib.request.urlopen", side_effect=URLError("DNS lookup failed"))

        result = geocoding.geocode_location("Austin, TX")

        assert "error" in result

    def test_timezone_derived(self, mocker):
        nominatim_result = [{"lat": "30.2672", "lon": "-97.7431", "display_name": "Austin, Texas, USA"}]
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value="America/Chicago")

        result = geocoding.geocode_location("Austin, TX")

        assert result["timezone"] == "America/Chicago"

    def test_country_code_passed_to_nominatim(self, mocker):
        """Country code should be appended as countrycodes= param to bias results."""
        nominatim_result = [{"lat": "30.2672", "lon": "-97.7431", "display_name": "78701, Austin, Texas, USA"}]
        mock_urlopen = mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value="America/Chicago")

        result = geocoding.geocode_location("78701", country_code="US")

        assert result["lat"] == "30.2672"
        # Verify the URL included countrycodes=us
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "countrycodes=us" in request_obj.full_url

    def test_no_country_code_omits_param(self, mocker):
        """Without country_code, no countrycodes param should be in URL."""
        nominatim_result = [{"lat": "30.0", "lon": "-97.0", "display_name": "Somewhere"}]
        mock_urlopen = mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value=None)

        geocoding.geocode_location("somewhere")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "countrycodes" not in request_obj.full_url

    def test_addressdetails_param_always_present(self, mocker):
        """#337 A16: addressdetails=1 must always be in the URL so the
        country_code field is populated for the UNITS-flip logic."""
        nominatim_result = [{"lat": "0", "lon": "0", "display_name": "Anywhere"}]
        mock_urlopen = mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value=None)

        geocoding.geocode_location("anywhere")
        url1 = mock_urlopen.call_args[0][0].full_url
        assert "addressdetails=1" in url1, "addressdetails=1 is required for country-change UNITS rule"

        geocoding.geocode_location("anywhere", country_code="US")
        url2 = mock_urlopen.call_args[0][0].full_url
        assert "addressdetails=1" in url2
        assert "countrycodes=us" in url2

    def test_country_code_extracted_from_address(self, mocker):
        """#337 A16: parse the country_code field from Nominatim's address
        sub-dict. Uppercase canonicalisation matches ip_geolocate."""
        nominatim_result = [
            {
                "lat": "51.5",
                "lon": "-0.1",
                "display_name": "Buckingham Palace, London, England",
                "address": {"country_code": "gb", "country": "United Kingdom"},
            }
        ]
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value="Europe/London")

        result = geocoding.geocode_location("SW1A 1AA")
        assert result["country_code"] == "GB"

    def test_country_code_none_when_address_missing(self, mocker):
        """Pre-A16 Nominatim responses (or responses without addressdetails)
        leave country_code as None — callers handle gracefully."""
        nominatim_result = [{"lat": "30", "lon": "-97", "display_name": "Somewhere"}]
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(nominatim_result))
        mocker.patch("geocoding.timezone_from_coords", return_value=None)

        result = geocoding.geocode_location("somewhere")
        assert result["country_code"] is None


# ── Country-bias coverage for UK + India + US (#337 T8) ────────────────────


class TestCountryBiasCoverage:
    """Pin the countrycodes= URL contract across the three countries called
    out in #337's locked plan. Test cases use mocked Nominatim responses
    shaped to match what the real service returns (verified against
    nominatim.openstreetmap.org responses 2026-06)."""

    def _stub_nominatim(self, mocker, lat, lon, display_name, country_code):
        result = [
            {
                "lat": lat,
                "lon": lon,
                "display_name": display_name,
                "address": {"country_code": country_code.lower()},
            }
        ]
        mock = mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(result))
        mocker.patch("geocoding.timezone_from_coords", return_value=None)
        return mock

    # ── UK ──────────────────────────────────────────────────────────────

    def test_uk_postcode_with_country_bias(self, mocker):
        """SW1A 1AA (Buckingham Palace) resolves with countrycodes=gb."""
        mock = self._stub_nominatim(mocker, "51.5", "-0.14", "Buckingham Palace, London, England, SW1A 1AA, UK", "gb")
        result = geocoding.geocode_location("SW1A 1AA", country_code="GB")
        assert result["country_code"] == "GB"
        assert "countrycodes=gb" in mock.call_args[0][0].full_url

    def test_uk_city_manchester(self, mocker):
        self._stub_nominatim(mocker, "53.48", "-2.24", "Manchester, England, UK", "gb")
        result = geocoding.geocode_location("Manchester", country_code="GB")
        assert result["country_code"] == "GB"

    def test_uk_edinburgh_scotland(self, mocker):
        self._stub_nominatim(mocker, "55.95", "-3.19", "Edinburgh, Scotland, UK", "gb")
        result = geocoding.geocode_location("Edinburgh, Scotland", country_code="GB")
        assert result["country_code"] == "GB"

    # ── India ───────────────────────────────────────────────────────────

    def test_india_city_mumbai(self, mocker):
        self._stub_nominatim(mocker, "19.07", "72.88", "Mumbai, Maharashtra, India", "in")
        result = geocoding.geocode_location("Mumbai", country_code="IN")
        assert result["country_code"] == "IN"

    def test_india_delhi_neighbourhood(self, mocker):
        self._stub_nominatim(
            mocker,
            "28.63",
            "77.22",
            "Connaught Place, Delhi, India",
            "in",
        )
        result = geocoding.geocode_location("Connaught Place, Delhi", country_code="IN")
        assert result["country_code"] == "IN"

    def test_india_pin_code_bangalore(self, mocker):
        """560001 = Bangalore central PIN. Indian PIN codes have known patchy
        coverage in Nominatim, but this PIN resolves consistently. UI hint
        should still recommend 'city + state' as the most reliable form."""
        self._stub_nominatim(mocker, "12.97", "77.59", "560001, Bangalore, Karnataka, India", "in")
        result = geocoding.geocode_location("560001", country_code="IN")
        assert result["country_code"] == "IN"

    # ── US (regression baseline) ────────────────────────────────────────

    def test_us_zip_78701(self, mocker):
        """Austin, TX. Regression baseline for US-zip country biasing."""
        self._stub_nominatim(mocker, "30.27", "-97.74", "78701, Austin, Travis County, Texas, USA", "us")
        result = geocoding.geocode_location("78701", country_code="US")
        assert result["country_code"] == "US"

    def test_us_city_austin_tx(self, mocker):
        self._stub_nominatim(mocker, "30.27", "-97.74", "Austin, Travis County, Texas, USA", "us")
        result = geocoding.geocode_location("Austin, TX", country_code="US")
        assert result["country_code"] == "US"

    def test_us_manhattan_ny(self, mocker):
        self._stub_nominatim(mocker, "40.78", "-73.97", "Manhattan, New York, USA", "us")
        result = geocoding.geocode_location("Manhattan, NY", country_code="US")
        assert result["country_code"] == "US"

    # ── Worldwide (no bias) for cross-country typing ────────────────────

    def test_uk_postcode_without_bias_still_resolves(self, mocker):
        """A5 + worldwide checkbox: US user typing a UK postcode should
        resolve when worldwide is checked (no countrycodes filter)."""
        mock = self._stub_nominatim(mocker, "51.5", "-0.14", "Buckingham Palace, London, England, SW1A 1AA, UK", "gb")
        result = geocoding.geocode_location("SW1A 1AA")  # no country_code
        assert result["country_code"] == "GB"
        assert "countrycodes" not in mock.call_args[0][0].full_url


# ── ip_geolocate ──────────────────────────────────────────────────


class TestIpGeolocate:
    def test_successful_lookup(self, mocker):
        ip_data = {
            "lat": 30.2672,
            "lon": -97.7431,
            "city": "Austin",
            "regionName": "Texas",
            "country": "United States",
            "countryCode": "US",
            "timezone": "America/Chicago",
        }
        mocker.patch("geocoding.urllib.request.urlopen", return_value=_mock_urlopen(ip_data))

        result = geocoding.ip_geolocate()

        assert result["lat"] == "30.2672"
        assert result["lon"] == "-97.7431"
        assert result["city"] == "Austin, Texas"
        assert result["country_code"] == "US"
        assert result["timezone"] == "America/Chicago"

    def test_network_failure(self, mocker):
        mocker.patch("geocoding.urllib.request.urlopen", side_effect=URLError("Connection refused"))

        result = geocoding.ip_geolocate()

        assert result is None

    def test_invalid_json(self, mocker):
        mock_response = MagicMock()
        mock_response.read.return_value = b"not valid json"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mocker.patch("geocoding.urllib.request.urlopen", return_value=mock_response)

        result = geocoding.ip_geolocate()

        assert result is None


# ── timezone_from_coords ──────────────────────────────────────────


class TestTimezoneFromCoords:
    def setup_method(self):
        # Reset cached TimezoneFinder between tests
        geocoding._tf = None

    def test_known_location(self, mocker):
        mock_tf = MagicMock()
        mock_tf.timezone_at.return_value = "America/Chicago"
        mocker.patch("geocoding._get_tf", return_value=mock_tf)

        result = geocoding.timezone_from_coords("30.2672", "-97.7431")

        assert result == "America/Chicago"
        mock_tf.timezone_at.assert_called_once_with(lat=30.2672, lng=-97.7431)

    def test_ocean_location(self, mocker):
        mock_tf = MagicMock()
        mock_tf.timezone_at.return_value = None
        mocker.patch("geocoding._get_tf", return_value=mock_tf)

        result = geocoding.timezone_from_coords("0.0", "0.0")

        assert result is None

    def test_import_error(self, mocker):
        mocker.patch("geocoding._get_tf", side_effect=ImportError("No module named 'timezonefinder'"))

        result = geocoding.timezone_from_coords("30.2672", "-97.7431")

        assert result is None


# ── LOCATION_ENV_KEYS (EPIC #383 shared writer contract) ──────────────


class TestLocationEnvKeys:
    """T2: the four-tuple constant is the canonical writer key list. Both
    setup_server._update_env_location and control_server's settings PATCH
    route reference it. The order isn't load-bearing for callers but a
    silent drift between the constant and what writers actually emit would
    surface as a regression on the all-or-none coherence guard in
    control_server/routes/settings.py."""

    def test_constant_is_four_tuple(self):
        assert isinstance(geocoding.LOCATION_ENV_KEYS, tuple)
        assert len(geocoding.LOCATION_ENV_KEYS) == 4

    def test_constant_contains_expected_keys(self):
        assert "WEATHER_LATITUDE" in geocoding.LOCATION_ENV_KEYS
        assert "WEATHER_LONGITUDE" in geocoding.LOCATION_ENV_KEYS
        assert "WEATHER_LOCATION_NAME" in geocoding.LOCATION_ENV_KEYS
        assert "WEATHER_UNITS" in geocoding.LOCATION_ENV_KEYS

    def test_triplet_prefix_matches_settings_route_subset(self):
        """control_server/routes/settings.py:515 uses the first three keys
        as the all-or-none coherence triplet (lat/lon/name). If the order
        in the constant ever changes, that triplet must be re-derived."""
        triplet = ("WEATHER_LATITUDE", "WEATHER_LONGITUDE", "WEATHER_LOCATION_NAME")
        assert geocoding.LOCATION_ENV_KEYS[:3] == triplet


# ── set_system_timezone: routes through the root-owned wrapper (#387) ──


class TestSetSystemTimezoneWrapper:
    """The arbitrary-tz path must sudo the fixed-path wrapper, never
    `timedatectl set-timezone <tz>` directly — sudoers/020 only authorizes the
    wrapper (a `set-timezone *` glob would be a privilege hole once 010 drops)."""

    def _patch_subprocess(self, mocker, *, valid_tz="America/Chicago"):
        calls = []

        def fake_run(argv, *a, **k):
            calls.append(argv)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            # First call = validation `timedatectl list-timezones`.
            if argv[:2] == ["timedatectl", "list-timezones"]:
                result.stdout = f"UTC\n{valid_tz}\nEurope/London\n"
            else:
                result.stdout = ""
            return result

        mocker.patch("geocoding.subprocess.run", side_effect=fake_run)
        return calls

    def test_calls_wrapper_not_timedatectl_directly(self, mocker):
        calls = self._patch_subprocess(mocker)
        ok, err = geocoding.set_system_timezone("America/Chicago")
        assert ok is True and err is None
        # The privileged call must be the wrapper via sudo, with the tz as argv.
        priv = [c for c in calls if c and c[0] == "sudo"]
        assert priv == [["sudo", "/usr/local/lib/litclock/litclock-set-timezone", "America/Chicago"]], (
            f"unexpected privileged argv: {priv}"
        )
        # And it must NOT sudo timedatectl set-timezone directly.
        assert not any("set-timezone" in c for c in calls if c and c[0] == "sudo")

    def test_invalid_tz_never_reaches_sudo(self, mocker):
        calls = self._patch_subprocess(mocker)
        ok, err = geocoding.set_system_timezone("Moon/Base")
        assert ok is False and "Invalid timezone" in err
        assert not any(c and c[0] == "sudo" for c in calls), "must not sudo an invalid tz"

    def test_wrapper_rejection_is_surfaced(self, mocker):
        """The wrapper is the security boundary; when it rejects/fails (non-zero),
        the caller MUST propagate that, not silently claim success."""

        def fake_run(argv, *a, **k):
            result = MagicMock()
            if argv[:2] == ["timedatectl", "list-timezones"]:
                result.returncode = 0
                result.stdout = "UTC\nAmerica/Chicago\n"
                result.stderr = ""
            else:  # the sudo-wrapper call fails
                result.returncode = 3
                result.stdout = ""
                result.stderr = "litclock-set-timezone: unknown timezone"
            return result

        mocker.patch("geocoding.subprocess.run", side_effect=fake_run)
        ok, err = geocoding.set_system_timezone("America/Chicago")
        assert ok is False
        assert "Failed to set timezone" in err and "unknown timezone" in err

    def test_missing_wrapper_is_surfaced(self, mocker):
        """If the wrapper binary is absent, set_system_timezone must return a
        clean (False, msg), not raise."""
        mocker.patch(
            "geocoding.subprocess.run",
            side_effect=[
                MagicMock(returncode=0, stdout="UTC\nAmerica/Chicago\n", stderr=""),
                FileNotFoundError("no wrapper"),
            ],
        )
        ok, err = geocoding.set_system_timezone("America/Chicago")
        assert ok is False and err
