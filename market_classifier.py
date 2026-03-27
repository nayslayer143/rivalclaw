#!/usr/bin/env python3
"""
RivalClaw market classifier — scores markets on resolution speed and clarity.
Capital velocity insight: 1% edge cycling 3x/day beats 5% edge cycling weekly.
Prioritizes fastest-resolving, most objectively-settled markets.
"""
from __future__ import annotations
import datetime
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
MIN_PRIORITY = float(os.environ.get("RIVALCLAW_MIN_PRIORITY", "3"))

CATEGORY_PATTERNS = {
    "sports": {
        "keywords": ["win", "beat", "game", "match", "score", "playoff", "championship",
                     "NBA", "NFL", "NHL", "MLB", "UFC", "soccer", "football", "basketball",
                     "tennis", "boxing", "Super Bowl", "World Series", "finals"],
        "speed": 3, "clarity": 3,
    },
    "weather": {
        "keywords": ["rain", "snow", "temperature", "degrees", "inches", "weather",
                     "hurricane", "tornado", "forecast", "NOAA", "precipitation",
                     "heat", "cold", "storm", "wind", "max temp", "high temp",
                     "highest temperature"],
        "speed": 3, "clarity": 3,
    },
    "crypto_fast": {
        "keywords": ["15 min", "15min", "next 15", "price up in next",
                     "DOGE price", "BNB price", "ADA price", "BCH price"],
        "speed": 3, "clarity": 3,
    },
    "crypto_daily": {
        "keywords": ["Bitcoin price", "Ethereum price", "BTC price", "ETH price",
                     "price range", "price at"],
        "speed": 2, "clarity": 3,
    },
    "econ": {
        "keywords": ["CPI", "inflation", "unemployment", "jobless", "GDP", "Fed",
                     "interest rate", "FOMC", "payroll", "retail sales",
                     "consumer confidence", "PMI", "earnings", "treasury", "yield"],
        "speed": 2, "clarity": 2,
    },
    "commodities": {
        "keywords": ["gold price", "silver price", "oil price", "crude",
                     "USD/JPY", "forex", "exchange rate"],
        "speed": 2, "clarity": 3,
    },
    "event": {
        "keywords": ["announce", "launch", "keynote", "conference", "premiere",
                     "release", "debut", "unveil", "award", "Oscar", "Grammy",
                     "Emmy", "box office"],
        "speed": 1, "clarity": 1,
    },
    "politics": {
        "keywords": ["election", "vote", "ballot", "primary", "caucus", "senate",
                     "congress", "bill", "legislation", "president", "governor",
                     "Trump", "presidential action"],
        "speed": 0, "clarity": 0,
    },
}

# Compile keyword patterns for fast matching
_COMPILED = {}
for cat, info in CATEGORY_PATTERNS.items():
    pattern = "|".join(re.escape(k) for k in info["keywords"])
    _COMPILED[cat] = (re.compile(pattern, re.IGNORECASE), info["speed"], info["clarity"])


def _detect_category(title: str) -> tuple[str, int, int]:
    """Match market title against category patterns. Returns (category, speed, clarity)."""
    for cat, (pattern, speed, clarity) in _COMPILED.items():
        if pattern.search(title):
            # Politics override: if "today" or "vote today" → boost
            if cat == "politics" and re.search(r"today|this week|vote\s+\w+day", title, re.I):
                return cat, 2, 2
            return cat, speed, clarity
    return "unknown", 0, 0


def resolution_speed_score(market: dict) -> int:
    title = market.get("question", "") or market.get("title", "")
    _, speed, _ = _detect_category(title)
    return speed


def resolution_clarity_score(market: dict) -> int:
    title = market.get("question", "") or market.get("title", "")
    _, _, clarity = _detect_category(title)
    return clarity


def time_decay_score(market: dict) -> int:
    end = market.get("end_date") or market.get("close_time")
    if not end:
        return 0
    try:
        close = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        hours = (close - now).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0
    if hours <= 0:
        return 0
    if hours < 6:
        return 3
    if hours < 24:
        return 2
    if hours < 72:
        return 1
    return 0


def market_priority(market: dict) -> float:
    speed = resolution_speed_score(market)
    clarity = resolution_clarity_score(market)
    decay = time_decay_score(market)
    return (speed * 2) + (clarity * 2) + decay


def classify_and_filter(markets: list[dict]) -> list[dict]:
    """
    Score all markets, store scores in DB, return only those above MIN_PRIORITY.
    Sorted by priority descending.
    """
    now = datetime.datetime.utcnow().isoformat()
    scored = []
    skipped = 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    # Ensure table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_scores (
            market_id TEXT PRIMARY KEY,
            platform TEXT,
            title TEXT,
            speed_score INTEGER,
            clarity_score INTEGER,
            decay_score INTEGER,
            priority_score REAL,
            category TEXT,
            scored_at TEXT
        )
    """)

    for m in markets:
        mid = m.get("market_id", "")
        title = m.get("question", "") or m.get("title", "")
        venue = m.get("venue", "unknown")

        speed = resolution_speed_score(m)
        clarity = resolution_clarity_score(m)
        decay = time_decay_score(m)
        priority = (speed * 2) + (clarity * 2) + decay
        cat, _, _ = _detect_category(title)

        # Store score
        conn.execute("""
            INSERT OR REPLACE INTO market_scores
            (market_id, platform, title, speed_score, clarity_score, decay_score,
             priority_score, category, scored_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (mid, venue, title[:200], speed, clarity, decay, priority, cat, now))

        # Attach score to market dict for downstream use
        m["priority_score"] = priority
        m["speed_category"] = cat

        if priority >= MIN_PRIORITY:
            scored.append(m)
        else:
            skipped += 1

    conn.commit()
    conn.close()

    scored.sort(key=lambda m: m.get("priority_score", 0), reverse=True)
    print(f"[rivalclaw/classify] {len(scored)} markets pass (>={MIN_PRIORITY}), {skipped} filtered out")
    return scored
