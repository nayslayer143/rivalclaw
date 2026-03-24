#!/usr/bin/env python3
"""
RivalClaw Polymarket feed — gamma API fetch with configurable cache and category filtering.
Architecture-faithful port of Mirofish polymarket_feed.py, arb-relevant paths only.
"""
from __future__ import annotations
import json
import os
import sqlite3
import datetime
import requests
from pathlib import Path

GAMMA_API = "https://gamma-api.polymarket.com/markets"
DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
MIN_VOLUME = float(os.environ.get("RIVALCLAW_MIN_VOLUME", "10000"))
CACHE_MAX_AGE_HOURS = float(os.environ.get("RIVALCLAW_CACHE_MAX_AGE_HOURS", "6"))

# Config toggles (adjustment #1 and #4)
FETCH_MODE = os.environ.get("RIVALCLAW_FETCH_MODE", "fresh")  # "fresh" or "cache_ok"
CATEGORIES = os.environ.get("RIVALCLAW_CATEGORIES", "")  # "" = all, or "crypto,politics,sports"


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parse_json(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            pass
    return []


def _is_cache_fresh():
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=CACHE_MAX_AGE_HOURS)).isoformat()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM market_data WHERE fetched_at > ?", (cutoff,)
        ).fetchone()
        return row["cnt"] > 0
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def fetch_markets():
    """Fetch active markets, cache to DB, return list of dicts."""
    # Cache toggle: skip API if cache_ok mode and cache is fresh
    if FETCH_MODE == "cache_ok" and _is_cache_fresh():
        print("[rivalclaw/feed] Cache fresh — skipping live fetch")
        return _load_cached_markets()

    now = datetime.datetime.utcnow().isoformat()
    try:
        resp = requests.get(
            GAMMA_API,
            params={"active": "true", "closed": "false", "limit": 100},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()
        markets_raw = raw if isinstance(raw, list) else raw.get("data", raw.get("markets", []))
    except Exception as e:
        print(f"[rivalclaw/feed] API error: {e}. Using cache.")
        return _load_cached_markets()

    markets = []
    conn = _get_conn()
    try:
        for m in markets_raw:
            volume = float(m.get("volume", 0) or 0)
            if volume < MIN_VOLUME:
                continue

            yes_price = no_price = None
            outcome_prices = _parse_json(m.get("outcomePrices"))
            outcomes = _parse_json(m.get("outcomes"))
            if outcome_prices and outcomes:
                for label, price_str in zip(outcomes, outcome_prices):
                    try:
                        price = float(price_str)
                    except (ValueError, TypeError):
                        continue
                    if (label or "").lower() == "yes":
                        yes_price = price
                    elif (label or "").lower() == "no":
                        no_price = price

            if yes_price is None or no_price is None:
                for tok in (m.get("tokens") or []):
                    outcome = (tok.get("outcome") or "").upper()
                    try:
                        price = float(tok.get("price", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    if outcome == "YES":
                        yes_price = price
                    elif outcome == "NO":
                        no_price = price

            if yes_price is None or no_price is None:
                continue

            market_id = m.get("conditionId") or m.get("id") or ""
            question = m.get("question", "")
            category = (m.get("category") or "").lower()
            end_date = m.get("endDate") or m.get("end_date")
            if not market_id or not question:
                continue

            conn.execute("""
                INSERT INTO market_data
                (market_id, question, category, yes_price, no_price, volume, end_date, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (market_id, question, category, yes_price, no_price, volume, end_date, now))

            markets.append({
                "market_id": market_id, "question": question, "category": category,
                "yes_price": yes_price, "no_price": no_price,
                "volume": volume, "end_date": end_date,
            })
        conn.commit()
    finally:
        conn.close()

    filtered = _apply_category_filter(markets)
    print(f"[rivalclaw/feed] Fetched {len(markets)} markets, {len(filtered)} after filter")
    return filtered


def _load_cached_markets():
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=CACHE_MAX_AGE_HOURS)).isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT md.* FROM market_data md
            INNER JOIN (
                SELECT market_id, MAX(fetched_at) AS latest
                FROM market_data WHERE fetched_at > ?
                GROUP BY market_id
            ) l ON md.market_id = l.market_id AND md.fetched_at = l.latest
        """, (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    markets = [dict(r) for r in rows]
    return _apply_category_filter(markets)


def _apply_category_filter(markets):
    if not CATEGORIES:
        return markets
    cats = {c.strip().lower() for c in CATEGORIES.split(",") if c.strip()}
    if not cats:
        return markets
    return [m for m in markets if not m.get("category") or m.get("category") in cats]


def get_latest_prices():
    """Return {market_id: {yes_price, no_price}} from most recent snapshot per market."""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT md.market_id, md.yes_price, md.no_price
            FROM market_data md
            INNER JOIN (
                SELECT market_id, MAX(fetched_at) AS latest
                FROM market_data GROUP BY market_id
            ) l ON md.market_id = l.market_id AND md.fetched_at = l.latest
        """).fetchall()
        return {r["market_id"]: {"yes_price": r["yes_price"], "no_price": r["no_price"]}
                for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
