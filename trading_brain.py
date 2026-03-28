#!/usr/bin/env python3
"""
RivalClaw trading brain — 8-strategy quant engine.

Strategies:
  1. arbitrage              — cross-outcome arb (original, both venues)
  2. fair_value_directional — spot-vs-contract mispricing (Kalshi threshold)
  3. near_expiry_momentum   — near-expiry directional (both venues, no brackets)
  4. cross_strike_arb       — bracket sum deviation from 1.0 (Kalshi)
  5. mean_reversion         — bet against crowd when fair value ≈ 0.50 (15-min crypto)
  6. time_decay             — sell overpriced near-expiry when spot ≈ strike
  7. vol_skew               — buy OTM when realized vol > implied vol
  8. calibration            — trade based on historical price-outcome calibration

Key fixes from -$624 lesson:
  - ONE trade per event_ticker (no bracket spam)
  - Fractional Kelly (0.25x) for unproven strategies
  - Bracket contracts excluded from momentum (stop-loss incompatible)
"""
from __future__ import annotations
import datetime
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import event_logger as elog

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strategies disabled based on live performance data (0% WR / negative edge)
DISABLED_STRATEGIES = set(
    os.environ.get("RIVALCLAW_DISABLED_STRATEGIES", "bracket_neighbor,hedge,pairs_trade").split(",")
)

POLYMARKET_FEE_RATE = float(os.environ.get("ARB_FEE_RATE", "0.02"))
MIN_EDGE = float(os.environ.get("ARB_MIN_EDGE", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))
STALE_THRESHOLD_MINUTES = float(os.environ.get("RIVALCLAW_STALE_MINUTES", "30"))

MIN_FAIR_VALUE_EDGE = float(os.environ.get("RIVALCLAW_MIN_FV_EDGE", "0.04"))
KALSHI_TAKER_FEE_RATE = float(os.environ.get("RIVALCLAW_KALSHI_FEE", "0.07"))
VELOCITY_PREFERENCE = float(os.environ.get("RIVALCLAW_VELOCITY_PREFERENCE", "1.5"))

# Data-driven edge multipliers (from 375-trade analysis)
# NO bets: 45% WR, $34 avg → 3.5x better than YES (37% WR, $15 avg)
NO_DIRECTION_BOOST = float(os.environ.get("RIVALCLAW_NO_BOOST", "1.3"))
# Morning 08-12 UTC: 21% WR. Evening 16-24 UTC: 51-57% WR.
TIME_WEIGHT = {
    "morning": float(os.environ.get("RIVALCLAW_MORNING_WEIGHT", "0.5")),   # 08-12 UTC
    "midday": float(os.environ.get("RIVALCLAW_MIDDAY_WEIGHT", "0.8")),     # 12-16 UTC
    "afternoon": float(os.environ.get("RIVALCLAW_AFTERNOON_WEIGHT", "1.2")),# 16-20 UTC
    "evening": float(os.environ.get("RIVALCLAW_EVENING_WEIGHT", "1.3")),   # 20-00 UTC
    "night": float(os.environ.get("RIVALCLAW_NIGHT_WEIGHT", "0.7")),       # 00-08 UTC
}

def _time_of_day_weight() -> float:
    """Scale position size by time-of-day performance pattern."""
    hour = datetime.datetime.utcnow().hour
    if 8 <= hour < 12:
        return TIME_WEIGHT["morning"]
    elif 12 <= hour < 16:
        return TIME_WEIGHT["midday"]
    elif 16 <= hour < 20:
        return TIME_WEIGHT["afternoon"]
    elif 20 <= hour < 24:
        return TIME_WEIGHT["evening"]
    else:
        return TIME_WEIGHT["night"]

NEAR_EXPIRY_HOURS = float(os.environ.get("RIVALCLAW_NEAR_EXPIRY_HOURS", "48"))
MIN_MOMENTUM_PRICE = float(os.environ.get("RIVALCLAW_MIN_MOMENTUM_PRICE", "0.78"))

# New strategy thresholds
MIN_REVERSION_EDGE = float(os.environ.get("RIVALCLAW_MIN_REVERSION_EDGE", "0.06"))
MIN_DECAY_EDGE = float(os.environ.get("RIVALCLAW_MIN_DECAY_EDGE", "0.03"))
MIN_VOL_SKEW_EDGE = float(os.environ.get("RIVALCLAW_MIN_VOL_SKEW_EDGE", "0.05"))

# Fractional Kelly for unproven strategies (0.25 = quarter Kelly)
KELLY_FRACTION_PROVEN = float(os.environ.get("RIVALCLAW_KELLY_PROVEN", "1.0"))
KELLY_FRACTION_NEW = float(os.environ.get("RIVALCLAW_KELLY_NEW", "0.25"))

CRYPTO_VOL = {
    "dogecoin": float(os.environ.get("RIVALCLAW_VOL_DOGECOIN", "0.90")),
    "cardano": float(os.environ.get("RIVALCLAW_VOL_CARDANO", "0.80")),
    "binancecoin": float(os.environ.get("RIVALCLAW_VOL_BINANCECOIN", "0.65")),
    "bitcoin-cash": float(os.environ.get("RIVALCLAW_VOL_BITCOIN_CASH", "0.75")),
    "bitcoin": float(os.environ.get("RIVALCLAW_VOL_BITCOIN", "0.60")),
    "ethereum": float(os.environ.get("RIVALCLAW_VOL_ETHEREUM", "0.65")),
}

SERIES_TO_UNDERLYING = {
    "KXDOGE15M": "dogecoin", "KXADA15M": "cardano",
    "KXBNB15M": "binancecoin", "KXBCH15M": "bitcoin-cash",
    "KXBTC": "bitcoin", "KXBTCMAXD": "bitcoin", "KXETH": "ethereum",
}

# Weather series → city mapping (for weather_feed)
SERIES_TO_WEATHER = {
    "KXHIGHTDC": "dc",
    "KXHIGHTSFO": "sf",
    "KXTEMPNYCH": "nyc",
}

# Proven strategies get full Kelly, new ones get fractional Kelly
PROVEN_STRATEGIES = {"arbitrage", "fair_value_directional", "time_decay"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TradeDecision:
    market_id: str
    question: str
    direction: str
    confidence: float
    reasoning: str
    strategy: str
    amount_usd: float
    entry_price: float
    shares: float
    decision_generated_at_ms: float = 0.0
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fee(price: float) -> float:
    return POLYMARKET_FEE_RATE * min(price, 1.0 - price)

def _kalshi_fee(price: float) -> float:
    return KALSHI_TAKER_FEE_RATE * min(price, 1.0 - price)

def _venue_fee(price: float, venue: str) -> float:
    return _kalshi_fee(price) if venue == "kalshi" else _fee(price)

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _kelly_size(confidence: float, entry_price: float, balance: float,
                fraction: float = 1.0) -> float | None:
    if entry_price <= 0 or entry_price >= 1:
        return None
    b = (1.0 / entry_price) - 1.0
    kelly = (confidence * b - (1.0 - confidence)) / b
    if kelly <= 0:
        return None
    return min(kelly * fraction * balance, MAX_POSITION_PCT * balance)

MAX_LOSS_PCT = float(os.environ.get("RIVALCLAW_MAX_LOSS_PCT", "0.03"))  # Max 3% balance loss per trade

def _size_for_strategy(strategy: str, confidence: float, entry_price: float,
                       balance: float, direction: str = "YES") -> float | None:
    frac = KELLY_FRACTION_PROVEN if strategy in PROVEN_STRATEGIES else KELLY_FRACTION_NEW
    if direction == "NO":
        frac *= NO_DIRECTION_BOOST
    frac *= _time_of_day_weight()
    amount = _kelly_size(confidence, entry_price, balance, frac)
    if amount is None:
        return None
    # Wipeout protection: if this position goes to $0, max loss = amount.
    # Cap so max loss never exceeds MAX_LOSS_PCT of balance.
    max_loss = balance * MAX_LOSS_PCT
    if amount > max_loss:
        amount = max_loss
    return amount

def _validate_market(market: dict) -> str | None:
    if not market.get("market_id") or not market.get("question"):
        return "malformed"
    yes_p = market.get("yes_price")
    no_p = market.get("no_price")
    if yes_p is None or no_p is None:
        return "missing prices"
    if not (0 < yes_p < 1) or not (0 < no_p < 1):
        return "impossible prices"
    total = yes_p + no_p
    if total > 2.0 or total < 0.01:
        return "sum sanity"
    return None

def _parse_expiry_minutes(market: dict) -> float | None:
    close_str = market.get("close_time") or market.get("end_date")
    if not close_str:
        return None
    try:
        close = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return (close - now).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None

def _find_underlying(market: dict) -> str | None:
    event_ticker = market.get("event_ticker", "")
    market_id = market.get("market_id", "")
    for series, crypto_id in SERIES_TO_UNDERLYING.items():
        if series in event_ticker or series in market_id:
            return crypto_id
    return None

def _compute_fair_value(spot, strike, minutes, vol, strike_type="greater_or_equal"):
    if spot <= 0 or strike <= 0 or minutes <= 0 or vol <= 0:
        return None
    years = minutes / (365.25 * 24 * 60)
    sigma_t = vol * math.sqrt(years)
    if sigma_t < 0.0001:
        if strike_type == "greater_or_equal":
            return 1.0 if spot >= strike else 0.0
        return 1.0 if spot < strike else 0.0
    d2 = math.log(spot / strike) / sigma_t
    if strike_type == "greater_or_equal":
        return max(0.01, min(0.99, _norm_cdf(d2)))
    return max(0.01, min(0.99, 1.0 - _norm_cdf(d2)))

def _make_decision(market, direction, confidence, edge, strategy, balance, venue=None,
                   extra_meta=None):
    """Helper to build a TradeDecision with proper sizing."""
    entry_price = market.get("yes_price", 0) if direction == "YES" else (1.0 - (market.get("yes_price", 0) or 0))
    if entry_price <= 0.01 or entry_price >= 0.99:
        return None
    amount = _size_for_strategy(strategy, confidence, entry_price, balance, direction)
    if amount is None:
        return None
    meta = {"edge": edge, "venue": venue or market.get("venue", "polymarket")}
    if extra_meta:
        meta.update(extra_meta)
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=meta.pop("reasoning", f"{strategy}: edge={edge:.4f}"),
        strategy=strategy, amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000, metadata=meta,
    )


# ---------------------------------------------------------------------------
# Strategy 1: Cross-outcome arbitrage
# ---------------------------------------------------------------------------

def _check_arbitrage(market, balance):
    yes_p = market.get("yes_price", 0) or 0
    no_p = market.get("no_price", 0) or 0
    venue = market.get("venue", "polymarket")
    fee_fn = _kalshi_fee if venue == "kalshi" else _fee
    total_cost = yes_p + no_p + fee_fn(yes_p) + fee_fn(no_p)
    edge = 1.0 - total_cost
    if edge <= MIN_EDGE:
        return None
    direction = "NO" if no_p < (1.0 - yes_p) else "YES"
    entry_price = no_p if direction == "NO" else yes_p
    confidence = min(entry_price + edge, 0.99)
    amount = _size_for_strategy("arbitrage", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"Arb: YES={yes_p:.3f}+NO={no_p:.3f}={yes_p+no_p:.3f} edge={edge:.4f} [{venue}]",
        strategy="arbitrage", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "venue": venue},
    )


# ---------------------------------------------------------------------------
# Strategy 2: Fair value directional (Kalshi threshold contracts)
# ---------------------------------------------------------------------------

def _compute_bracket_fair_value(spot, floor_strike, cap_strike, minutes, vol):
    """Fair value of a bracket: P(floor <= spot <= cap at expiry)."""
    if spot <= 0 or floor_strike <= 0 or cap_strike <= 0 or minutes <= 0 or vol <= 0:
        return None
    if floor_strike >= cap_strike:
        return None
    p_above_floor = _compute_fair_value(spot, floor_strike, minutes, vol, "greater_or_equal")
    p_above_cap = _compute_fair_value(spot, cap_strike, minutes, vol, "greater_or_equal")
    if p_above_floor is None or p_above_cap is None:
        return None
    fair = p_above_floor - p_above_cap
    return max(0.005, min(0.995, fair))


# Price bucket preference: 0.10-0.30 is the sweet spot ($101 avg profit)
# 0.50-0.70 is the dead zone (negative EV). Scale edge requirement by bucket.
BUCKET_EDGE_MULTIPLIER = {
    # Data-driven from 375 trades:
    # 0.15-0.30: $129 avg profit (THE GOLD MINE)
    # 0.30-0.50: -$18 avg profit (MONEY BURNER)
    # 0.70+: -$3 avg profit (dead weight)
    "sweet": 0.4,    # 0.15-0.30: strongest bucket, easiest entry
    "good": 0.7,     # <0.15: decent (deep OTM lottery tickets)
    "neutral": 1.0,  # 0.70-0.90: standard threshold
    "dead": 2.0,     # 0.50-0.70: DOUBLE the requirement — this range loses money
    "extreme": 1.5,  # >0.90: high WR but tiny payoffs, not worth capital
}

def _bucket_multiplier(entry_price):
    if 0.15 <= entry_price < 0.30:
        return BUCKET_EDGE_MULTIPLIER["sweet"]  # $129 avg profit
    elif entry_price < 0.15:
        return BUCKET_EDGE_MULTIPLIER["good"]   # $42 avg (deep OTM)
    elif 0.30 <= entry_price < 0.50:
        return BUCKET_EDGE_MULTIPLIER["dead"]   # -$18 avg (MONEY BURNER)
    elif 0.50 <= entry_price < 0.70:
        return BUCKET_EDGE_MULTIPLIER["dead"]   # -$0.47 avg (worthless)
    elif entry_price >= 0.90:
        return BUCKET_EDGE_MULTIPLIER["extreme"]
    return BUCKET_EDGE_MULTIPLIER["neutral"]


def _get_realtime_vol(underlying_id):
    """Compute realized vol from spot_prices if enough data, else use static."""
    static_vol = CRYPTO_VOL.get(underlying_id, 0.70)
    try:
        import sqlite3
        db = os.environ.get("RIVALCLAW_DB_PATH", str(Path(__file__).parent / "rivalclaw.db"))
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT price_usd FROM spot_prices WHERE crypto_id=? ORDER BY fetched_at DESC LIMIT 200",
            (underlying_id,)).fetchall()
        conn.close()
        prices = [r[0] for r in rows if r[0] and r[0] > 0]
        if len(prices) < 30:
            return static_vol
        prices.reverse()
        log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))
                       if prices[i] > 0 and prices[i-1] > 0]
        if len(log_returns) < 10:
            return static_vol
        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r)**2 for r in log_returns) / len(log_returns)
        realized = math.sqrt(variance) * math.sqrt(365.25 * 24 * 30)
        return max(0.30, min(1.50, realized))
    except Exception:
        return static_vol


def _find_weather_city(market):
    """Match a Kalshi market to a weather city."""
    event_ticker = market.get("event_ticker", "")
    market_id = market.get("market_id", "")
    for series, city in SERIES_TO_WEATHER.items():
        if series in event_ticker or series in market_id:
            return city
    return None


def _get_weather_spot_and_vol(city):
    """Get forecast high (spot equivalent) and forecast error (vol equivalent) for a city."""
    try:
        import weather_feed
        forecast = weather_feed.get_city_forecast(city)
        if forecast:
            return forecast["high_f"], forecast["forecast_error"]
    except Exception:
        pass
    return None, None


def _check_fair_value(market, balance, spot_prices):
    if market.get("venue") != "kalshi":
        return None
    strike_type = market.get("strike_type", "")
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 24 * 60:
        return None

    # Determine underlying: crypto or weather?
    underlying_id = _find_underlying(market)
    weather_city = _find_weather_city(market) if not underlying_id else None

    if underlying_id:
        spot = spot_prices.get(underlying_id)
        if not spot or spot <= 0:
            return None
        vol = _get_realtime_vol(underlying_id)
    elif weather_city:
        spot, vol = _get_weather_spot_and_vol(weather_city)
        if not spot:
            return None
        # Weather "vol" is forecast error in °F, but _compute_fair_value expects
        # annualized vol as a fraction. Convert: error_F / spot_F gives a percentage.
        # For a 2.5°F error on 55°F forecast = ~4.5% → annualize for the timeframe
        vol_pct = vol / spot if spot > 0 else 0.05
        # Scale to annualized: for a same-day forecast, the error is fixed
        # Use a simplified approach: compute directly with normal CDF
        # P(max > strike) = Φ((forecast - strike) / error)
        # We'll handle this in the fair value computation below
        vol = vol_pct * math.sqrt(365.25 * 24 * 60 / max(minutes, 1))
    else:
        return None  # Unknown underlying

    # Bracket contracts: P(floor <= spot <= cap)
    if strike_type == "between":
        floor_s = market.get("floor_strike")
        cap_s = market.get("cap_strike")
        if not floor_s or not cap_s:
            return None
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
        if fair is None:
            return None
        yes_price = market.get("yes_price", 0) or 0
        if yes_price <= 0.01 or yes_price >= 0.99:
            return None
        fee_yes = _kalshi_fee(yes_price)
        no_price = 1.0 - yes_price
        fee_no = _kalshi_fee(no_price)
        edge_yes = fair - (yes_price + fee_yes)
        edge_no = (1.0 - fair) - (no_price + fee_no)
        # Apply price bucket preference — sweet spot gets lower threshold
        thresh_yes = MIN_FAIR_VALUE_EDGE * _bucket_multiplier(yes_price)
        thresh_no = MIN_FAIR_VALUE_EDGE * _bucket_multiplier(no_price)
        if edge_yes > thresh_yes and edge_yes >= edge_no:
            direction, entry_price, confidence, edge = "YES", yes_price, min(fair, 0.90), edge_yes
        elif edge_no > thresh_no:
            direction, entry_price, confidence, edge = "NO", no_price, min(1.0 - fair, 0.90), edge_no
        else:
            return None
        amount = _size_for_strategy("fair_value_directional", confidence, entry_price, balance, direction)
        if amount is None:
            return None
        return TradeDecision(
            market_id=market["market_id"], question=market.get("question", ""),
            direction=direction, confidence=confidence,
            reasoning=f"FairVal/Bracket: spot=${spot:,.0f} [{floor_s:,.0f}-{cap_s:,.0f}] exp={minutes:.0f}m fair={fair:.4f} mkt={yes_price:.3f} edge={edge:.3f} bkt={_bucket_multiplier(entry_price):.1f}x",
            strategy="fair_value_directional", amount_usd=amount, entry_price=entry_price,
            shares=amount / entry_price if entry_price > 0 else 0,
            decision_generated_at_ms=time.time() * 1000,
            metadata={"edge": edge, "fair_value": fair, "spot": spot,
                      "strike": floor_s, "minutes_to_expiry": minutes,
                      "vol": vol, "venue": "kalshi", "strike_type": "between"},
        )

    # Threshold contracts (original logic)
    if strike_type == "greater_or_equal":
        strike = market.get("floor_strike")
    elif strike_type == "less":
        strike = market.get("cap_strike")
    else:
        strike = market.get("floor_strike") or market.get("cap_strike")
    if not strike or strike <= 0:
        return None
    fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)
    if fair is None:
        return None
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None
    fee_yes = _kalshi_fee(yes_price)
    no_price = 1.0 - yes_price
    fee_no = _kalshi_fee(no_price)
    edge_yes = fair - (yes_price + fee_yes)
    edge_no = (1.0 - fair) - (no_price + fee_no)
    if edge_yes > MIN_FAIR_VALUE_EDGE and edge_yes >= edge_no:
        direction, entry_price, confidence, edge = "YES", yes_price, min(fair, 0.95), edge_yes
    elif edge_no > MIN_FAIR_VALUE_EDGE:
        direction, entry_price, confidence, edge = "NO", no_price, min(1.0 - fair, 0.95), edge_no
    else:
        return None
    amount = _size_for_strategy("fair_value_directional", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"FairVal: spot=${spot:,.2f} strike=${strike:,.2f} exp={minutes:.0f}m fair={fair:.3f} mkt={yes_price:.3f} edge={edge:.3f}",
        strategy="fair_value_directional", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "fair_value": fair, "spot": spot, "strike": strike,
                  "minutes_to_expiry": minutes, "vol": vol, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 3: Spot momentum (NEW — replaces dead near_expiry_momentum)
# ---------------------------------------------------------------------------

MIN_SPOT_MOMENTUM_PCT = float(os.environ.get("RIVALCLAW_MIN_SPOT_MOMENTUM", "0.003"))  # 0.3% move

def _check_spot_momentum(market, balance, spot_prices, spot_history):
    """
    When crypto spot has moved sharply in recent minutes, threshold/bracket
    contracts may lag behind. Trade in the direction of momentum.
    Uses spot_prices (current) vs spot_history (recent from DB).
    """
    if market.get("venue") != "kalshi":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 30:
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    current_spot = spot_prices.get(underlying_id)
    if not current_spot or current_spot <= 0:
        return None

    # Need recent spot history to detect momentum
    recent = spot_history.get(underlying_id, [])
    if len(recent) < 3:  # Need at least 3 data points (~6 min of 2-min cycles)
        return None

    # Compute momentum: % change from oldest to newest
    oldest_price = recent[0]
    if oldest_price <= 0:
        return None
    momentum_pct = (current_spot - oldest_price) / oldest_price

    if abs(momentum_pct) < MIN_SPOT_MOMENTUM_PCT:
        return None

    strike_type = market.get("strike_type", "")
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    # For threshold contracts: momentum UP → buy YES on "above" strikes near spot
    # For bracket contracts: momentum toward bracket → buy YES
    if strike_type == "between":
        floor_s = market.get("floor_strike") or 0
        cap_s = market.get("cap_strike") or 0
        if floor_s <= 0 or cap_s <= 0:
            return None
        mid_bracket = (floor_s + cap_s) / 2
        # Is spot moving TOWARD this bracket?
        dist_now = abs(current_spot - mid_bracket)
        dist_before = abs(oldest_price - mid_bracket)
        if dist_now >= dist_before:
            return None  # Moving away, not toward
        # Spot is approaching this bracket — buy YES
        direction = "YES"
        entry_price = yes_price
        edge = abs(momentum_pct) * 2  # Scale edge by momentum strength
    elif strike_type in ("greater_or_equal", "greater"):
        strike = market.get("floor_strike") or 0
        if strike <= 0:
            return None
        if momentum_pct > 0 and current_spot > strike * 0.99:
            # Upward momentum, spot near/above strike → YES
            direction = "YES"
            entry_price = yes_price
            edge = momentum_pct
        elif momentum_pct < 0 and current_spot < strike * 1.01:
            # Downward momentum, spot near/below strike → NO
            direction = "NO"
            entry_price = 1.0 - yes_price
            edge = abs(momentum_pct)
        else:
            return None
    else:
        return None

    fee = _kalshi_fee(entry_price)
    edge_after_fee = edge - fee
    if edge_after_fee <= MIN_EDGE:
        return None

    confidence = min(0.50 + edge * 2, 0.85)  # Conservative — momentum isn't certainty
    amount = _size_for_strategy("spot_momentum", confidence, entry_price, balance, direction)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"SpotMom: {underlying_id} {momentum_pct:+.2%} in {len(recent)*2}min, edge={edge_after_fee:.3f}",
        strategy="spot_momentum", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge_after_fee, "momentum_pct": momentum_pct,
                  "spot": current_spot, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 4: Cross-strike arbitrage (bracket sum != 1.0)
# ---------------------------------------------------------------------------

def _check_cross_strike_arb(event_markets: list[dict], balance: float) -> TradeDecision | None:
    """
    For a set of bracket contracts on the same event: if sum(yes_prices) + total_fees < 1.0,
    buying YES on all brackets guarantees profit. Trade the cheapest bracket as our entry.
    """
    if len(event_markets) < 3:
        return None
    # Only bracket contracts
    brackets = [m for m in event_markets if m.get("strike_type") == "between"]
    if len(brackets) < 3:
        return None

    total_yes = sum(m.get("yes_price", 0) or 0 for m in brackets)
    total_fees = sum(_kalshi_fee(m.get("yes_price", 0) or 0) for m in brackets)
    cost = total_yes + total_fees
    edge = 1.0 - cost

    if edge <= 0.02:  # Need >2% edge to overcome friction on multiple legs
        return None

    # Buy the bracket most likely to hit (cheapest NO = highest YES relative to fair)
    # In practice, buy the bracket closest to current price
    best = max(brackets, key=lambda m: m.get("yes_price", 0) or 0)
    yes_price = best.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    confidence = min(yes_price + edge * 0.5, 0.95)
    amount = _size_for_strategy("cross_strike_arb", confidence, yes_price, balance, "YES")
    if amount is None:
        return None

    return TradeDecision(
        market_id=best["market_id"], question=best.get("question", ""),
        direction="YES", confidence=confidence,
        reasoning=f"XStrikeArb: {len(brackets)} brackets sum={total_yes:.3f} fees={total_fees:.3f} edge={edge:.3f}",
        strategy="cross_strike_arb", amount_usd=amount, entry_price=yes_price,
        shares=amount / yes_price if yes_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "bracket_count": len(brackets), "total_yes": total_yes,
                  "total_fees": total_fees, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 5: Adjacent bracket cone (amplifies fair_value by spreading bets)
# ---------------------------------------------------------------------------

def _check_bracket_cone(event_markets: list[dict], balance: float,
                        spot_prices: dict) -> list[TradeDecision]:
    """
    Find the 2-3 brackets closest to current spot and buy YES on each.
    Data shows 0.10-0.30 entry price range = $101 avg profit.
    This spreads the hit zone so crypto doesn't need to land in one exact bracket.
    """
    brackets = [m for m in event_markets if m.get("strike_type") == "between"]
    if len(brackets) < 5:
        return []

    # Find underlying
    underlying_id = None
    for m in brackets:
        underlying_id = _find_underlying(m)
        if underlying_id:
            break
    if not underlying_id:
        return []

    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return []

    # Sort brackets by distance from spot (closest first)
    def bracket_dist(m):
        f = m.get("floor_strike") or 0
        c = m.get("cap_strike") or 0
        if f <= 0 or c <= 0:
            return float('inf')
        mid = (f + c) / 2
        return abs(spot - mid)

    sorted_brackets = sorted(brackets, key=bracket_dist)

    # Take the 3 closest brackets with YES in sweet spot (0.05-0.40)
    decisions = []
    for m in sorted_brackets[:5]:
        yes_price = m.get("yes_price", 0) or 0
        if not (0.05 <= yes_price <= 0.40):
            continue

        minutes = _parse_expiry_minutes(m)
        if minutes is None or minutes <= 2:
            continue

        vol = _get_realtime_vol(underlying_id)
        floor_s = m.get("floor_strike") or 0
        cap_s = m.get("cap_strike") or 0
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
        if fair is None:
            continue

        fee = _kalshi_fee(yes_price)
        edge = fair - (yes_price + fee)
        if edge <= MIN_FAIR_VALUE_EDGE * 0.5:  # Lower threshold for cone — diversification is the hedge
            continue

        confidence = min(fair, 0.85)
        amount = _size_for_strategy("bracket_cone", confidence, yes_price, balance, "YES")
        if amount is None:
            continue

        decisions.append(TradeDecision(
            market_id=m["market_id"], question=m.get("question", ""),
            direction="YES", confidence=confidence,
            reasoning=f"Cone: spot=${spot:,.0f} [{floor_s:,.0f}-{cap_s:,.0f}] fair={fair:.4f} mkt={yes_price:.3f} edge={edge:.3f}",
            strategy="bracket_cone", amount_usd=amount, entry_price=yes_price,
            shares=amount / yes_price if yes_price > 0 else 0,
            decision_generated_at_ms=time.time() * 1000,
            metadata={"edge": edge, "fair_value": fair, "spot": spot,
                      "venue": "kalshi", "cone_size": len(decisions) + 1},
        ))

        if len(decisions) >= 3:  # Max 3 brackets per cone
            break

    return decisions


# ---------------------------------------------------------------------------
# Strategy 6: Mean reversion (15-min crypto, bet against crowd at coin-flip)
# ---------------------------------------------------------------------------

def _check_mean_reversion(market, balance, spot_prices):
    """When fair value ≈ 0.50 but market disagrees, bet against the crowd."""
    if market.get("venue") != "kalshi":
        return None
    strike_type = market.get("strike_type", "")
    if strike_type == "between":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 1 or minutes > 30:  # Only fast markets
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    strike = market.get("floor_strike") if strike_type == "greater_or_equal" else market.get("cap_strike")
    if not strike or strike <= 0:
        return None

    vol = CRYPTO_VOL.get(underlying_id, 0.70)
    fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)
    if fair is None:
        return None

    # Only trigger in the coin-flip zone: fair value 0.40-0.60
    if not (0.40 <= fair <= 0.60):
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.05 or yes_price >= 0.95:
        return None

    # The crowd is overpricing one side. Bet against them.
    deviation = yes_price - fair
    fee = _kalshi_fee(yes_price)

    if deviation > MIN_REVERSION_EDGE:
        # Market overprices YES → buy NO
        direction = "NO"
        entry_price = 1.0 - yes_price
        edge = deviation - fee
        confidence = min(1.0 - fair, 0.90)
    elif deviation < -MIN_REVERSION_EDGE:
        # Market underprices YES → buy YES
        direction = "YES"
        entry_price = yes_price
        edge = abs(deviation) - fee
        confidence = min(fair, 0.90)
    else:
        return None

    if edge <= MIN_EDGE:
        return None

    amount = _size_for_strategy("mean_reversion", confidence, entry_price, balance, direction)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"MeanRev: fair={fair:.3f} mkt={yes_price:.3f} dev={deviation:+.3f} edge={edge:.3f} [{underlying_id}]",
        strategy="mean_reversion", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "fair_value": fair, "deviation": deviation,
                  "spot": spot, "strike": strike, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 6: Time decay selling (very near expiry, spot ≈ strike)
# ---------------------------------------------------------------------------

def _check_time_decay(market, balance, spot_prices):
    """
    Very near expiry (<10 min), spot ≈ strike → contract should be ~0.50.
    Buy whichever side is cheaper. Most 15-min windows are boring.
    """
    if market.get("venue") != "kalshi":
        return None
    strike_type = market.get("strike_type", "")
    if strike_type == "between":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 1 or minutes > 10:
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    strike = market.get("floor_strike") if strike_type == "greater_or_equal" else market.get("cap_strike")
    if not strike or strike <= 0:
        return None

    vol = CRYPTO_VOL.get(underlying_id, 0.70)
    fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)
    if fair is None:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.05 or yes_price >= 0.95:
        return None
    no_price = 1.0 - yes_price

    # Buy the cheaper side — time decay favors whoever paid less
    fee_yes = _kalshi_fee(yes_price)
    fee_no = _kalshi_fee(no_price)

    # Expected value: if we buy YES at yes_price, expected payout = fair * 1.0
    ev_yes = fair - (yes_price + fee_yes)
    ev_no = (1.0 - fair) - (no_price + fee_no)

    if ev_yes > MIN_DECAY_EDGE and ev_yes >= ev_no:
        direction, entry_price, edge = "YES", yes_price, ev_yes
        confidence = min(fair, 0.90)
    elif ev_no > MIN_DECAY_EDGE:
        direction, entry_price, edge = "NO", no_price, ev_no
        confidence = min(1.0 - fair, 0.90)
    else:
        return None

    amount = _size_for_strategy("time_decay", confidence, entry_price, balance, direction)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"TimeDecay: {minutes:.0f}m left fair={fair:.3f} mkt={yes_price:.3f} edge={edge:.3f} [{underlying_id}]",
        strategy="time_decay", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "fair_value": fair, "minutes_to_expiry": minutes,
                  "spot": spot, "strike": strike, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 7: Volatility skew (realized > implied → buy OTM)
# ---------------------------------------------------------------------------

def _check_vol_skew(market, balance, spot_prices):
    """
    If realized vol >> implied vol, out-of-the-money contracts are underpriced.
    Buy cheap OTM contracts — they hit more often than the market thinks.
    """
    if market.get("venue") != "kalshi":
        return None
    strike_type = market.get("strike_type", "")
    if strike_type == "between":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 5 or minutes > 60:
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    strike = market.get("floor_strike") if strike_type == "greater_or_equal" else market.get("cap_strike")
    if not strike or strike <= 0:
        return None

    realized_vol = CRYPTO_VOL.get(underlying_id, 0.70)
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.03 or yes_price >= 0.97:
        return None

    # Compute implied vol from market price (reverse Black-Scholes)
    # fair_value(spot, strike, minutes, implied_vol) = yes_price
    # Binary search for implied_vol
    implied_vol = _solve_implied_vol(spot, strike, minutes, yes_price, strike_type)
    if implied_vol is None or implied_vol <= 0:
        return None

    vol_ratio = realized_vol / implied_vol
    if vol_ratio <= 1.2:  # Need at least 20% vol discount
        return None

    # Realized vol is higher than implied → tails are underpriced
    # If OTM (fair value < 0.30 or > 0.70), buy the OTM side
    fair_at_realized = _compute_fair_value(spot, strike, minutes, realized_vol, strike_type)
    if fair_at_realized is None:
        return None

    fee_yes = _kalshi_fee(yes_price)
    no_price = 1.0 - yes_price
    fee_no = _kalshi_fee(no_price)

    edge_yes = fair_at_realized - (yes_price + fee_yes)
    edge_no = (1.0 - fair_at_realized) - (no_price + fee_no)

    if edge_yes > MIN_VOL_SKEW_EDGE and edge_yes >= edge_no:
        direction, entry_price, edge = "YES", yes_price, edge_yes
        confidence = min(fair_at_realized, 0.90)
    elif edge_no > MIN_VOL_SKEW_EDGE:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair_at_realized, 0.90)
    else:
        return None

    amount = _size_for_strategy("vol_skew", confidence, entry_price, balance, direction)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"VolSkew: realized={realized_vol:.2f} implied={implied_vol:.2f} ratio={vol_ratio:.2f} edge={edge:.3f}",
        strategy="vol_skew", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "realized_vol": realized_vol, "implied_vol": implied_vol,
                  "vol_ratio": vol_ratio, "venue": "kalshi"},
    )


def _solve_implied_vol(spot, strike, minutes, target_price, strike_type, tol=0.001, max_iter=20):
    """Binary search for implied vol that produces target_price."""
    lo, hi = 0.05, 3.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        fv = _compute_fair_value(spot, strike, minutes, mid, strike_type)
        if fv is None:
            return None
        if abs(fv - target_price) < tol:
            return mid
        if fv > target_price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Strategy 8: Closing convergence (price should approach 0 or 1 near expiry)
# ---------------------------------------------------------------------------

def _check_closing_convergence(market, balance):
    """
    Near expiry (<2h), prices should converge toward 0 or 1.
    If YES is 0.85 with 1h left, it should be heading toward 1.0.
    Buy the dominant side — momentum toward resolution.
    """
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 120:
        return None
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.05 or yes_price >= 0.95:
        return None
    venue = market.get("venue", "polymarket")
    fee = _venue_fee(yes_price, venue)
    hours = minutes / 60.0
    # Convergence strength: closer to expiry + more extreme price = stronger
    if yes_price >= 0.75:
        direction, entry_price = "YES", yes_price
        # Expected convergence: price should approach 1.0
        convergence_target = 1.0 - (1.0 - yes_price) * (hours / 2.0)  # Linear decay
        edge = convergence_target - (yes_price + fee)
        confidence = min(convergence_target, 0.92)
    elif yes_price <= 0.25:
        direction, entry_price = "NO", 1.0 - yes_price
        convergence_target = 1.0 - yes_price * (hours / 2.0)
        edge = convergence_target - (entry_price + _venue_fee(entry_price, venue))
        confidence = min(convergence_target, 0.92)
    else:
        return None
    if edge <= 0.01:
        return None
    amount = _size_for_strategy("closing_convergence", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"Converge: {hours:.1f}h left yes={yes_price:.3f} target={convergence_target:.3f} edge={edge:.3f} [{venue}]",
        strategy="closing_convergence", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "hours_to_expiry": hours, "venue": venue},
    )


# ---------------------------------------------------------------------------
# Strategy 9: Bracket neighbor mispricing
# ---------------------------------------------------------------------------

def _check_bracket_neighbor(event_markets: list[dict], balance: float,
                            spot_prices: dict) -> list[TradeDecision]:
    """
    Adjacent brackets should have smoothly transitioning prices.
    When one bracket is cheap relative to its neighbors, buy it.
    """
    brackets = [m for m in event_markets if m.get("strike_type") == "between"
                and m.get("floor_strike") and m.get("yes_price")]
    if len(brackets) < 5:
        return []
    brackets.sort(key=lambda m: m.get("floor_strike", 0))

    decisions = []
    for i in range(1, len(brackets) - 1):
        prev_yes = brackets[i-1].get("yes_price", 0) or 0
        curr_yes = brackets[i].get("yes_price", 0) or 0
        next_yes = brackets[i+1].get("yes_price", 0) or 0
        if prev_yes <= 0 or curr_yes <= 0 or next_yes <= 0:
            continue
        # Expected price: average of neighbors
        expected = (prev_yes + next_yes) / 2
        discount = expected - curr_yes
        fee = _kalshi_fee(curr_yes)
        if discount > fee + 0.02 and 0.03 <= curr_yes <= 0.40:
            # This bracket is cheap relative to neighbors → buy YES
            confidence = min(expected, 0.85)
            amount = _size_for_strategy("bracket_neighbor", confidence, curr_yes, balance, "YES")
            if amount:
                m = brackets[i]
                decisions.append(TradeDecision(
                    market_id=m["market_id"], question=m.get("question", ""),
                    direction="YES", confidence=confidence,
                    reasoning=f"Neighbor: prev={prev_yes:.3f} curr={curr_yes:.3f} next={next_yes:.3f} expected={expected:.3f} discount={discount:.3f}",
                    strategy="bracket_neighbor", amount_usd=amount, entry_price=curr_yes,
                    shares=amount / curr_yes if curr_yes > 0 else 0,
                    decision_generated_at_ms=time.time() * 1000,
                    metadata={"edge": discount - fee, "venue": "kalshi"},
                ))
            if len(decisions) >= 2:
                break
    return decisions


# ---------------------------------------------------------------------------
# Strategy 10: Expiry acceleration (amplified fair_value in last 5 min)
# ---------------------------------------------------------------------------

def _check_expiry_acceleration(market, balance, spot_prices):
    """
    In the last 5 minutes, our fair value model is extremely accurate
    (low vol = sharp distribution). Trade more aggressively with higher confidence.
    """
    if market.get("venue") != "kalshi":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 0.5 or minutes > 5:
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    vol = _get_realtime_vol(underlying_id)
    strike_type = market.get("strike_type", "")

    if strike_type == "between":
        floor_s = market.get("floor_strike")
        cap_s = market.get("cap_strike")
        if not floor_s or not cap_s:
            return None
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    elif strike_type in ("greater_or_equal", "greater"):
        strike = market.get("floor_strike")
        if not strike:
            return None
        fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)
    else:
        return None

    if fair is None:
        return None
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    fee = _kalshi_fee(yes_price)
    no_price = 1.0 - yes_price
    edge_yes = fair - (yes_price + fee)
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))

    # Lower threshold for acceleration — we're very confident near expiry
    min_edge = 0.008
    if edge_yes > min_edge and edge_yes >= edge_no:
        direction, entry_price, confidence, edge = "YES", yes_price, min(fair, 0.93), edge_yes
    elif edge_no > min_edge:
        direction, entry_price, confidence, edge = "NO", no_price, min(1.0 - fair, 0.93), edge_no
    else:
        return None

    amount = _size_for_strategy("expiry_acceleration", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"ExpiryAccel: {minutes:.1f}m left fair={fair:.4f} mkt={yes_price:.3f} edge={edge:.4f}",
        strategy="expiry_acceleration", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "minutes_to_expiry": minutes, "fair_value": fair, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 11: Correlation echo (BTC signal → ETH trade, vice versa)
# ---------------------------------------------------------------------------

def _check_correlation_echo(market, balance, spot_prices, active_signals: set):
    """
    BTC and ETH are ~85% correlated. If we just found a strong signal on BTC,
    echo a weaker version to the equivalent ETH bracket (and vice versa).
    """
    if market.get("venue") != "kalshi" or market.get("strike_type") != "between":
        return None
    mid = market.get("market_id", "")
    underlying = _find_underlying(market)
    if not underlying:
        return None

    # Check if the correlated asset has a recent signal
    correlated = "ethereum" if underlying == "bitcoin" else ("bitcoin" if underlying == "ethereum" else None)
    if not correlated:
        return None

    # Look for active signals on the correlated asset
    has_correlated_signal = any(correlated in sig for sig in active_signals)
    if not has_correlated_signal:
        return None

    spot = spot_prices.get(underlying)
    if not spot or spot <= 0:
        return None

    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2:
        return None

    vol = _get_realtime_vol(underlying)
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    if not floor_s or not cap_s:
        return None

    fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    if fair is None:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    no_price = 1.0 - yes_price
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))
    # Echo trades are weaker — need higher edge threshold
    if edge_no > 0.03:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair, 0.85)
    else:
        return None

    amount = _size_for_strategy("correlation_echo", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"CorrEcho: {correlated} signal → {underlying} echo, fair={fair:.4f} edge={edge:.3f}",
        strategy="correlation_echo", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "correlated_asset": correlated, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 12: Polymarket convergence (near-resolution markets)
# ---------------------------------------------------------------------------

def _check_polymarket_convergence(market, balance):
    """
    Polymarket markets with extreme prices — buy the dominant side.
    Works on both near-expiry AND longer-dated heavy favorites.
    "Will Charlotte Hornets win NBA Finals?" at YES=0.01 → buy NO at 0.99.
    """
    if market.get("venue") != "polymarket":
        return None
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.005 or yes_price >= 0.995:
        return None  # Too extreme, likely no liquidity
    fee = _fee(yes_price)

    minutes = _parse_expiry_minutes(market)
    hours = minutes / 60.0 if minutes and minutes > 0 else 999

    if yes_price >= 0.85:
        direction, entry_price = "YES", yes_price
        edge = (1.0 - yes_price) * 0.7 - fee
        confidence = min(yes_price, 0.93)
    elif yes_price <= 0.15:
        direction, entry_price = "NO", 1.0 - yes_price
        edge = yes_price * 0.7 - fee
        confidence = min(1.0 - yes_price, 0.93)
    elif yes_price >= 0.75 and hours < 168:  # Moderate favorite + <1 week
        direction, entry_price = "YES", yes_price
        edge = (1.0 - yes_price) * 0.5 - fee
        confidence = min(yes_price, 0.90)
    elif yes_price <= 0.25 and hours < 168:
        direction, entry_price = "NO", 1.0 - yes_price
        edge = yes_price * 0.5 - fee
        confidence = min(1.0 - yes_price, 0.90)
    else:
        return None

    if edge <= 0.005:
        return None

    # Smaller positions on longer-dated markets (capital lock risk)
    if hours > 168:  # >1 week
        edge *= 0.3  # Heavily discount for time lock

    amount = _size_for_strategy("polymarket_convergence", confidence, entry_price, balance, direction)
    if amount is None:
        # Kelly breaks down at extreme prices (YES at 0.88 → Kelly=0).
        # Use flat bet: 0.5% of balance for convergence plays.
        amount = balance * 0.005
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"PolyConv: yes={yes_price:.3f} {hours:.0f}h left edge={edge:.3f}",
        strategy="polymarket_convergence", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "hours_to_expiry": hours, "venue": "polymarket"},
    )


# ---------------------------------------------------------------------------
# Strategy 13: Liquidity fade (trade against illiquid brackets)
# ---------------------------------------------------------------------------

def _check_liquidity_fade(market, balance, spot_prices):
    """
    Illiquid brackets (wide bid-ask spread) are more likely mispriced.
    When our fair value diverges from an illiquid bracket, trade it —
    the market maker hasn't bothered to update.
    """
    if market.get("venue") != "kalshi" or market.get("strike_type") != "between":
        return None
    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    if yes_bid is None or yes_ask is None or yes_bid <= 0 or yes_ask <= 0:
        return None
    spread = yes_ask - yes_bid
    if spread < 0.03:  # Only target wide-spread (illiquid) markets
        return None

    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2:
        return None

    vol = _get_realtime_vol(underlying_id)
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    if not floor_s or not cap_s:
        return None

    fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    if fair is None:
        return None

    # Use midpoint as market price (illiquid = wide spread)
    mid_price = (yes_bid + yes_ask) / 2
    no_mid = 1.0 - mid_price

    edge_yes = fair - (yes_ask + _kalshi_fee(yes_ask))  # Buy at ask
    edge_no = (1.0 - fair) - (no_mid + _kalshi_fee(no_mid))

    if edge_yes > 0.02 and edge_yes >= edge_no and yes_ask <= 0.35:
        direction, entry_price, edge = "YES", yes_ask, edge_yes
        confidence = min(fair, 0.85)
    elif edge_no > 0.02:
        direction, entry_price, edge = "NO", no_mid, edge_no
        confidence = min(1.0 - fair, 0.85)
    else:
        return None

    amount = _size_for_strategy("liquidity_fade", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"LiqFade: spread={spread:.3f} fair={fair:.4f} mid={mid_price:.3f} edge={edge:.3f}",
        strategy="liquidity_fade", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "spread": spread, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 14: Volume-confirmed trades (only trade brackets with real volume)
# ---------------------------------------------------------------------------

MIN_VOLUME_THRESHOLD = float(os.environ.get("RIVALCLAW_MIN_VOLUME", "50"))

def _check_volume_confirmed(market, balance, spot_prices):
    """
    Fair value on brackets that have REAL volume. High-volume brackets
    have better price discovery = less risk of total wipeout.
    Low-volume brackets are where we get killed (-$546 on illiquid 15-min).
    """
    if market.get("venue") != "kalshi" or market.get("strike_type") != "between":
        return None
    volume = market.get("volume", 0) or market.get("volume_24h", 0) or 0
    if volume < MIN_VOLUME_THRESHOLD:
        return None

    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2:
        return None

    vol = _get_realtime_vol(underlying_id)
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    if not floor_s or not cap_s:
        return None
    fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    if fair is None:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    no_price = 1.0 - yes_price
    fee = _kalshi_fee(yes_price)
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))

    # Only NO bets in the sweet spot — volume-confirmed + NO bias
    thresh = MIN_FAIR_VALUE_EDGE * _bucket_multiplier(no_price)
    if edge_no > thresh and 0.10 <= no_price <= 0.35:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair, 0.90)
    else:
        return None

    amount = _size_for_strategy("volume_confirmed", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"VolConf: vol={volume:.0f} fair={fair:.4f} mkt={yes_price:.3f} edge={edge:.3f} [{underlying_id}]",
        strategy="volume_confirmed", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "volume": volume, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 15: Wipeout reversal (after 15-min contract wipes, next one reverts)
# ---------------------------------------------------------------------------

def _check_wipeout_reversal(market, balance, spot_prices):
    """
    After a 15-min crypto contract goes to 0 (wipeout), the NEXT window
    often overreacts in the opposite direction. Bet on mean reversion
    across consecutive 15-min windows.
    """
    if market.get("venue") != "kalshi":
        return None
    mid = market.get("market_id", "")
    if "15M" not in mid:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 15:
        return None

    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None

    strike = market.get("floor_strike")
    if not strike or strike <= 0:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.05 or yes_price >= 0.95:
        return None

    # Check if spot is very close to strike (the "coin flip" zone)
    # In this zone, the market tends to overreact to the last window's outcome
    pct_from_strike = abs(spot - strike) / strike
    if pct_from_strike > 0.005:  # Only when spot is within 0.5% of strike
        return None

    # Bet toward 0.50 — if market is pricing away from 0.50, bet the other way
    fair = 0.50  # Coin flip when spot ≈ strike
    fee = _kalshi_fee(yes_price)

    if yes_price > 0.55:
        direction, entry_price = "NO", 1.0 - yes_price
        edge = (yes_price - fair) - fee
        confidence = min(0.55, 0.85)
    elif yes_price < 0.45:
        direction, entry_price = "YES", yes_price
        edge = (fair - yes_price) - fee
        confidence = min(0.55, 0.85)
    else:
        return None

    if edge <= 0.01:
        return None

    amount = _size_for_strategy("wipeout_reversal", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"WipeoutRev: spot≈strike ({pct_from_strike:.3%}) yes={yes_price:.3f} fair=0.50 edge={edge:.3f} [{underlying_id}]",
        strategy="wipeout_reversal", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "pct_from_strike": pct_from_strike, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 16: Multi-timeframe consensus (15-min + daily agree = high confidence)
# ---------------------------------------------------------------------------

def _check_multi_timeframe(market, balance, spot_prices, all_decisions: list):
    """
    If a 15-min contract AND a daily contract on the same underlying
    both signal the same direction, trade with boosted confidence.
    Consensus across timeframes = stronger signal.
    """
    if market.get("venue") != "kalshi":
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None

    # Check if we already have a decision on this underlying from a different timeframe
    my_timeframe = "fast" if "15M" in market.get("market_id", "") else "slow"
    opposite_tf = "slow" if my_timeframe == "fast" else "fast"

    matching_direction = None
    for d in all_decisions:
        d_underlying = (d.metadata or {}).get("underlying") or ""
        d_mid = d.market_id or ""
        # Check if same underlying, different timeframe
        if underlying_id in d_mid or underlying_id.replace("-", "") in d_mid.lower():
            d_tf = "fast" if "15M" in d_mid else "slow"
            if d_tf == opposite_tf:
                matching_direction = d.direction
                break

    if not matching_direction:
        return None

    # We have consensus — compute fair value and trade with boosted confidence
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2:
        return None

    vol = _get_realtime_vol(underlying_id)
    strike_type = market.get("strike_type", "")
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    if strike_type == "between":
        floor_s, cap_s = market.get("floor_strike"), market.get("cap_strike")
        if not floor_s or not cap_s:
            return None
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    else:
        strike = market.get("floor_strike") or market.get("cap_strike")
        if not strike:
            return None
        fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)

    if fair is None:
        return None

    no_price = 1.0 - yes_price
    fee = _kalshi_fee(yes_price)
    edge_yes = fair - (yes_price + fee)
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))

    # Only trade in the consensus direction with lower threshold (0.5x)
    if matching_direction == "NO" and edge_no > MIN_FAIR_VALUE_EDGE * 0.5:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair + 0.05, 0.93)  # Boosted by consensus
    elif matching_direction == "YES" and edge_yes > MIN_FAIR_VALUE_EDGE * 0.5:
        direction, entry_price, edge = "YES", yes_price, edge_yes
        confidence = min(fair + 0.05, 0.93)
    else:
        return None

    amount = _size_for_strategy("multi_timeframe", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"MultiTF: {my_timeframe}+{opposite_tf} consensus={matching_direction} edge={edge:.3f}",
        strategy="multi_timeframe", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "consensus": matching_direction, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Hedge engine — turns naked bets into defined-risk spreads
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Strategy 17: Election field arb (buy NO on every underdog in multi-candidate)
# ---------------------------------------------------------------------------

def _check_election_field(markets: list[dict], balance: float) -> list[TradeDecision]:
    """
    For multi-candidate elections: only one can win. Buy NO on every underdog.
    12 candidates, buy NO on 11 → at most 1 loses. Structural profit.
    """
    # Group Polymarket markets by election (match by question pattern)
    from collections import defaultdict
    import re
    elections = defaultdict(list)
    for m in markets:
        if m.get("venue") != "polymarket":
            continue
        q = m.get("question", "")
        # Match "Will X win the YYYY EVENT?" pattern
        match = re.search(r'win the (\d{4} .+?)[\?$]', q)
        if match:
            elections[match.group(1)].append(m)

    decisions = []
    for event, candidates in elections.items():
        if len(candidates) < 4:  # Need multi-candidate field
            continue
        # Buy NO on every underdog (YES < 0.15)
        for m in candidates:
            yes = m.get("yes_price", 0) or 0
            if yes >= 0.15 or yes <= 0.005:  # Skip favorites and zero-liquidity
                continue
            no_price = 1.0 - yes
            fee = _fee(no_price)
            edge = yes * 0.9 - fee  # 90% of the time this candidate loses
            if edge <= 0.005:
                continue
            confidence = min(no_price, 0.93)
            amount = balance * 0.003  # Tiny bets — many positions
            if amount < 1:
                continue
            decisions.append(TradeDecision(
                market_id=m["market_id"], question=m.get("question", ""),
                direction="NO", confidence=confidence,
                reasoning=f"FieldArb: {len(candidates)} candidates, {event[:30]} yes={yes:.3f} edge={edge:.3f}",
                strategy="election_field_arb", amount_usd=amount, entry_price=no_price,
                shares=amount / no_price if no_price > 0 else 0,
                decision_generated_at_ms=time.time() * 1000,
                metadata={"edge": edge, "field_size": len(candidates), "venue": "polymarket"},
            ))
    return decisions[:15]  # Cap at 15 per cycle


# ---------------------------------------------------------------------------
# Strategy 18: Vol regime switching (vol spike → buy OTM)
# ---------------------------------------------------------------------------

def _check_vol_regime(market, balance, spot_prices):
    """
    When realized vol jumps >2x in 30 min, market prices lag.
    Buy OTM brackets that are now more likely under higher vol.
    """
    if market.get("venue") != "kalshi" or market.get("strike_type") != "between":
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None

    # Compare recent vol (last 30min) vs baseline vol
    recent_vol = _get_realtime_vol(underlying_id)  # Last 200 snapshots
    baseline_vol = CRYPTO_VOL.get(underlying_id, 0.70)

    vol_spike = recent_vol / baseline_vol if baseline_vol > 0 else 1.0
    if vol_spike < 1.5:  # Need 50%+ vol increase
        return None

    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 60:
        return None

    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    if not floor_s or not cap_s:
        return None

    # Under high vol, OTM brackets become more likely
    fair_high_vol = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, recent_vol)
    fair_low_vol = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, baseline_vol)
    if fair_high_vol is None or fair_low_vol is None:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    # Market is pricing at low vol, but real vol is high
    edge = fair_high_vol - (yes_price + _kalshi_fee(yes_price))
    if edge <= 0.02:
        return None

    # Only buy YES on OTM brackets (cheap ones that high vol makes more likely)
    if yes_price > 0.30:
        return None

    confidence = min(fair_high_vol, 0.85)
    amount = _size_for_strategy("vol_regime", confidence, yes_price, balance, "YES")
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction="YES", confidence=confidence,
        reasoning=f"VolRegime: spike={vol_spike:.1f}x fair_hi={fair_high_vol:.4f} fair_lo={fair_low_vol:.4f} mkt={yes_price:.3f} edge={edge:.3f}",
        strategy="vol_regime", amount_usd=amount, entry_price=yes_price,
        shares=amount / yes_price if yes_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "vol_spike": vol_spike, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 19: Correlation cascade (BTC resolves → trade ETH before it catches up)
# ---------------------------------------------------------------------------

def _check_correlation_cascade(market, balance, spot_prices, spot_history):
    """
    BTC and ETH are ~85% correlated with ~15min lag.
    When BTC spot moves sharply, ETH contracts haven't repriced yet. Trade ETH.
    """
    if market.get("venue") != "kalshi":
        return None
    underlying_id = _find_underlying(market)
    if underlying_id != "ethereum":  # Only trade ETH based on BTC signal
        return None

    # Check BTC momentum (the leading asset)
    btc_history = spot_history.get("bitcoin", [])
    if len(btc_history) < 5:
        return None
    btc_move = (btc_history[-1] - btc_history[0]) / btc_history[0] if btc_history[0] > 0 else 0

    if abs(btc_move) < 0.003:  # BTC needs to move >0.3%
        return None

    # ETH should follow but hasn't yet — compute fair value with ETH spot
    spot = spot_prices.get("ethereum")
    if not spot or spot <= 0:
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 30:
        return None

    vol = _get_realtime_vol("ethereum")
    strike_type = market.get("strike_type", "")
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    if strike_type == "between":
        floor_s, cap_s = market.get("floor_strike"), market.get("cap_strike")
        if not floor_s or not cap_s:
            return None
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, vol)
    else:
        strike = market.get("floor_strike") or market.get("cap_strike")
        if not strike:
            return None
        fair = _compute_fair_value(spot, strike, minutes, vol, strike_type)

    if fair is None:
        return None

    no_price = 1.0 - yes_price
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))
    edge_yes = fair - (yes_price + _kalshi_fee(yes_price))

    min_edge = 0.015
    if edge_no > min_edge and edge_no >= edge_yes:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair, 0.88)
    elif edge_yes > min_edge:
        direction, entry_price, edge = "YES", yes_price, edge_yes
        confidence = min(fair, 0.88)
    else:
        return None

    amount = _size_for_strategy("correlation_cascade", confidence, entry_price, balance, direction)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"CorrCascade: BTC {btc_move:+.2%} → ETH fair={fair:.4f} mkt={yes_price:.3f} edge={edge:.3f}",
        strategy="correlation_cascade", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "btc_move": btc_move, "venue": "kalshi"},
    )


# ---------------------------------------------------------------------------
# Strategy 20: Pairs trading (long one bracket, short adjacent)
# ---------------------------------------------------------------------------

def _check_pairs_trade(event_markets: list[dict], balance: float,
                       spot_prices: dict) -> list[TradeDecision]:
    """
    Go YES on one bracket + NO on adjacent bracket = bet on which half
    the price lands in. Market neutral — profits from relative mispricing.
    """
    brackets = sorted(
        [m for m in event_markets if m.get("strike_type") == "between"
         and m.get("floor_strike") and m.get("yes_price")],
        key=lambda m: m.get("floor_strike", 0)
    )
    if len(brackets) < 5:
        return []

    underlying_id = _find_underlying(brackets[0]) if brackets else None
    if not underlying_id:
        return []
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return []
    minutes = _parse_expiry_minutes(brackets[0])
    if minutes is None or minutes <= 2:
        return []
    vol = _get_realtime_vol(underlying_id)

    decisions = []
    for i in range(len(brackets) - 1):
        a, b = brackets[i], brackets[i + 1]
        a_yes = a.get("yes_price", 0) or 0
        b_yes = b.get("yes_price", 0) or 0
        if a_yes <= 0.02 or b_yes <= 0.02:
            continue

        # Compute fair values
        a_fair = _compute_bracket_fair_value(
            spot, a.get("floor_strike"), a.get("cap_strike"), minutes, vol)
        b_fair = _compute_bracket_fair_value(
            spot, b.get("floor_strike"), b.get("cap_strike"), minutes, vol)
        if a_fair is None or b_fair is None:
            continue

        # Relative mispricing: if A is cheap relative to B vs fair ratio
        if a_fair > 0 and b_fair > 0:
            fair_ratio = a_fair / b_fair
            market_ratio = a_yes / b_yes if b_yes > 0 else 0
            if market_ratio > 0 and abs(fair_ratio - market_ratio) / fair_ratio > 0.30:
                # Significant relative mispricing
                if fair_ratio > market_ratio:
                    # A is underpriced relative to B → buy A YES
                    edge = (a_fair - a_yes) - _kalshi_fee(a_yes)
                    if edge > 0.01 and a_yes <= 0.35:
                        amount = _size_for_strategy("pairs_trade", min(a_fair, 0.85), a_yes, balance, "YES")
                        if amount:
                            decisions.append(TradeDecision(
                                market_id=a["market_id"], question=a.get("question", ""),
                                direction="YES", confidence=min(a_fair, 0.85),
                                reasoning=f"Pairs: A/B ratio fair={fair_ratio:.2f} mkt={market_ratio:.2f} edge={edge:.3f}",
                                strategy="pairs_trade", amount_usd=amount, entry_price=a_yes,
                                shares=amount / a_yes if a_yes > 0 else 0,
                                decision_generated_at_ms=time.time() * 1000,
                                metadata={"edge": edge, "venue": "kalshi"},
                            ))
        if len(decisions) >= 3:
            break
    return decisions


# ---------------------------------------------------------------------------
# Strategy 21: NWS forecast delta (trade weather gaps after forecast updates)
# ---------------------------------------------------------------------------

def _check_forecast_delta(market, balance):
    """
    When NWS forecast changes, weather market prices lag.
    If forecast shifted from 65→68°F but market still prices ">67°" at 50%,
    it should be higher.
    """
    if market.get("venue") != "kalshi":
        return None
    weather_city = _find_weather_city(market)
    if not weather_city:
        return None

    spot, vol = _get_weather_spot_and_vol(weather_city)
    if not spot:
        return None

    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 0:
        return None

    strike_type = market.get("strike_type", "")
    if strike_type == "between":
        floor_s = market.get("floor_strike")
        cap_s = market.get("cap_strike")
        if not floor_s or not cap_s:
            return None
        # Weather vol: forecast error / spot as fraction, scaled
        weather_vol = vol / spot if spot > 0 else 0.05
        scaled_vol = weather_vol * math.sqrt(365.25 * 24 * 60 / max(minutes, 1))
        fair = _compute_bracket_fair_value(spot, floor_s, cap_s, minutes, scaled_vol)
    elif strike_type in ("greater", "greater_or_equal", "less"):
        strike = market.get("floor_strike") if strike_type != "less" else market.get("cap_strike")
        if not strike:
            return None
        weather_vol = vol / spot if spot > 0 else 0.05
        scaled_vol = weather_vol * math.sqrt(365.25 * 24 * 60 / max(minutes, 1))
        fair = _compute_fair_value(spot, strike, minutes, scaled_vol, strike_type)
    else:
        return None

    if fair is None:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    fee = _kalshi_fee(yes_price)
    no_price = 1.0 - yes_price
    edge_yes = fair - (yes_price + fee)
    edge_no = (1.0 - fair) - (no_price + _kalshi_fee(no_price))

    if edge_yes > 0.03 and edge_yes >= edge_no:
        direction, entry_price, edge = "YES", yes_price, edge_yes
        confidence = min(fair, 0.88)
    elif edge_no > 0.03:
        direction, entry_price, edge = "NO", no_price, edge_no
        confidence = min(1.0 - fair, 0.88)
    else:
        return None

    amount = _size_for_strategy("forecast_delta", confidence, entry_price, balance, direction)
    if amount is None:
        amount = balance * 0.003
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"ForecastDelta: {weather_city} forecast={spot:.0f}°F fair={fair:.3f} mkt={yes_price:.3f} edge={edge:.3f}",
        strategy="forecast_delta", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "forecast": spot, "venue": "kalshi"},
    )


HEDGE_RATIO = float(os.environ.get("RIVALCLAW_HEDGE_RATIO", "0.30"))  # Hedge = 30% of primary size
HEDGE_STRIKE_OFFSET = float(os.environ.get("RIVALCLAW_HEDGE_OFFSET", "0.02"))  # 2% OTM for hedge

def _find_hedge(primary: TradeDecision, markets_by_event: dict[str, list[dict]],
                balance: float) -> TradeDecision | None:
    """
    Find a hedging contract to cap downside on a primary trade.

    For Kalshi threshold contracts:
      - If primary is YES on "BTC > $71,000": hedge with NO on "BTC > $72,000"
        (if BTC drops below $71K we lose primary, but hedge was cheap)
      - If primary is NO on "BTC > $71,000": hedge with YES on "BTC > $70,000"
        (if BTC rises above $71K we lose primary, but hedge limits damage)

    For Polymarket / non-threshold: no hedge available (different market structure).
    """
    meta = primary.metadata or {}
    if meta.get("venue") != "kalshi":
        return None

    # Find the event group for this market
    evt = None
    for event_ticker, group in markets_by_event.items():
        for m in group:
            if m.get("market_id") == primary.market_id:
                evt = event_ticker
                break
        if evt:
            break
    if not evt:
        return None

    group = markets_by_event.get(evt, [])
    if len(group) < 2:
        return None

    primary_strike = meta.get("strike") or 0
    if primary_strike <= 0:
        return None

    # Find hedge contract: same event, different strike, opposite direction
    best_hedge = None
    best_hedge_cost = 1.0

    for m in group:
        if m.get("market_id") == primary.market_id:
            continue
        strike_type = m.get("strike_type", "")
        if strike_type == "between":
            continue  # Don't hedge with brackets

        h_strike = m.get("floor_strike") or m.get("cap_strike") or 0
        if h_strike <= 0:
            continue

        yes_price = m.get("yes_price", 0) or 0
        if yes_price <= 0.02 or yes_price >= 0.98:
            continue

        if primary.direction == "YES":
            # Primary is bullish → hedge is a cheaper bullish contract further OTM
            # Buy YES on a HIGHER strike (protective put equivalent)
            # If underlying drops, this also loses, but it was cheap
            # Actually, better: buy NO on a slightly higher strike
            # If underlying drops below primary strike, NO on higher strike also wins
            # NO: hedge with NO on a lower strike
            # Best hedge for YES on ">71K": buy YES on ">70K" (insurance floor)
            if h_strike < primary_strike and (primary_strike - h_strike) / primary_strike < 0.05:
                # Strike is 1-5% below primary — good insurance
                cost = yes_price + _kalshi_fee(yes_price)
                if cost < best_hedge_cost:
                    best_hedge = m
                    best_hedge_cost = cost
        else:
            # Primary is bearish (NO) → hedge with YES on the same or nearby strike
            # Buy YES on a higher strike as insurance
            if h_strike > primary_strike and (h_strike - primary_strike) / primary_strike < 0.05:
                cost = yes_price + _kalshi_fee(yes_price)
                if cost < best_hedge_cost:
                    best_hedge = m
                    best_hedge_cost = cost

    if best_hedge is None:
        return None

    # Size the hedge at HEDGE_RATIO of primary
    hedge_amount = primary.amount_usd * HEDGE_RATIO
    if hedge_amount < 5:  # Minimum $5 hedge
        return None

    # Hedge direction: for YES primary, buy YES on lower strike (insurance)
    # For NO primary, buy YES on higher strike (insurance)
    hedge_price = best_hedge.get("yes_price", 0) or 0
    if hedge_price <= 0.02 or hedge_price >= 0.98:
        return None

    direction = "YES"  # Hedges are always YES on the insurance strike
    entry_price = hedge_price
    hedge_strike = best_hedge.get("floor_strike") or best_hedge.get("cap_strike") or 0

    return TradeDecision(
        market_id=best_hedge["market_id"],
        question=best_hedge.get("question", ""),
        direction=direction,
        confidence=0.50,  # Low confidence — this is insurance, not a bet
        reasoning=(f"Hedge: protects {primary.market_id[:25]} {primary.direction} "
                   f"strike=${primary_strike:,.0f} → ins_strike=${hedge_strike:,.0f} "
                   f"cost=${hedge_amount:.0f} ({HEDGE_RATIO:.0%} of primary)"),
        strategy="hedge",
        amount_usd=hedge_amount,
        entry_price=entry_price,
        shares=hedge_amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": 0, "venue": "kalshi", "hedge_for": primary.market_id,
                  "primary_strike": primary_strike, "hedge_strike": hedge_strike},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _load_spot_history(lookback_minutes=10):
    """Load recent spot prices from DB for momentum detection."""
    import sqlite3
    from pathlib import Path
    db = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=lookback_minutes)).isoformat()
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT crypto_id, price_usd FROM spot_prices WHERE fetched_at > ? ORDER BY fetched_at ASC",
            (cutoff,)).fetchall()
        conn.close()
    except Exception:
        return {}
    history = defaultdict(list)
    for r in rows:
        history[r["crypto_id"]].append(r["price_usd"])
    return dict(history)


def analyze(markets: list[dict], wallet: dict[str, Any],
            spot_prices: dict | None = None) -> list[TradeDecision]:
    """
    Run strategies. One signal per event_ticker max (prevent bracket spam).
    Pairs primary trades with hedge legs for defined-risk spreads.

    Active strategies (ordered by edge quality):
      1. arbitrage (pure arb, guaranteed)
      2. fair_value_directional (proven winner: 50% WR, 2.7x ratio)
      3. spot_momentum (NEW: ride crypto trends into lagging contracts)
      4. vol_skew (realized > implied vol)
      5. time_decay (proven: 5.1x ratio)
      6. mean_reversion (testing: 1.7x ratio)

    Killed: near_expiry_momentum (0.2x ratio, -$691, 48 trades)
    """
    balance = wallet.get("balance", 1000.0)
    spot = spot_prices or {}
    spot_history = _load_spot_history()
    decisions = []
    event_trade_count = defaultdict(int)  # Allow up to 2 trades per event
    MAX_TRADES_PER_EVENT = int(os.environ.get("RIVALCLAW_MAX_PER_EVENT", "2"))
    stats = defaultdict(int)

    # Group markets by event_ticker for cross-strike arb
    event_groups = defaultdict(list)
    for m in markets:
        evt = m.get("event_ticker", "")
        if evt:
            event_groups[evt].append(m)

    # Cross-strike arb (runs on event groups)
    for evt, group in event_groups.items():
        d = _check_cross_strike_arb(group, balance)
        if d:
            decisions.append(d)
            event_trade_count[evt] += 1
            stats["cross_strike_arb"] += 1

    # Bracket neighbor mispricing (runs on event groups)
    if "bracket_neighbor" not in DISABLED_STRATEGIES:
        for evt, group in event_groups.items():
            if event_trade_count[evt] >= MAX_TRADES_PER_EVENT:
                continue
            neighbors = _check_bracket_neighbor(group, balance, spot)
            for d in neighbors:
                decisions.append(d)
                event_trade_count[evt] += 1
                stats["bracket_neighbor"] += 1

    # Pairs trading (runs on event groups)
    if "pairs_trade" not in DISABLED_STRATEGIES:
        for evt, group in event_groups.items():
            if event_trade_count[evt] >= MAX_TRADES_PER_EVENT:
                continue
            pairs = _check_pairs_trade(group, balance, spot)
            for d in pairs:
                decisions.append(d)
                event_trade_count[evt] += 1
                stats["pairs_trade"] += 1

    # Election field arb (runs on all Polymarket markets)
    field_trades = _check_election_field(markets, balance)
    for d in field_trades:
        decisions.append(d)
        stats["election_field_arb"] += 1

    # Per-market strategies
    for market in markets:
        reason = _validate_market(market)
        if reason:
            stats["integrity"] += 1
            elog.decision(
                action="abstain", strategy="none", market_id=market.get("market_id", ""),
                reason="policy_block", confidence=0, threshold=0,
            )
            continue

        # For Polymarket: no event_ticker, use market_id as event key (each market is independent)
        evt = market.get("event_ticker", "") or market.get("market_id", "")
        if evt and event_trade_count[evt] >= MAX_TRADES_PER_EVENT:
            elog.decision(
                action="abstain", strategy="none", market_id=market.get("market_id", ""),
                reason="position_limit_reached",
            )
            continue

        d = None

        # 1. Cross-outcome arb
        d = d or _check_arbitrage(market, balance)
        if d:
            stats["arbitrage"] += 1

        # 2. Fair value (THE moneymaker — proven 50% WR, 2.7x ratio)
        if not d:
            d = _check_fair_value(market, balance, spot)
            if d:
                stats["fair_value"] += 1

        # 3. Spot momentum (NEW — ride crypto trends)
        if not d:
            d = _check_spot_momentum(market, balance, spot, spot_history)
            if d:
                stats["spot_momentum"] += 1

        # 4. Vol skew
        if not d:
            d = _check_vol_skew(market, balance, spot)
            if d:
                stats["vol_skew"] += 1

        # 5. Time decay
        if not d:
            d = _check_time_decay(market, balance, spot)
            if d:
                stats["time_decay"] += 1

        # 6. Mean reversion
        if not d:
            d = _check_mean_reversion(market, balance, spot)
            if d:
                stats["mean_reversion"] += 1

        # 7. Expiry acceleration (last 5 min, high confidence)
        if not d:
            d = _check_expiry_acceleration(market, balance, spot)
            if d:
                stats["expiry_acceleration"] += 1

        # 8. Closing convergence (price approaching 0 or 1)
        if not d:
            d = _check_closing_convergence(market, balance)
            if d:
                stats["closing_convergence"] += 1

        # 9. Correlation echo (BTC signal → ETH)
        if not d:
            active_sigs = {d2.market_id for d2 in decisions}
            d = _check_correlation_echo(market, balance, spot, active_sigs)
            if d:
                stats["correlation_echo"] += 1

        # 10. Polymarket convergence
        if not d:
            d = _check_polymarket_convergence(market, balance)
            if d:
                stats["polymarket_convergence"] += 1

        # 11. Liquidity fade (illiquid brackets)
        if not d:
            d = _check_liquidity_fade(market, balance, spot)
            if d:
                stats["liquidity_fade"] += 1

        # 12. Volume-confirmed (high-volume brackets only)
        if not d:
            d = _check_volume_confirmed(market, balance, spot)
            if d:
                stats["volume_confirmed"] += 1

        # 13. Wipeout reversal (15-min mean reversion)
        if not d:
            d = _check_wipeout_reversal(market, balance, spot)
            if d:
                stats["wipeout_reversal"] += 1

        # 14. Multi-timeframe consensus
        if not d:
            d = _check_multi_timeframe(market, balance, spot, decisions)
            if d:
                stats["multi_timeframe"] += 1

        # 15. Vol regime switching (vol spike → buy OTM)
        if not d:
            d = _check_vol_regime(market, balance, spot)
            if d:
                stats["vol_regime"] += 1

        # 16. Correlation cascade (BTC move → trade ETH)
        if not d:
            d = _check_correlation_cascade(market, balance, spot, spot_history)
            if d:
                stats["correlation_cascade"] += 1

        # 17. NWS forecast delta (weather gap after forecast update)
        if not d:
            d = _check_forecast_delta(market, balance)
            if d:
                stats["forecast_delta"] += 1

        if d:
            # Attach market priority score for velocity-weighted ranking
            if d.metadata is None:
                d.metadata = {}
            d.metadata["priority_score"] = market.get("priority_score", 0)
            d.metadata["speed_category"] = market.get("speed_category", "unknown")
            decisions.append(d)
            if evt:
                event_trade_count[evt] += 1
            # Strategy Lab: emit signal + decision
            elog.signal(
                strategy=d.strategy, market_id=d.market_id, direction=d.direction,
                confidence=d.confidence, edge_estimate=(d.metadata or {}).get("edge", 0),
                features={"venue": (d.metadata or {}).get("venue", "polymarket")},
            )
            elog.decision(
                action="enter", strategy=d.strategy, market_id=d.market_id,
                confidence=d.confidence, size_proposed=d.amount_usd,
            )
        else:
            # No strategy fired — emit classified abstain
            elog.decision(
                action="abstain", strategy="none", market_id=market.get("market_id", ""),
                reason="confidence_below_threshold",
            )

    # Hedge engine: pair primary Kalshi trades with hedge legs
    hedged = []
    for d in decisions:
        hedged.append(d)
        if "hedge" not in DISABLED_STRATEGIES:
            hedge = _find_hedge(d, event_groups, balance)
            if hedge:
                hedged.append(hedge)
                stats["hedge"] += 1

    if stats["integrity"]:
        print(f"[rivalclaw/brain] Integrity rejected: {stats['integrity']}")
    parts = " ".join(f"{k}={v}" for k, v in sorted(stats.items()) if k != "integrity")
    print(f"[rivalclaw/brain] Signals: {parts} (total={len(hedged)})")

    # Sort by confidence × velocity preference — faster markets rank higher
    def _rank(d):
        priority = (d.metadata or {}).get("priority_score", 0)
        velocity_boost = 1.0 + (priority / 15.0) * (VELOCITY_PREFERENCE - 1.0)
        return d.confidence * velocity_boost
    return sorted(hedged, key=_rank, reverse=True)
