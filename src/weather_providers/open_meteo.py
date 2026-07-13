import logging

from weather_providers.base_provider import BaseWeatherProvider

# WMO Weather interpretation codes
# Reference: https://open-meteo.com/en/docs
WMO_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Light rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Moderate showers",
    82: "Violent showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with light hail",
    99: "Thunderstorm with heavy hail",
}


class OpenMeteo(BaseWeatherProvider):
    _cache_prefix = "weather-cache-openmeteo"

    def __init__(self, location_lat, location_long, units):
        self.location_lat = location_lat
        self.location_long = location_long
        self.units = units

    def get_icon_from_wmo_code(self, code, is_daytime):
        """Map WMO weather code to a local icon name."""

        icon_map_day = {
            0: "sun",
            1: "cloud_sun",
            2: "cloud_sun",
            3: "clouds",
            45: "cloud",
            48: "cloud",
            51: "rain0",
            53: "rain0",
            55: "rain0",
            56: "rain0",
            57: "rain0",
            61: "rain0",
            63: "rain1",
            65: "rain2",
            66: "rain1",
            67: "rain2",
            71: "snow",
            73: "snow",
            75: "snow",
            77: "snow",
            80: "rain0_sun",
            81: "rain1_sun",
            82: "rain2",
            85: "snow_sun",
            86: "snow",
            95: "lightning",
            96: "rain_lightning",
            99: "rain_lightning",
        }

        icon_map_night = {
            0: "moon",
            1: "cloud_moon",
            2: "cloud_moon",
            3: "clouds",
            45: "cloud",
            48: "cloud",
            51: "rain0",
            53: "rain0",
            55: "rain0",
            56: "rain0",
            57: "rain0",
            61: "rain0",
            63: "rain1",
            65: "rain2",
            66: "rain1",
            67: "rain2",
            71: "snow",
            73: "snow",
            75: "snow",
            77: "snow",
            80: "rain1_moon",
            81: "rain1_moon",
            82: "rain2",
            85: "snow_moon",
            86: "snow",
            95: "lightning",
            96: "rain_lightning",
            99: "rain_lightning",
        }

        icon_map = icon_map_day if is_daytime else icon_map_night

        if code not in icon_map:
            logging.warning(f"Unknown WMO code: {code}, using cloud icon")
            return "cloud"

        icon = icon_map[code]
        logging.debug(f"get_icon_from_wmo_code({code}, {is_daytime}) - {icon}")
        return icon

    def get_weather(self):
        temp_unit = "fahrenheit" if self.units == "imperial" else "celsius"
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.location_lat}&longitude={self.location_long}"
            f"&current=temperature_2m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit={temp_unit}&timezone=auto"
        )
        response_data = self.get_response_data(url)
        logging.debug(response_data)

        weather_code = int(response_data["current"]["weather_code"])
        temp_max = response_data["daily"]["temperature_2m_max"][0]
        temp_min = response_data["daily"]["temperature_2m_min"][0]

        description = WMO_DESCRIPTIONS.get(weather_code, "Unknown")
        is_day = self.is_daytime(self.location_lat, self.location_long)

        return {
            "temperatureMin": temp_min,
            "temperatureMax": temp_max,
            "icon": self.get_icon_from_wmo_code(weather_code, is_day),
            "description": description,
        }
