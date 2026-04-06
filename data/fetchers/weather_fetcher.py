"""
Weather data fetcher — Open-Meteo API.

Open-Meteo is free, requires no API key, and updates every 6h.
Base URL: https://api.open-meteo.com/v1/forecast

Layer structure (pure functions separated from I/O):
  fetch_daily_max_forecast()  — I/O: fetches raw JSON from Open-Meteo
  parse_daily_max()           — pure: extracts daily max temp from raw JSON
  parse_weather_buckets()     — pure: extracts (lo, hi, label) from market title
  build_weather_forecast()    — pure: constructs WeatherForecast schema
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

from config.schemas import WeatherBucket, WeatherForecast


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Regex patterns for extracting temperature buckets from Polymarket market questions.
# Polymarket weather market titles follow patterns like:
#   "Will the high temperature in NYC be above 27°C on April 7?"
#   "Will NYC reach 80°F or higher on April 7?"
#   "What will be NYC's high temp on April 8? Under 70°F / 70-75°F / 75-80°F / Over 80°F"
_CELSIUS_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*°C", re.IGNORECASE)
_FAHRENHEIT_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*°F", re.IGNORECASE)
_RANGE_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°([CF])", re.IGNORECASE
)


# ── I/O ───────────────────────────────────────────────────────────────────────

def fetch_daily_max_forecast(
    lat: float,
    lon: float,
    forecast_date: date,
    http_client: Any,
) -> dict:
    """
    Fetch hourly temperature forecast from Open-Meteo for a location.

    Args:
        lat:           Latitude of the location.
        lon:           Longitude of the location.
        forecast_date: The date we want the daily max for.
        http_client:   requests.Session (or compatible) instance.

    Returns:
        Raw JSON response dict from Open-Meteo.

    Raises:
        requests.HTTPError: if the API returns a non-2xx status.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "forecast_days": 3,    # buffer so forecast_date is always in range
        "timezone": "UTC",
    }
    response = http_client.get(OPEN_METEO_URL, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


# ── Pure functions ────────────────────────────────────────────────────────────

def parse_daily_max(raw: dict, target_date: date) -> float:
    """
    Extract the maximum temperature for target_date from Open-Meteo hourly response.

    Args:
        raw:         Raw JSON from Open-Meteo API.
        target_date: The date we want the daily max for (UTC).

    Returns:
        Maximum temperature in Celsius for target_date.

    Raises:
        ValueError: if no hourly data found for target_date.
        KeyError:   if response schema is unexpected.
    """
    times: list[str] = raw["hourly"]["time"]
    temps: list[float | None] = raw["hourly"]["temperature_2m"]
    prefix = target_date.isoformat()

    day_temps = [
        t for ts, t in zip(times, temps)
        if ts.startswith(prefix) and t is not None
    ]

    if not day_temps:
        raise ValueError(
            f"No temperature data for {target_date} in Open-Meteo response"
        )

    return max(day_temps)


def _fahrenheit_to_celsius(f: float) -> float:
    """Pure conversion."""
    return (f - 32.0) * 5.0 / 9.0


def parse_weather_buckets(
    question: str,
) -> list[tuple[float, float, str]]:
    """
    Extract temperature bucket boundaries from a Polymarket market question.

    Returns a list of (lo_celsius, hi_celsius, label) tuples, sorted by lo ascending.
    The last bucket always has hi = math.inf.

    Handles three market formats:
    1. Single threshold — "above 27°C" → [(27.0, inf, "27°C or higher")]
    2. Range buckets   — "70-75°F / 75-80°F / over 80°F"
                         → [(21.1, 23.9, "70-75°F"), (23.9, 26.7, "75-80°F"),
                            (26.7, inf, "80°F or higher")]
    3. Exact temp      — "Will NYC be exactly 27°C?" → [(27.0, inf, "27°C or higher")]

    Args:
        question: Raw market question string from Polymarket.

    Returns:
        List of (lo, hi, label) tuples. Empty list if no temperature found.
    """
    unit = "C"

    # Detect unit
    if _FAHRENHEIT_PATTERN.search(question) and not _CELSIUS_PATTERN.search(question):
        unit = "F"

    # Try range buckets first (multi-bucket ladder markets)
    ranges = _RANGE_PATTERN.findall(question)
    if ranges:
        buckets: list[tuple[float, float, str]] = []
        for lo_str, hi_str, u in ranges:
            lo_val = float(lo_str)
            hi_val = float(hi_str)
            if u.upper() == "F":
                lo_val = _fahrenheit_to_celsius(lo_val)
                hi_val = _fahrenheit_to_celsius(hi_val)
            lo_val, hi_val = min(lo_val, hi_val), max(lo_val, hi_val)
            label = f"{lo_str}–{hi_str}°{u.upper()}"
            buckets.append((lo_val, hi_val, label))

        # Sort and make last bucket open-ended
        buckets.sort(key=lambda b: b[0])
        if buckets:
            last_lo, _, last_label = buckets[-1]
            buckets[-1] = (last_lo, math.inf, last_label + " or higher")
        return buckets

    # Single threshold market — extract the one temperature
    pattern = _CELSIUS_PATTERN if unit == "C" else _FAHRENHEIT_PATTERN
    matches = pattern.findall(question)
    if not matches:
        return []

    temp_val = float(matches[0])
    if unit == "F":
        temp_celsius = _fahrenheit_to_celsius(temp_val)
        label = f"{matches[0]}°F or higher"
    else:
        temp_celsius = temp_val
        label = f"{matches[0]}°C or higher"

    return [(temp_celsius, math.inf, label)]


def build_weather_forecast(
    city: str,
    lat: float,
    lon: float,
    forecast_date: date,
    raw_api_response: dict,
    sigma_celsius: float,
    source: str = "open_meteo",
) -> WeatherForecast:
    """
    Pure function. Build a WeatherForecast from raw Open-Meteo API data.
    Buckets are empty — filled separately by the strategy.

    Args:
        city:             City name.
        lat:              Latitude.
        lon:              Longitude.
        forecast_date:    Target date.
        raw_api_response: Raw JSON from fetch_daily_max_forecast().
        sigma_celsius:    Forecast uncertainty in °C.
        source:           Data source identifier.

    Returns:
        WeatherForecast with point_forecast_celsius set and buckets=[].
    """
    point_forecast = parse_daily_max(raw_api_response, forecast_date)
    return WeatherForecast(
        city=city,
        lat=lat,
        lon=lon,
        forecast_date=forecast_date,
        point_forecast_celsius=point_forecast,
        sigma_celsius=sigma_celsius,
        source=source,
        fetched_at=datetime.now(timezone.utc),
        buckets=[],
    )
