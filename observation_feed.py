#!/usr/bin/env python3
"""Real-time NWS observation feed — actual current temperature readings.

This is our primary weather alpha source. While the brain uses NWS FORECASTS
to compute fair value, this feed provides ACTUAL current observations.
If it's 2 PM and the current temp is already 3 degrees above forecast high,
the high-temp bracket market is mispriced.

Also pulls Binance funding rates as a crypto directional bias signal.

All endpoints are free, no auth required.
"""
from __future__ import annotations

import json
import time
import logging
import requests
from pathlib import Path

logger = logging.getLogger("rivalclaw.observation_feed")

# NWS station IDs for Kalshi settlement cities
NWS_STATIONS = {
    "NYC": "KNYC",    "LAX": "KLAX",    "MIA": "KMIA",
    "CHI": "KMDW",    "PHIL": "KPHL",   "AUS": "KAUS",
    "DEN": "KDEN",    "PHX": "KPHX",    "SEA": "KSEA",
    "SFO": "KSFO",    "BOS": "KBOS",    "ATL": "KATL",
    "DAL": "KDFW",    "HOU": "KHOU",    "MIN": "KMSP",
    "LV": "KLAS",     "SATX": "KSAT",   "OKC": "KOKC",
    "NOLA": "KMSY",   "NY": "KNYC",
}

# Map Kalshi series prefixes to city keys
SERIES_TO_CITY = {
    "KXHIGHTNY": "NYC", "KXHIGHNY": "NYC", "KXLOWTNYC": "NYC", "KXTEMPNY": "NYC",
    "KXHIGHLAX": "LAX", "KXLOWTLAX": "LAX",
    "KXHIGHMIA": "MIA", "KXLOWTMIA": "MIA",
    "KXHIGHCHI": "CHI", "KXLOWTCHI": "CHI",
    "KXHIGHPHIL": "PHIL", "KXLOWTPHIL": "PHIL",
    "KXHIGHAUS": "AUS", "KXLOWTAUS": "AUS",
    "KXHIGHDEN": "DEN", "KXLOWTDEN": "DEN",
    "KXHIGHTPHX": "PHX", "KXHIGHTSEA": "SEA", "KXHIGHTSFO": "SFO",
    "KXHIGHTBOS": "BOS", "KXHIGHTATL": "ATL", "KXHIGHTDAL": "DAL",
    "KXHIGHTHOU": "HOU", "KXHIGHTMIN": "MIN", "KXHIGHTLV": "LV",
    "KXHIGHTSATX": "SATX", "KXHIGHTOKC": "OKC", "KXHIGHTNOLA": "NOLA",
}

_observation_cache: dict = {}
_cache_time: float = 0.0
_CACHE_TTL = 300  # 5 minutes

_funding_cache: dict = {}
_funding_cache_time: float = 0.0
_FUNDING_TTL = 60  # 1 minute


def get_observation(city_key: str) -> dict | None:
    """Get latest NWS observation for a city.

    Returns: {"temp_f": float, "temp_c": float, "timestamp": str, "station": str}
    or None on failure.
    """
    station = NWS_STATIONS.get(city_key)
    if not station:
        return None

    # Check cache
    global _observation_cache, _cache_time
    now = time.time()
    if city_key in _observation_cache and (now - _cache_time) < _CACHE_TTL:
        return _observation_cache[city_key]

    url = f"https://api.weather.gov/stations/{station}/observations/latest"
    try:
        resp = requests.get(url, headers={"User-Agent": "RivalClaw/1.0"}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        props = data.get("properties", {})
        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None:
            return None
        temp_f = temp_c * 9 / 5 + 32
        result = {
            "temp_f": round(temp_f, 1),
            "temp_c": round(temp_c, 1),
            "timestamp": props.get("timestamp", ""),
            "station": station,
        }
        _observation_cache[city_key] = result
        _cache_time = now
        return result
    except Exception as e:
        logger.warning("NWS observation failed for %s: %s", city_key, e)
        return None


def get_all_observations() -> dict:
    """Fetch observations for all cities. Returns {city_key: observation_dict}."""
    results = {}
    for city_key in NWS_STATIONS:
        obs = get_observation(city_key)
        if obs:
            results[city_key] = obs
    return results


def get_observation_for_series(series_prefix: str) -> dict | None:
    """Get observation for a Kalshi series prefix like KXHIGHTNY."""
    city = SERIES_TO_CITY.get(series_prefix)
    if not city:
        return None
    return get_observation(city)


def get_funding_rates() -> dict:
    """Get current Binance funding rates for BTC and ETH.

    Returns: {"BTC": {"rate": float, "next_time": str}, "ETH": {...}}
    Positive rate = longs pay shorts (crowded longs, bearish signal)
    Negative rate = shorts pay longs (crowded shorts, bullish signal)
    """
    global _funding_cache, _funding_cache_time
    now = time.time()
    if _funding_cache and (now - _funding_cache_time) < _FUNDING_TTL:
        return _funding_cache

    result = {}
    for symbol, key in [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")]:
        try:
            resp = requests.get(
                f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                result[key] = {
                    "rate": float(data.get("lastFundingRate", 0)),
                    "mark_price": float(data.get("markPrice", 0)),
                    "index_price": float(data.get("indexPrice", 0)),
                    "next_funding_time": data.get("nextFundingTime", ""),
                }
        except Exception as e:
            logger.warning("Binance funding rate failed for %s: %s", symbol, e)

    _funding_cache = result
    _funding_cache_time = now
    return result


def get_exchange_spreads() -> dict:
    """Get BTC spot price from Binance, Coinbase, Kraken to detect divergences.

    Returns: {"binance": float, "coinbase": float, "kraken": float, "spread_pct": float}
    """
    prices = {}
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        if resp.status_code == 200:
            prices["binance"] = float(resp.json()["price"])
    except Exception:
        pass

    try:
        resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        if resp.status_code == 200:
            prices["coinbase"] = float(resp.json()["data"]["amount"])
    except Exception:
        pass

    try:
        resp = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5)
        if resp.status_code == 200:
            data = resp.json()["result"]["XXBTZUSD"]
            prices["kraken"] = float(data["c"][0])  # last trade price
    except Exception:
        pass

    if len(prices) >= 2:
        vals = list(prices.values())
        prices["spread_pct"] = (max(vals) - min(vals)) / min(vals) * 100
    return prices


if __name__ == "__main__":
    print("=== NWS Observations ===")
    obs = get_all_observations()
    for city, data in sorted(obs.items()):
        print(f"  {city}: {data['temp_f']}°F ({data['station']}) @ {data['timestamp'][:16]}")

    print("\n=== Binance Funding Rates ===")
    rates = get_funding_rates()
    for sym, data in rates.items():
        direction = "LONGS PAY (bearish)" if data["rate"] > 0 else "SHORTS PAY (bullish)"
        print(f"  {sym}: {data['rate']:.6f} ({direction})")

    print("\n=== Exchange Spreads ===")
    spreads = get_exchange_spreads()
    for k, v in spreads.items():
        if k == "spread_pct":
            print(f"  Spread: {v:.3f}%")
        else:
            print(f"  {k}: ${v:,.2f}")
