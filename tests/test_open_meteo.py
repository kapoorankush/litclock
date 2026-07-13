"""Tests for Open-Meteo weather provider."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# Stub out hardware-specific dependencies before importing the provider
for mod_name in ("pytz", "astral", "astral.sun"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock() if mod_name != "pytz" else ModuleType(mod_name)

import pytest  # noqa: E402

from weather_providers.open_meteo import WMO_DESCRIPTIONS, OpenMeteo  # noqa: E402

# ── WMO code → icon mapping ───────────────────────────────────────


class TestWmoCodeToIcon:
    @pytest.fixture
    def provider(self):
        return OpenMeteo("0", "0", "metric")

    # Day icons
    def test_clear_day(self, provider):
        assert provider.get_icon_from_wmo_code(0, True) == "sun"

    def test_mainly_clear_day(self, provider):
        assert provider.get_icon_from_wmo_code(1, True) == "cloud_sun"

    def test_partly_cloudy_day(self, provider):
        assert provider.get_icon_from_wmo_code(2, True) == "cloud_sun"

    def test_overcast_day(self, provider):
        assert provider.get_icon_from_wmo_code(3, True) == "clouds"

    def test_fog_day(self, provider):
        assert provider.get_icon_from_wmo_code(45, True) == "cloud"

    def test_rime_fog_day(self, provider):
        assert provider.get_icon_from_wmo_code(48, True) == "cloud"

    def test_light_drizzle_day(self, provider):
        assert provider.get_icon_from_wmo_code(51, True) == "rain0"

    def test_moderate_drizzle_day(self, provider):
        assert provider.get_icon_from_wmo_code(53, True) == "rain0"

    def test_dense_drizzle_day(self, provider):
        assert provider.get_icon_from_wmo_code(55, True) == "rain0"

    def test_light_freezing_drizzle_day(self, provider):
        assert provider.get_icon_from_wmo_code(56, True) == "rain0"

    def test_dense_freezing_drizzle_day(self, provider):
        assert provider.get_icon_from_wmo_code(57, True) == "rain0"

    def test_light_rain_day(self, provider):
        assert provider.get_icon_from_wmo_code(61, True) == "rain0"

    def test_moderate_rain_day(self, provider):
        assert provider.get_icon_from_wmo_code(63, True) == "rain1"

    def test_heavy_rain_day(self, provider):
        assert provider.get_icon_from_wmo_code(65, True) == "rain2"

    def test_light_freezing_rain_day(self, provider):
        assert provider.get_icon_from_wmo_code(66, True) == "rain1"

    def test_heavy_freezing_rain_day(self, provider):
        assert provider.get_icon_from_wmo_code(67, True) == "rain2"

    def test_snow_day(self, provider):
        for code in [71, 73, 75, 77]:
            assert provider.get_icon_from_wmo_code(code, True) == "snow"

    def test_light_showers_day(self, provider):
        assert provider.get_icon_from_wmo_code(80, True) == "rain0_sun"

    def test_moderate_showers_day(self, provider):
        assert provider.get_icon_from_wmo_code(81, True) == "rain1_sun"

    def test_violent_showers_day(self, provider):
        assert provider.get_icon_from_wmo_code(82, True) == "rain2"

    def test_light_snow_showers_day(self, provider):
        assert provider.get_icon_from_wmo_code(85, True) == "snow_sun"

    def test_heavy_snow_showers_day(self, provider):
        assert provider.get_icon_from_wmo_code(86, True) == "snow"

    def test_thunderstorm_day(self, provider):
        assert provider.get_icon_from_wmo_code(95, True) == "lightning"

    def test_thunderstorm_hail_day(self, provider):
        assert provider.get_icon_from_wmo_code(96, True) == "rain_lightning"
        assert provider.get_icon_from_wmo_code(99, True) == "rain_lightning"

    # Night icons
    def test_clear_night(self, provider):
        assert provider.get_icon_from_wmo_code(0, False) == "moon"

    def test_mainly_clear_night(self, provider):
        assert provider.get_icon_from_wmo_code(1, False) == "cloud_moon"

    def test_partly_cloudy_night(self, provider):
        assert provider.get_icon_from_wmo_code(2, False) == "cloud_moon"

    def test_overcast_night(self, provider):
        assert provider.get_icon_from_wmo_code(3, False) == "clouds"

    def test_light_showers_night(self, provider):
        assert provider.get_icon_from_wmo_code(80, False) == "rain1_moon"

    def test_moderate_showers_night(self, provider):
        assert provider.get_icon_from_wmo_code(81, False) == "rain1_moon"

    def test_light_snow_showers_night(self, provider):
        assert provider.get_icon_from_wmo_code(85, False) == "snow_moon"

    def test_thunderstorm_night(self, provider):
        assert provider.get_icon_from_wmo_code(95, False) == "lightning"

    # Unknown code fallback
    def test_unknown_code_falls_back_to_cloud(self, provider):
        assert provider.get_icon_from_wmo_code(999, True) == "cloud"
        assert provider.get_icon_from_wmo_code(999, False) == "cloud"


# ── WMO description mapping ───────────────────────────────────────


class TestWmoDescription:
    def test_clear_sky(self):
        assert WMO_DESCRIPTIONS[0] == "Clear sky"

    def test_fog(self):
        assert WMO_DESCRIPTIONS[45] == "Fog"

    def test_heavy_rain(self):
        assert WMO_DESCRIPTIONS[65] == "Heavy rain"

    def test_thunderstorm(self):
        assert WMO_DESCRIPTIONS[95] == "Thunderstorm"

    def test_all_codes_have_descriptions(self):
        expected_codes = {
            0,
            1,
            2,
            3,
            45,
            48,
            51,
            53,
            55,
            56,
            57,
            61,
            63,
            65,
            66,
            67,
            71,
            73,
            75,
            77,
            80,
            81,
            82,
            85,
            86,
            95,
            96,
            99,
        }
        assert set(WMO_DESCRIPTIONS.keys()) == expected_codes


# ── Temperature unit in URL ────────────────────────────────────────


class TestTemperatureUnit:
    def test_imperial_uses_fahrenheit(self, mocker):
        provider = OpenMeteo("40.7", "-74.0", "imperial")
        mock_get = mocker.patch.object(
            provider,
            "get_response_data",
            return_value={
                "current": {"weather_code": 0, "temperature_2m": 72.0},
                "daily": {"temperature_2m_max": [80.0], "temperature_2m_min": [65.0]},
            },
        )
        mocker.patch.object(provider, "is_daytime", return_value=True)
        provider.get_weather()
        url = mock_get.call_args[0][0]
        assert "temperature_unit=fahrenheit" in url

    def test_metric_uses_celsius(self, mocker):
        provider = OpenMeteo("40.7", "-74.0", "metric")
        mock_get = mocker.patch.object(
            provider,
            "get_response_data",
            return_value={
                "current": {"weather_code": 0, "temperature_2m": 22.0},
                "daily": {"temperature_2m_max": [27.0], "temperature_2m_min": [18.0]},
            },
        )
        mocker.patch.object(provider, "is_daytime", return_value=True)
        provider.get_weather()
        url = mock_get.call_args[0][0]
        assert "temperature_unit=celsius" in url


# ── get_weather response parsing ───────────────────────────────────


class TestGetWeather:
    @pytest.fixture
    def mock_response(self):
        return {
            "current": {
                "time": "2026-03-28T14:00",
                "interval": 900,
                "temperature_2m": 15.2,
                "weather_code": 3,
            },
            "daily": {
                "time": ["2026-03-28"],
                "temperature_2m_max": [18.5],
                "temperature_2m_min": [7.3],
            },
        }

    def test_parses_response(self, mocker, mock_response):
        provider = OpenMeteo("51.5", "-0.1", "metric")
        mocker.patch.object(provider, "get_response_data", return_value=mock_response)
        mocker.patch.object(provider, "is_daytime", return_value=True)

        result = provider.get_weather()

        assert result["temperatureMax"] == 18.5
        assert result["temperatureMin"] == 7.3
        assert result["icon"] == "clouds"
        assert result["description"] == "Overcast"

    def test_night_icon(self, mocker, mock_response):
        mock_response["current"]["weather_code"] = 0
        provider = OpenMeteo("51.5", "-0.1", "metric")
        mocker.patch.object(provider, "get_response_data", return_value=mock_response)
        mocker.patch.object(provider, "is_daytime", return_value=False)

        result = provider.get_weather()
        assert result["icon"] == "moon"

    def test_unknown_weather_code(self, mocker, mock_response):
        mock_response["current"]["weather_code"] = 999
        provider = OpenMeteo("51.5", "-0.1", "metric")
        mocker.patch.object(provider, "get_response_data", return_value=mock_response)
        mocker.patch.object(provider, "is_daytime", return_value=True)

        result = provider.get_weather()
        assert result["icon"] == "cloud"
        assert result["description"] == "Unknown"

    def test_url_contains_coordinates(self, mocker, mock_response):
        provider = OpenMeteo("40.7128", "-74.0060", "metric")
        mock_get = mocker.patch.object(provider, "get_response_data", return_value=mock_response)
        mocker.patch.object(provider, "is_daytime", return_value=True)

        provider.get_weather()
        url = mock_get.call_args[0][0]
        assert "latitude=40.7128" in url
        assert "longitude=-74.0060" in url

    def test_cache_prefix_is_openmeteo(self):
        """OpenMeteo must write to its own cache-file namespace so its
        responses never collide with OpenWeatherMap's (different schemas).
        Post-#175 refactor: cache path also includes units so a WEATHER_UNITS
        change writes to a fresh file instead of serving stale-unit values."""
        provider_metric = OpenMeteo("51.5", "-0.1", "metric")
        provider_imperial = OpenMeteo("51.5", "-0.1", "imperial")
        assert provider_metric._cache_prefix == "weather-cache-openmeteo"
        # Post-M3 (#245 hardware QA): filename also includes coords so a
        # location change from the Settings tab gets a clean cache miss
        # instead of serving the prior city's cached payload until
        # WEATHER_TTL expires.
        assert provider_metric._cache_file_path().endswith("weather-cache-openmeteo-metric-51.5--0.1.json")
        assert provider_imperial._cache_file_path().endswith("weather-cache-openmeteo-imperial-51.5--0.1.json")
