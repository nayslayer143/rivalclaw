#!/usr/bin/env python3
"""
RivalClaw Kalshi feed — RSA-authenticated API client focused on fast-resolution markets.
Ported from OpenClaw kalshi_feed.py, adapted for rivalclaw.db schema.

Auth: RSA key-pair signature (KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE).
Env vars:
    KALSHI_API_KEY_ID       — API key ID from Kalshi dashboard
    KALSHI_PRIVATE_KEY_PATH — path to RSA private key PEM file
    KALSHI_API_ENV          — "demo" or "prod"
"""
from __future__ import annotations

import base64
import datetime
import os
import sqlite3
import time
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

PROD_API = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"
PAGE_LIMIT = 200
CACHE_MAX_AGE_HOURS = 0  # Always fetch fresh — we need current data for all series

# Focus on fastest-resolving series (expanded beyond crypto)
FAST_SERIES = [
    # 15-min crypto (fastest feedback)
    "KXDOGE15M", "KXADA15M", "KXBNB15M", "KXBCH15M",
    # Daily crypto
    "KXBTC", "KXETH", "KXBTCMAXD",
    # Weather (resolves same day, real volume)
    "KXHIGHTDC", "KXHIGHTSFO", "KXTEMPNYCH",
    # Commodities + FX (daily)
    "KXGOLDD", "KXSILVERD", "KXTNOTED", "KXUSDJPY",
    # Index (daily)
    "KXINXSPX", "KXINXNDX",
]

# Map series prefix to CoinGecko underlying ID (crypto only)
SERIES_TO_UNDERLYING = {
    "KXDOGE15M": "dogecoin",
    "KXADA15M": "cardano",
    "KXBNB15M": "binancecoin",
    "KXBCH15M": "bitcoin-cash",
    "KXBTC": "bitcoin",
    "KXBTCMAXD": "bitcoin",
    "KXETH": "ethereum",
}

# Map series to weather city (for weather_feed)
SERIES_TO_WEATHER = {
    "KXHIGHTDC": "dc",
    "KXHIGHTSFO": "sf",
    "KXTEMPNYCH": "nyc",
}

_warned_no_key = False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _is_cache_fresh():
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=CACHE_MAX_AGE_HOURS)).isoformat()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM market_data WHERE venue='kalshi' AND fetched_at > ?",
            (cutoff,),
        ).fetchone()
        return row["cnt"] > 0
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RSA signature auth
# ---------------------------------------------------------------------------

def _get_api_base():
    env = os.environ.get("KALSHI_API_ENV", "demo").lower()
    return PROD_API if env == "prod" else DEMO_API


def _load_private_key():
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not Path(key_path).exists():
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(key_path, "rb") as f:
            return load_pem_private_key(f.read(), password=None)
    except Exception as e:
        print(f"[rivalclaw/kalshi] Failed to load private key: {e}")
        return None


def _sign_request(private_key, timestamp_str, method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    message = (timestamp_str + method.upper() + path).encode("utf-8")
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _auth_headers(method, path):
    global _warned_no_key

    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    if not api_key_id:
        if not _warned_no_key:
            print("[rivalclaw/kalshi] KALSHI_API_KEY_ID not set — skipping Kalshi feed")
            _warned_no_key = True
        return None

    private_key = _load_private_key()
    if private_key is None:
        if not _warned_no_key:
            print("[rivalclaw/kalshi] Private key not available — skipping")
            _warned_no_key = True
        return None

    timestamp_str = str(int(time.time() * 1000))
    signature = _sign_request(private_key, timestamp_str, method, path)

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _call_kalshi(method, path, params=None):
    headers = _auth_headers(method, path)
    if headers is None:
        return None

    url = f"{_get_api_base()}{path}"
    try:
        resp = requests.request(method, url, params=params, headers=headers, timeout=30)
        if resp.status_code == 401:
            print(f"[rivalclaw/kalshi] 401 Unauthorized — check API credentials")
            return None
        if resp.status_code == 429:
            print(f"[rivalclaw/kalshi] 429 Rate limited")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[rivalclaw/kalshi] API error: {e}")
        try:
            import event_logger as elog
            elog.error("kalshi_feed", type(e).__name__, str(e))
        except Exception:
            pass
        return None


def _adapt_market_fields(m):
    """Adapt Kalshi API v2 field names (dollars_suffix → cents)."""
    def _dollars_to_cents(val):
        if val is None:
            return None
        try:
            f = float(val)
            if f > 0:
                return int(round(f * 100))
        except (ValueError, TypeError):
            pass
        return None

    m.setdefault("yes_bid", _dollars_to_cents(m.get("yes_bid_dollars")))
    m.setdefault("yes_ask", _dollars_to_cents(m.get("yes_ask_dollars")))
    m.setdefault("no_bid", _dollars_to_cents(m.get("no_bid_dollars")))
    m.setdefault("no_ask", _dollars_to_cents(m.get("no_ask_dollars")))
    m.setdefault("last_price", _dollars_to_cents(m.get("last_price_dollars") or m.get("previous_price_dollars")))
    m.setdefault("volume", float(m.get("volume_fp", 0) or 0))
    m.setdefault("volume_24h", float(m.get("volume_24h_fp", 0) or 0))
    m.setdefault("open_interest", float(m.get("open_interest_fp", 0) or 0))
    return m


def _cents_to_float(val):
    if val is None:
        return None
    try:
        return float(val) / 100.0
    except (ValueError, TypeError):
        return None


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _fetch_event_markets(series_ticker):
    """Fetch open markets for a specific series."""
    data = _call_kalshi("GET", "/events", params={
        "series_ticker": series_ticker, "status": "open", "limit": 10,
    })
    if not data:
        return []

    markets = []
    for event in data.get("events", []):
        evt_ticker = event.get("event_ticker", "")
        if not evt_ticker:
            continue
        mdata = _call_kalshi("GET", "/markets", params={
            "event_ticker": evt_ticker, "status": "open", "limit": 100,
        })
        if mdata:
            for m in mdata.get("markets", []):
                _adapt_market_fields(m)
                if not m.get("mve_collection_ticker") and "KXMVE" not in m.get("ticker", ""):
                    markets.append(m)
    return markets


# ---------------------------------------------------------------------------
# Main feed functions
# ---------------------------------------------------------------------------

def fetch_markets():
    """
    Fetch active Kalshi fast-resolution markets, cache to DB.
    Returns list of normalized market dicts.
    """
    if _auth_headers("GET", "/markets") is None:
        return []

    if _is_cache_fresh():
        print("[rivalclaw/kalshi] Cache fresh — using cached data")
        return _load_cached()

    now = datetime.datetime.utcnow().isoformat()
    all_markets = []
    seen_tickers = set()

    for series in FAST_SERIES:
        series_markets = _fetch_event_markets(series)
        for m in series_markets:
            ticker = m.get("ticker", "")
            if ticker and ticker not in seen_tickers:
                seen_tickers.add(ticker)
                all_markets.append(m)

    if not all_markets:
        print("[rivalclaw/kalshi] No markets from API — using cache")
        return _load_cached()

    # Cache to DB
    conn = _get_conn()
    try:
        for m in all_markets:
            last_price = _cents_to_float(m.get("last_price"))
            yes_price = last_price if last_price else _cents_to_float(m.get("yes_ask"))
            no_price = (1.0 - yes_price) if yes_price else None
            close_time = m.get("close_time") or m.get("expiration_time", "")

            # Store in market_data for dashboard compat
            conn.execute("""
                INSERT INTO market_data
                (market_id, question, category, yes_price, no_price, volume, end_date, fetched_at, venue)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                m.get("ticker", ""), m.get("title", ""),
                (m.get("category") or "").lower(),
                yes_price, no_price,
                float(m.get("volume", 0) or 0),
                close_time, now, "kalshi",
            ))

            # Store extra Kalshi fields for fair value computation
            conn.execute("""
                INSERT INTO kalshi_extra
                (market_id, event_ticker, yes_bid, yes_ask, no_bid, no_ask, last_price,
                 volume_24h, open_interest, close_time, strike_type, cap_strike, floor_strike,
                 rules_primary, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                m.get("ticker", ""), m.get("event_ticker", ""),
                _cents_to_float(m.get("yes_bid")), _cents_to_float(m.get("yes_ask")),
                _cents_to_float(m.get("no_bid")), _cents_to_float(m.get("no_ask")),
                last_price,
                float(m.get("volume_24h", 0) or 0),
                float(m.get("open_interest", 0) or 0),
                close_time,
                m.get("strike_type", ""), _safe_float(m.get("cap_strike")),
                _safe_float(m.get("floor_strike")),
                m.get("rules_primary", ""), now,
            ))
        conn.commit()
    finally:
        conn.close()

    normalized = [_normalize(m) for m in all_markets]
    print(f"[rivalclaw/kalshi] Fetched {len(normalized)} markets from {len(FAST_SERIES)} series")
    return normalized


def _load_cached():
    """Load cached Kalshi markets from market_data + kalshi_extra."""
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=CACHE_MAX_AGE_HOURS)).isoformat()
    conn = _get_conn()
    try:
        # Get latest market_data rows for Kalshi
        rows = conn.execute("""
            SELECT md.*, ke.yes_bid, ke.yes_ask, ke.no_bid, ke.no_ask,
                   ke.last_price as ke_last_price, ke.close_time as ke_close_time,
                   ke.strike_type, ke.cap_strike, ke.floor_strike, ke.event_ticker
            FROM market_data md
            LEFT JOIN (
                SELECT ke2.* FROM kalshi_extra ke2
                INNER JOIN (
                    SELECT market_id, MAX(fetched_at) AS latest
                    FROM kalshi_extra WHERE fetched_at > ?
                    GROUP BY market_id
                ) kl ON ke2.market_id = kl.market_id AND ke2.fetched_at = kl.latest
            ) ke ON md.market_id = ke.market_id
            INNER JOIN (
                SELECT market_id, MAX(fetched_at) AS latest
                FROM market_data WHERE venue='kalshi' AND fetched_at > ?
                GROUP BY market_id
            ) l ON md.market_id = l.market_id AND md.fetched_at = l.latest
        """, (cutoff, cutoff)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    markets = []
    for r in rows:
        markets.append({
            "market_id": r["market_id"],
            "event_ticker": r["event_ticker"] or "",
            "question": r["question"],
            "category": r["category"] or "",
            "yes_price": r["yes_price"],
            "no_price": r["no_price"],
            "yes_bid": r["yes_bid"],
            "yes_ask": r["yes_ask"],
            "no_bid": r["no_bid"],
            "no_ask": r["no_ask"],
            "last_price": r["ke_last_price"],
            "volume": r["volume"] or 0,
            "end_date": r["end_date"],
            "close_time": r["ke_close_time"] or r["end_date"],
            "strike_type": r["strike_type"] or "",
            "cap_strike": r["cap_strike"],
            "floor_strike": r["floor_strike"],
            "venue": "kalshi",
        })
    return markets


def _normalize(m):
    """Convert raw Kalshi API market dict to our internal format."""
    last_price = _cents_to_float(m.get("last_price"))
    yes_ask = _cents_to_float(m.get("yes_ask"))
    yes_bid = _cents_to_float(m.get("yes_bid"))
    no_ask = _cents_to_float(m.get("no_ask"))
    no_bid = _cents_to_float(m.get("no_bid"))
    yes_price = last_price or yes_ask
    no_price = (1.0 - yes_price) if yes_price else None

    return {
        "market_id": m.get("ticker", ""),
        "event_ticker": m.get("event_ticker", ""),
        "question": m.get("title", ""),
        "category": (m.get("category") or "").lower(),
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": last_price,
        "volume": float(m.get("volume", 0) or 0),
        "volume_24h": float(m.get("volume_24h", 0) or 0),
        "open_interest": float(m.get("open_interest", 0) or 0),
        "end_date": m.get("close_time") or m.get("expiration_time", ""),
        "close_time": m.get("close_time") or m.get("expiration_time", ""),
        "strike_type": m.get("strike_type", ""),
        "cap_strike": _safe_float(m.get("cap_strike")),
        "floor_strike": _safe_float(m.get("floor_strike")),
        "venue": "kalshi",
    }


def get_latest_prices():
    """Return {market_id: {yes_price, no_price}} for Kalshi markets."""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT md.market_id, md.yes_price, md.no_price
            FROM market_data md
            INNER JOIN (
                SELECT market_id, MAX(fetched_at) AS latest
                FROM market_data WHERE venue='kalshi'
                GROUP BY market_id
            ) l ON md.market_id = l.market_id AND md.fetched_at = l.latest
        """).fetchall()
        return {r["market_id"]: {"yes_price": r["yes_price"], "no_price": r["no_price"]}
                for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
