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
    # Original 9 cities
    "dc": "https://api.weather.gov/gridpoints/LWX/97,71/forecast",
    "sf": "https://api.weather.gov/gridpoints/MTR/85,105/forecast",
    "nyc": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
    "houston": "https://api.weather.gov/gridpoints/HGX/63,95/forecast",
    "boston": "https://api.weather.gov/gridpoints/BOX/71,90/forecast",
    "atlanta": "https://api.weather.gov/gridpoints/FFC/51,87/forecast",
    "dallas": "https://api.weather.gov/gridpoints/FWD/89,104/forecast",
    "phoenix": "https://api.weather.gov/gridpoints/PSR/159,58/forecast",
    "seattle": "https://api.weather.gov/gridpoints/SEW/125,68/forecast",
    # Expansion: 12 new cities (NWS gridpoints verified via points API 2026-03-30)
    "la": "https://api.weather.gov/gridpoints/LOX/155,45/forecast",
    "miami": "https://api.weather.gov/gridpoints/MFL/110,50/forecast",
    "philadelphia": "https://api.weather.gov/gridpoints/PHI/50,76/forecast",
    "chicago": "https://api.weather.gov/gridpoints/LOT/76,73/forecast",
    "austin": "https://api.weather.gov/gridpoints/EWX/156,91/forecast",
    "denver": "https://api.weather.gov/gridpoints/BOU/63,62/forecast",
    "lasvegas": "https://api.weather.gov/gridpoints/VEF/123,98/forecast",
    "sanantonio": "https://api.weather.gov/gridpoints/EWX/126,54/forecast",
    "minneapolis": "https://api.weather.gov/gridpoints/MPX/108,72/forecast",
    "okc": "https://api.weather.gov/gridpoints/OUN/97,94/forecast",
    "neworleans": "https://api.weather.gov/gridpoints/LIX/68,88/forecast",
}

# Map Kalshi series to (city, temp_type)
# temp_type: "high" uses high_f forecast, "low" uses low_f forecast
SERIES_TO_CITY = {
    # Original 9 cities — high temp
    "KXHIGHTDC": ("dc", "high"),
    "KXHIGHTSFO": ("sf", "high"),
    "KXTEMPNYCH": ("nyc", "high"),
    "KXHIGHTHOU": ("houston", "high"),
    "KXHIGHTBOS": ("boston", "high"),
    "KXHIGHTATL": ("atlanta", "high"),
    "KXHIGHTDAL": ("dallas", "high"),
    "KXHIGHTPHX": ("phoenix", "high"),
    "KXHIGHTSEA": ("seattle", "high"),
    # Expansion: 12 new cities — high temp (tickers verified on Kalshi 2026-03-30)
    "KXHIGHLAX": ("la", "high"),
    "KXHIGHMIA": ("miami", "high"),
    "KXHIGHPHIL": ("philadelphia", "high"),
    "KXHIGHCHI": ("chicago", "high"),
    "KXHIGHNY": ("nyc", "high"),          # NYC daily high (vs KXTEMPNYCH hourly)
    "KXHIGHAUS": ("austin", "high"),
    "KXHIGHDEN": ("denver", "high"),
    "KXHIGHTLV": ("lasvegas", "high"),
    "KXHIGHTSATX": ("sanantonio", "high"),
    "KXHIGHTMIN": ("minneapolis", "high"),
    "KXHIGHTOKC": ("okc", "high"),
    "KXHIGHTNOLA": ("neworleans", "high"),
    # Low-temp series (7 cities confirmed active on Kalshi 2026-03-30)
    "KXLOWTNYC": ("nyc", "low"),
    "KXLOWTLAX": ("la", "low"),
    "KXLOWTMIA": ("miami", "low"),
    "KXLOWTCHI": ("chicago", "low"),
    "KXLOWTPHIL": ("philadelphia", "low"),
    "KXLOWTAUS": ("austin", "low"),
    "KXLOWTDEN": ("denver", "low"),
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

            # NWS alternates daytime (high) and nighttime (low) periods.
            # Extract both high and low from the first few periods.
            today = periods[0]
            temp = today.get("temperature")
            if temp is None:
                continue

            is_daytime = today.get("isDaytime", True)
            high_f = None
            low_f = None

            if is_daytime:
                high_f = float(temp)
                # Next period should be tonight's low
                if len(periods) > 1 and not periods[1].get("isDaytime", True):
                    low_temp = periods[1].get("temperature")
                    if low_temp is not None:
                        low_f = float(low_temp)
            else:
                low_f = float(temp)
                # Next period should be tomorrow's high
                if len(periods) > 1 and periods[1].get("isDaytime", True):
                    high_temp = periods[1].get("temperature")
                    if high_temp is not None:
                        high_f = float(high_temp)

            # Need at least a high to be useful
            if high_f is None and low_f is None:
                continue

            result[city] = {
                "high_f": high_f,
                "low_f": low_f,
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
