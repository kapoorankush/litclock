import logging

from weather_providers.base_provider import BaseWeatherProvider


class OpenWeatherMap(BaseWeatherProvider):
    _cache_prefix = "weather-cache-owm"
    # Pre-refactor: OpenWeatherMap used to write to plain `weather-cache.json`
    # via the old unit-less default cache path, removed in the #175
    # unit-aware-cache refactor. Listed here so the orphan sweep cleans it
    # up on upgrade, otherwise it lingers as dead weight forever.
    _legacy_cache_filenames = ("weather-cache.json",)

    def __init__(self, openweathermap_apikey, location_lat, location_long, units):
        self.openweathermap_apikey = openweathermap_apikey
        self.location_lat = location_lat
        self.location_long = location_long
        self.units = units

    # Map OpenWeatherMap icons to local icons
    # Reference: https://openweathermap.org/weather-conditions
    def get_icon_from_openweathermap_weathercode(self, weathercode, is_daytime):

        icon_dict = {
            200: "lightning",  # thunderstorm with light rain
            201: "lightning",  # thunderstorm with rain
            202: "rain_lightning",  # thunderstorm with heavy rain
            210: "lightning",  # light thunderstorm
            211: "lightning",  # thunderstorm
            212: "rain_lightning",  # heavy thunderstorm
            221: "lightning",  # ragged thunderstorm
            230: "lightning",  # thunderstorm with light drizzle
            231: "lightning",  # thunderstorm with drizzle
            232: "rain_lightning",  # thunderstorm with heavy drizzle
            300: "rain0_sun" if is_daytime else "rain1_moon",  # light intensity drizzle
            301: "rain0_sun" if is_daytime else "rain1_moon",  # drizzle
            302: "rain1_sun" if is_daytime else "rain1_moon",  # heavy intensity drizzle
            310: "rain0_sun" if is_daytime else "rain1_moon",  # light intensity drizzle rain
            311: "rain1_sun" if is_daytime else "rain1_moon",  # drizzle rain
            312: "rain1",  # heavy intensity drizzle rain
            313: "rain1_sun" if is_daytime else "rain1_moon",  # shower rain and drizzle
            314: "rain1" if is_daytime else "rain1_moon",  # heavy shower rain and drizzle
            321: "rain0",  # shower drizzle
            500: "rain0",  # light rain
            501: "rain1",  # moderate rain
            502: "rain2",  # heavy intensity rain
            503: "rain2",  # very heavy rain
            504: "rain2",  # extreme rain
            511: "rain_snow",  # freezing rain
            520: "rain0_sun" if is_daytime else "rain1_moon",  # light intensity shower rain
            521: "rain1_sun" if is_daytime else "rain1_moon",  # shower rain
            522: "rain2",  # heavy intensity shower rain
            531: "rain1",  # ragged shower rain
            600: "snow_sun" if is_daytime else "snow_moon",  # light snow
            601: "snow",  # Snow
            602: "snow",  # Heavy snow
            611: "snow",  # Sleet
            612: "rain_snow",  # Light shower sleet
            613: "rain_snow",  # Shower sleet
            615: "rain_snow",  # Light rain and snow
            616: "rain_snow",  # Rain and snow
            620: "snow_sun" if is_daytime else "snow_moon",  # Light shower snow
            621: "snow",  # Shower snow
            622: "snow",  # Heavy shower snow
            701: "rain0",  # mist
            711: "rain0",  # Smoke
            721: "rain0",  # Haze
            731: "rain0",  # sand/ dust whirls
            741: "rain0",  # fog
            751: "rain0",  # sand
            761: "rain0",  # dust
            762: "rain0",  # volcanic ash
            771: "wind",  # squalls
            781: "wind",  # tornado
            800: "sun" if is_daytime else "moon",  # clear sky
            801: "cloud_sun" if is_daytime else "cloud_moon",  # few clouds: 11-25%
            802: "cloud",  # scattered clouds: 25-50%
            803: "clouds",  # broken clouds: 51-84%
            804: "clouds",  # overcast clouds: 85-100%
        }

        icon = icon_dict[weathercode]
        logging.debug(f"get_icon_by_weathercode({weathercode}, {is_daytime}) - {icon}")

        return icon

    def get_weather(self):
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={self.location_lat}&lon={self.location_long}"
            f"&units={self.units}&appid={self.openweathermap_apikey}"
        )
        response_data = self.get_response_data(url)
        logging.debug(response_data)

        weather_data = response_data["main"]
        weather_description = response_data["weather"][0]
        logging.debug(f"get_weather() - {weather_data}")

        is_day = self.is_daytime(self.location_lat, self.location_long)
        return {
            "temperatureMin": weather_data["temp_min"],
            "temperatureMax": weather_data["temp_max"],
            "icon": self.get_icon_from_openweathermap_weathercode(weather_description["id"], is_day),
            "description": weather_description["description"].title(),
        }
