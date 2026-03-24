#!/usr/bin/env python3
"""
RivalClaw weather feed — NWS forecast data for weather market fair value.
Free API, no auth needed. Used to price Kalshi temperature contracts.

For "Will DC max temp be >56°?":
  - Get NWS forecast high for DC
  - Fair value = P(actual_max > 56) = Φ((forecast - 56) / forecast_error)
  - Same math as crypto fair value but with temperature
"""
from __future__ import annotations
import time
import requests

# NWS forecast endpoints (gridpoint-based)
# DC: WFO=LWX, gridX=97, gridY=71
# SF: WFO=MTR, gridX=85, gridY=105
NWS_POINTS = {
    "dc": "https://api.weather.gov/gridpoints/LWX/97,71/forecast",
    "sf": "https://api.weather.gov/gridpoints/MTR/85,105/forecast",
    "nyc": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
}

# Map Kalshi series to city
SERIES_TO_CITY = {
    "KXHIGHTDC": "dc",
    "KXHIGHTSFO": "sf",
    "KXTEMPNYCH": "nyc",
}

# Forecast error std dev in °F (same-day NWS forecast accuracy)
FORECAST_ERROR_F = 2.5

_cache: dict = {}
_cache_ts: float = 0
CACHE_TTL_SECONDS = 300  # 5 min — weather doesn't change fast


def get_forecasts() -> dict:
    """
    Returns {city: {"high_f": float, "low_f": float, "current_f": float}}
    from NWS forecast API.
    """
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
        return _cache

    result = {}
    headers = {"User-Agent": "RivalClaw/1.0 (paper-trading-bot)"}

    for city, url in NWS_POINTS.items():
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])
            if not periods:
                continue

            # First period is current/today
            today = periods[0]
            temp = today.get("temperature")
            if temp is None:
                continue

            # NWS gives daytime high or nighttime low depending on time
            is_daytime = today.get("isDaytime", True)
            if is_daytime:
                result[city] = {
                    "high_f": float(temp),
                    "current_f": float(temp),  # Best estimate
                    "forecast_error": FORECAST_ERROR_F,
                }
            else:
                # Nighttime — use next daytime period for high
                if len(periods) > 1:
                    tomorrow = periods[1]
                    high = tomorrow.get("temperature", temp)
                    result[city] = {
                        "high_f": float(high),
                        "current_f": float(temp),
                        "forecast_error": FORECAST_ERROR_F,
                    }
        except Exception as e:
            print(f"[rivalclaw/weather] NWS error for {city}: {e}")

    if result:
        _cache = result
        _cache_ts = time.time()

    return result


def get_city_forecast(city: str) -> dict | None:
    """Get forecast for a specific city."""
    return get_forecasts().get(city)
