"""
weather.py — FRIDAY's weather sense.

Wraps the Open-Meteo API (free, no key required). Other modules use this
for context-aware reminders ("it's raining, leave early") and weather-reactive
music playlists.

Test directly:
    python modules/weather.py
"""

import json
import os
import sys
from typing import Dict

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import LATITUDE, LONGITUDE


_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SECONDS = 15


WEATHER_CODES: Dict[int, str] = {
    0:  "clear sky",
    1:  "mainly clear",
    2:  "partly cloudy",
    3:  "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "rain showers",
    81: "heavy rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}


def _describe_code(code: int) -> str:
    """Translate a WMO weather code into a short human description."""
    return WEATHER_CODES.get(code, "unknown")


def get_weather() -> dict:
    """
    Fetch current conditions and a 2-day forecast from Open-Meteo.

    Returns:
        A normalized dict with current conditions and today/tomorrow summaries.

    Raises:
        RuntimeError: If the API call times out, errors, or returns malformed data.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
        "forecast_days": 2,
    }

    try:
        response = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT_SECONDS)
    except requests.Timeout:
        raise RuntimeError(f"Open-Meteo timed out after {_TIMEOUT_SECONDS}s.")
    except requests.RequestException as e:
        raise RuntimeError(f"Open-Meteo request failed: {e}")

    if not response.ok:
        raise RuntimeError(
            f"Open-Meteo HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
        current = data["current"]
        daily = data["daily"]
        return {
            "temp_f":              current["temperature_2m"],
            "feels_like_f":        current["apparent_temperature"],
            "conditions":          _describe_code(current["weather_code"]),
            "humidity":            current["relative_humidity_2m"],
            "wind_mph":            current["wind_speed_10m"],
            "is_day":              bool(current["is_day"]),
            "today_high_f":        daily["temperature_2m_max"][0],
            "today_low_f":         daily["temperature_2m_min"][0],
            "rain_chance_today":   daily["precipitation_probability_max"][0],
            "tomorrow_high_f":     daily["temperature_2m_max"][1],
            "tomorrow_low_f":      daily["temperature_2m_min"][1],
            "tomorrow_conditions": _describe_code(daily["weather_code"][1]),
            "rain_chance_tomorrow": daily["precipitation_probability_max"][1],
        }
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(
            f"Malformed Open-Meteo response: {e}. Body: {response.text[:300]}"
        )


def describe_weather() -> str:
    """
    Return a one-sentence, spoken-friendly weather summary.

    Whole numbers, "percent" instead of "%", natural phrasing.
    """
    w = get_weather()
    return (
        f"It's {round(w['temp_f'])} degrees and {w['conditions']} right now, "
        f"with a {round(w['rain_chance_today'])} percent chance of rain later. "
        f"Tomorrow's looking {w['tomorrow_conditions']}, up to {round(w['tomorrow_high_f'])}."
    )


if __name__ == "__main__":
    weather = get_weather()
    print(json.dumps(weather, indent=2))
    print()
    print(describe_weather())
