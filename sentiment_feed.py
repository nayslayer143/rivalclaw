#!/usr/bin/env python3
"""
Crypto sentiment feed — pulls sentiment signals for BTC/ETH/BNB.
Uses free APIs that don't require Chrome/browser sessions.
Produces a confidence modifier per asset: +boost, 0 neutral, -dampen, or SKIP.

This feeds into paper trades ONLY. Live trades ignore this signal.
"""
from __future__ import annotations
import time
import requests
import subprocess
import json
from pathlib import Path

_cache: dict = {}
_cache_ts: float = 0
CACHE_TTL = 120  # 2 min

OPENCLI = Path.home() / "bin" / "opencli-rs"


def _opencli_available() -> bool:
    """Check if opencli-rs can connect to Chrome."""
    try:
        result = subprocess.run(
            [str(OPENCLI), "twitter", "profile", "bitcoin"],
            capture_output=True, text=True, timeout=15,
        )
        return "not connected" not in result.stderr and result.returncode == 0
    except Exception:
        return False


def _get_crypto_fear_greed() -> dict | None:
    """Free crypto fear & greed index — alternative.me API."""
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10,
        )
        if resp.ok:
            data = resp.json().get("data", [{}])[0]
            return {
                "value": int(data.get("value", 50)),
                "label": data.get("value_classification", "Neutral"),
            }
    except Exception:
        pass
    return None


def _get_twitter_sentiment(query: str, limit: int = 10) -> dict | None:
    """Get Twitter sentiment via opencli-rs (requires Chrome extension)."""
    try:
        result = subprocess.run(
            [str(OPENCLI), "twitter", "search", query, "--limit", str(limit), "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        tweets = json.loads(result.stdout)
        if not tweets:
            return None

        # Simple sentiment: count bearish vs bullish keywords
        bearish_words = {"crash", "dump", "plunge", "rekt", "bear", "sell", "panic", "fear", "scam", "rug"}
        bullish_words = {"moon", "pump", "bull", "buy", "rally", "ath", "breakout", "surge", "bullish"}

        bearish = 0
        bullish = 0
        for t in tweets:
            text = (t.get("text", "") or t.get("content", "")).lower()
            bearish += sum(1 for w in bearish_words if w in text)
            bullish += sum(1 for w in bullish_words if w in text)

        total = bearish + bullish
        if total == 0:
            return {"sentiment": 0.0, "label": "neutral", "sample": len(tweets)}

        # -1 to +1 scale: negative = bearish, positive = bullish
        score = (bullish - bearish) / total
        label = "bullish" if score > 0.2 else "bearish" if score < -0.2 else "neutral"
        return {"sentiment": score, "label": label, "sample": len(tweets)}
    except Exception:
        return None


def _get_reddit_sentiment(subreddit: str = "cryptocurrency", limit: int = 10) -> dict | None:
    """Get Reddit sentiment via opencli-rs."""
    try:
        result = subprocess.run(
            [str(OPENCLI), "reddit", "hot", "--subreddit", subreddit, "--limit", str(limit), "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        posts = json.loads(result.stdout)
        if not posts:
            return None

        bearish_words = {"crash", "dump", "bear", "sell", "panic", "fear", "scam", "rug", "warning"}
        bullish_words = {"moon", "pump", "bull", "buy", "rally", "ath", "breakout", "surge", "accumulate"}

        bearish = bullish = 0
        for p in posts:
            text = (p.get("title", "") + " " + p.get("selftext", "")).lower()
            bearish += sum(1 for w in bearish_words if w in text)
            bullish += sum(1 for w in bullish_words if w in text)

        total = bearish + bullish
        if total == 0:
            return {"sentiment": 0.0, "label": "neutral", "sample": len(posts)}

        score = (bullish - bearish) / total
        label = "bullish" if score > 0.2 else "bearish" if score < -0.2 else "neutral"
        return {"sentiment": score, "label": label, "sample": len(posts)}
    except Exception:
        return None


def get_signals() -> dict:
    """
    Returns per-asset confidence modifiers.

    {
        "bitcoin": {"modifier": 1.0, "reason": "neutral", "sources": [...]},
        "ethereum": {"modifier": 1.0, "reason": "neutral", "sources": [...]},
        "binancecoin": {"modifier": 1.0, "reason": "neutral", "sources": [...]},
        "fear_greed": {"value": 50, "label": "Neutral"},
    }

    modifier: 1.2 = boost 20%, 1.0 = neutral, 0.5 = dampen 50%, 0.0 = SKIP
    """
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < CACHE_TTL:
        return _cache

    signals = {}

    # 1. Fear & Greed Index (always available, no Chrome needed)
    fng = _get_crypto_fear_greed()
    if fng:
        signals["fear_greed"] = fng
        # Map FNG to modifier: Extreme Fear (0-25) = boost NO bets,
        # Extreme Greed (75-100) = dampen NO bets (crowd is bullish, might be right)
        fng_value = fng["value"]
        if fng_value <= 20:
            base_mod = 1.3  # Extreme fear → strong NO conviction
        elif fng_value <= 35:
            base_mod = 1.15  # Fear → mild NO boost
        elif fng_value >= 80:
            base_mod = 0.6  # Extreme greed → dampen NO (crowd might be right)
        elif fng_value >= 65:
            base_mod = 0.85  # Greed → mild dampen
        else:
            base_mod = 1.0  # Neutral
    else:
        base_mod = 1.0

    # Apply to all crypto assets
    for asset in ["bitcoin", "ethereum", "binancecoin"]:
        signals[asset] = {
            "modifier": base_mod,
            "reason": fng["label"] if fng else "no_data",
            "sources": ["fear_greed"],
        }

    # 2. Twitter sentiment (if opencli-rs is available)
    use_opencli = _opencli_available()
    if use_opencli:
        for asset, query in [("bitcoin", "BTC crypto"), ("ethereum", "ETH ethereum"), ("binancecoin", "BNB")]:
            twitter = _get_twitter_sentiment(query)
            if twitter:
                signals[asset]["sources"].append("twitter")
                # Bearish Twitter + Fear = stronger NO conviction
                # Bullish Twitter + Greed = weaker NO conviction
                if twitter["label"] == "bearish":
                    signals[asset]["modifier"] *= 1.15
                    signals[asset]["reason"] += f" + twitter_bearish({twitter['sentiment']:.2f})"
                elif twitter["label"] == "bullish":
                    signals[asset]["modifier"] *= 0.85
                    signals[asset]["reason"] += f" + twitter_bullish({twitter['sentiment']:.2f})"

        # 3. Reddit sentiment
        reddit = _get_reddit_sentiment()
        if reddit:
            for asset in ["bitcoin", "ethereum", "binancecoin"]:
                signals[asset]["sources"].append("reddit")
                if reddit["label"] == "bearish":
                    signals[asset]["modifier"] *= 1.1
                elif reddit["label"] == "bullish":
                    signals[asset]["modifier"] *= 0.9

    # Clamp modifiers to reasonable range
    for asset in ["bitcoin", "ethereum", "binancecoin"]:
        mod = signals[asset]["modifier"]
        signals[asset]["modifier"] = max(0.3, min(1.5, mod))

    _cache = signals
    _cache_ts = time.time()
    return signals


if __name__ == "__main__":
    signals = get_signals()
    print("=== SENTIMENT SIGNALS ===")
    for k, v in signals.items():
        if isinstance(v, dict) and "modifier" in v:
            print(f"  {k:15} mod={v['modifier']:.2f} reason={v['reason']} sources={v['sources']}")
        else:
            print(f"  {k:15} {v}")
