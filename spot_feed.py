#!/usr/bin/env python3
"""
RivalClaw spot price feed — CoinGecko free API for crypto spot prices.
Used to compute fair value of Kalshi binary contracts.
"""
from __future__ import annotations

import time
import requests

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"

# CoinGecko IDs for cryptos we trade on Kalshi
CRYPTO_IDS = "bitcoin,ethereum,dogecoin,cardano,binancecoin,bitcoin-cash"

_cache: dict = {}
_cache_ts: float = 0
CACHE_TTL_SECONDS = 30  # Refresh every 30s max


def get_spot_prices() -> dict:
    """
    Returns {coingecko_id: price_usd} for all tracked cryptos.
    Cached for 30 seconds to avoid CoinGecko rate limits (free = 30 calls/min).
    """
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
        return _cache

    try:
        resp = requests.get(COINGECKO_API, params={
            "ids": CRYPTO_IDS,
            "vs_currencies": "usd",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _cache = {k: v["usd"] for k, v in data.items() if "usd" in v}
        _cache_ts = time.time()
        return _cache
    except Exception as e:
        print(f"[rivalclaw/spot] CoinGecko error: {e}")
        try:
            import event_logger as elog
            elog.error("spot_feed", type(e).__name__, str(e))
        except Exception:
            pass
        return _cache  # Return stale cache on error


def get_crypto_price(coingecko_id: str) -> float | None:
    """Get spot price for a single crypto. Returns None if unavailable."""
    prices = get_spot_prices()
    return prices.get(coingecko_id)
