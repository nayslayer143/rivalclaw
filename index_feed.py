#!/usr/bin/env python3
"""
RivalClaw index spot feed — Yahoo Finance free API for equity index prices.
Used to compute fair value of Kalshi index binary contracts (KXINXU, KXNASDAQ100U).

Separate from spot_feed.py (CoinGecko crypto) because Yahoo Finance is a
different API with different rate limits and response format.
"""
from __future__ import annotations

import time
import requests

YAHOO_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Map internal index IDs to Yahoo Finance tickers
INDEX_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq100": "^NDX",
}

_cache: dict = {}
_cache_ts: float = 0
CACHE_TTL_SECONDS = 30  # Refresh every 30s max


def get_index_prices() -> dict:
    """
    Returns {index_id: price_usd} for all tracked indices.
    Cached for 30 seconds to avoid Yahoo Finance rate limits.
    Example: {"sp500": 5432.10, "nasdaq100": 18765.43}
    """
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
        return _cache

    results = {}
    for index_id, ticker in INDEX_TICKERS.items():
        try:
            resp = requests.get(
                f"{YAHOO_CHART_API}/{ticker}",
                params={"interval": "1m", "range": "1d"},
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                price = result[0].get("meta", {}).get("regularMarketPrice")
                if price and price > 0:
                    results[index_id] = float(price)
        except Exception as e:
            print(f"[rivalclaw/index] Yahoo Finance error for {ticker}: {e}")
            try:
                import event_logger as elog
                elog.error("index_feed", type(e).__name__, f"{ticker}: {e}")
            except Exception:
                pass

    if results:
        _cache = results
        _cache_ts = time.time()
    return _cache if _cache else results


def get_index_price(index_id: str) -> float | None:
    """Get spot price for a single index. Returns None if unavailable."""
    prices = get_index_prices()
    return prices.get(index_id)
