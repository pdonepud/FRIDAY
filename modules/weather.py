"""
weather.py — FRIDAY's weather sense.

Wraps the Open-Meteo API (free, no key required). Other modules use this
for context-aware reminders ("it's raining, leave early") and weather-reactive
music playlists.

Test directly:
    python modules/weather.py
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import LATITUDE, LONGITUDE


logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SECONDS = 15

# Number of hourly cells to expose in the API response. Open-Meteo returns
# up to 168 hours; the UI strip shows the next 12 to keep the panel a
# manageable width.
_HOURLY_WINDOW = 12

# Real-world UTC offsets max out at ±14h (Kiribati at +14, Baker Island at
# -12; Samoa was UTC+14 pre-2011). Anything outside that means Open-Meteo
# returned a corrupted value — clamp before feeding it to timedelta(), which
# OverflowErrors on absurd magnitudes like 10**12 seconds.
_MAX_UTC_OFFSET_SECONDS = 14 * 3600


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
        "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
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
        # `hourly` is optional at this layer — unlike current/daily, every
        # other field on the response is independent of it. Missing/malformed
        # hourly should degrade to an empty strip, not 503 the whole endpoint.
        # The `or {}` (rather than checking for None only) also covers
        # non-dict values like null, [], or a stray string — _slice_next_hours's
        # KeyError guard then returns [] cleanly.
        hourly_raw = data.get("hourly")
        hourly_block = hourly_raw if isinstance(hourly_raw, dict) else {}
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
            "hourly":              _slice_next_hours(
                hourly_block,
                _HOURLY_WINDOW,
                utc_offset_seconds=data.get("utc_offset_seconds"),
            ),
        }
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(
            f"Malformed Open-Meteo response: {e}. Body: {response.text[:300]}"
        )


def _slice_next_hours(
    hourly_block: dict,
    window: int,
    utc_offset_seconds: Optional[int] = None,
) -> List[dict]:
    """
    Take the parallel arrays Open-Meteo returns under `hourly` and reshape
    them into a list of dicts, dropping any hours that already passed
    (Open-Meteo returns from the start of today, not the current hour).

    `utc_offset_seconds` is the offset of the forecast location, taken from
    the top-level Open-Meteo response. It's needed to align "now" with the
    forecast's naive-local timestamps when the server isn't in the same
    timezone as the forecast location.

    Falls back to an empty list rather than raising if the shape is off —
    the endpoint's outer error path already covers total failure, and a
    graceful degrade here means the current-conditions display still ships
    even if the hourly block is malformed for one call.
    """
    try:
        times = hourly_block["time"]
        temps = hourly_block["temperature_2m"]
        codes = hourly_block["weather_code"]
        precip = hourly_block["precipitation_probability"]
        is_days = hourly_block["is_day"]
    except KeyError:
        return []

    # Guard against parallel-array corruption: any of the five coming back
    # as a non-list (TypeError on later index) or with mismatched lengths
    # (IndexError on later index) would 503 the whole /api/weather call.
    # Degrade to [] instead so the current-conditions display still ships.
    arrays = (times, temps, codes, precip, is_days)
    if not all(isinstance(a, list) for a in arrays):
        return []
    if len({len(a) for a in arrays}) != 1:
        return []

    # Open-Meteo timestamps are naive-local ISO strings (no offset) when
    # timezone=auto is set — local to the FORECAST LOCATION, not the server.
    # Build a naive "now" in that same timezone using the offset the API
    # returned. If the field is missing, fall back to server-local time
    # with a warning (works today because dev machine == Santa Cruz TZ, but
    # would silently misalign if the server ran elsewhere).
    if (
        isinstance(utc_offset_seconds, int)
        and not isinstance(utc_offset_seconds, bool)
        and abs(utc_offset_seconds) <= _MAX_UTC_OFFSET_SECONDS
    ):
        now_local = datetime.now(timezone.utc) + timedelta(seconds=utc_offset_seconds)
        now_hour = now_local.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    else:
        logger.warning(
            "Open-Meteo utc_offset_seconds missing or out of range (got %r); "
            "falling back to server-local time for hourly alignment.",
            utc_offset_seconds,
        )
        now_hour = datetime.now().replace(minute=0, second=0, microsecond=0)

    # Use a None sentinel so "no future slot found" is explicit and can't be
    # confused with "found at index 0" — the old default of start_idx=0 meant
    # a stale forecast (all timestamps in the past) would leak past hours to
    # the UI. Return [] in that case; current-conditions still ship.
    start_idx: Optional[int] = None
    for i, iso in enumerate(times):
        try:
            slot = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        # timezone=auto is contracted to return naive-local strings, but
        # fromisoformat also accepts trailing offsets ("+00:00", or a "Z"
        # suffix on 3.11+) and produces a tz-aware datetime for those. A
        # naive-vs-aware `>=` on the next line would TypeError and escape
        # the fromisoformat guard above. Strip tzinfo if present — trusts
        # any offset matches the forecast tz (safer than a translate that
        # would silently misalign if it didn't).
        if slot.tzinfo is not None:
            slot = slot.replace(tzinfo=None)
        if slot >= now_hour:
            start_idx = i
            break

    if start_idx is None:
        return []

    slice_end = min(start_idx + window, len(times))
    out: List[dict] = []
    for i in range(start_idx, slice_end):
        # Defense-in-depth for per-row value corruption: outer guards catch
        # shape issues (non-list, mismatched lengths) but a single non-numeric
        # temp or a non-bool-coercible is_day would still crash inside the
        # dict build. Skip the bad row, keep the rest of the strip.
        try:
            out.append({
                "time":                      times[i],
                "temp_f":                    temps[i],
                "weather_code":              codes[i],
                "precipitation_probability": precip[i] if precip[i] is not None else 0,
                "is_day":                    bool(is_days[i]),
            })
        except (TypeError, ValueError):
            continue
    return out


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
