#!/usr/bin/env python3
"""
RivalClaw trading brain — multi-strategy with integrity guards.

Strategies:
  1. arbitrage        — cross-outcome arb (original, both venues)
  2. fair_value_directional — spot-vs-contract mispricing on Kalshi fast-resolution
  3. near_expiry_momentum   — near-expiry directional on both venues

Arb math is identical to ArbClaw (same fee computation, same Kelly, same thresholds).
Wrapped in Mirofish's TradeDecision/analyze() pattern.
"""
from __future__ import annotations
import datetime
import math
import os
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Arb constants — MUST match ArbClaw for experiment parity
POLYMARKET_FEE_RATE = float(os.environ.get("ARB_FEE_RATE", "0.02"))
MIN_EDGE = float(os.environ.get("ARB_MIN_EDGE", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))
STALE_THRESHOLD_MINUTES = float(os.environ.get("RIVALCLAW_STALE_MINUTES", "30"))

# Fair value strategy
MIN_FAIR_VALUE_EDGE = float(os.environ.get("RIVALCLAW_MIN_FV_EDGE", "0.04"))
KALSHI_TAKER_FEE_RATE = float(os.environ.get("RIVALCLAW_KALSHI_FEE", "0.07"))

# Near-expiry momentum strategy
NEAR_EXPIRY_HOURS = float(os.environ.get("RIVALCLAW_NEAR_EXPIRY_HOURS", "48"))
MIN_MOMENTUM_PRICE = float(os.environ.get("RIVALCLAW_MIN_MOMENTUM_PRICE", "0.78"))

# Annualized volatility estimates for crypto (conservative)
CRYPTO_VOL = {
    "dogecoin": float(os.environ.get("RIVALCLAW_VOL_DOGECOIN", "0.90")),
    "cardano": float(os.environ.get("RIVALCLAW_VOL_CARDANO", "0.80")),
    "binancecoin": float(os.environ.get("RIVALCLAW_VOL_BINANCECOIN", "0.65")),
    "bitcoin-cash": float(os.environ.get("RIVALCLAW_VOL_BITCOIN_CASH", "0.75")),
    "bitcoin": float(os.environ.get("RIVALCLAW_VOL_BITCOIN", "0.60")),
    "ethereum": float(os.environ.get("RIVALCLAW_VOL_ETHEREUM", "0.65")),
}

# Series-to-underlying mapping (same as kalshi_feed)
SERIES_TO_UNDERLYING = {
    "KXDOGE15M": "dogecoin",
    "KXADA15M": "cardano",
    "KXBNB15M": "binancecoin",
    "KXBCH15M": "bitcoin-cash",
    "KXBTC": "bitcoin",
    "KXBTCMAXD": "bitcoin",
    "KXETH": "ethereum",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TradeDecision:
    market_id: str
    question: str
    direction: str          # YES | NO
    confidence: float
    reasoning: str
    strategy: str
    amount_usd: float
    entry_price: float
    shares: float
    decision_generated_at_ms: float = 0.0
    metadata: dict | None = None


# ---------------------------------------------------------------------------
# Fee helpers
# ---------------------------------------------------------------------------

def _fee(price: float) -> float:
    """Polymarket fee: 2% of min(price, 1-price). Identical to ArbClaw."""
    return POLYMARKET_FEE_RATE * min(price, 1.0 - price)


def _kalshi_fee(price: float) -> float:
    """Kalshi taker fee: ~7% of min(price, 1-price)."""
    return KALSHI_TAKER_FEE_RATE * min(price, 1.0 - price)


def _venue_fee(price: float, venue: str) -> float:
    return _kalshi_fee(price) if venue == "kalshi" else _fee(price)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def _kelly_size(confidence: float, entry_price: float, balance: float) -> float | None:
    """Kelly criterion for prediction markets. Identical to ArbClaw."""
    if entry_price <= 0 or entry_price >= 1:
        return None
    b = (1.0 / entry_price) - 1.0
    kelly = (confidence * b - (1.0 - confidence)) / b
    if kelly <= 0:
        return None
    return min(kelly * balance, MAX_POSITION_PCT * balance)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_market(market: dict) -> str | None:
    """Integrity guards. Returns rejection reason or None if valid."""
    if not market.get("market_id") or not market.get("question"):
        return "malformed: missing market_id or question"

    yes_p = market.get("yes_price")
    no_p = market.get("no_price")

    if yes_p is None or no_p is None:
        return "missing prices"

    if not (0 < yes_p < 1) or not (0 < no_p < 1):
        return f"impossible prices: yes={yes_p} no={no_p}"

    total = yes_p + no_p
    if total > 2.0 or total < 0.01:
        return f"sum sanity: yes+no={total:.3f}"

    return None


# ---------------------------------------------------------------------------
# Strategy 1: Cross-outcome arbitrage (original)
# ---------------------------------------------------------------------------

def _check_arbitrage(market: dict, balance: float) -> TradeDecision | None:
    """
    Cross-outcome arb detection. Same math as ArbClaw:
    edge = 1.0 - (yes + no + fee_yes + fee_no)
    """
    yes_p = market.get("yes_price", 0) or 0
    no_p = market.get("no_price", 0) or 0
    venue = market.get("venue", "polymarket")
    fee_fn = _kalshi_fee if venue == "kalshi" else _fee

    total_cost = yes_p + no_p + fee_fn(yes_p) + fee_fn(no_p)
    edge = 1.0 - total_cost

    if edge <= MIN_EDGE:
        return None

    if no_p < (1.0 - yes_p):
        direction, entry_price = "NO", no_p
    else:
        direction, entry_price = "YES", yes_p

    confidence = min(entry_price + edge, 0.99)
    amount = _kelly_size(confidence, entry_price, balance)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"],
        question=market.get("question", ""),
        direction=direction,
        confidence=confidence,
        reasoning=(f"Arb: YES={yes_p:.3f} + NO={no_p:.3f} = {yes_p+no_p:.3f}, "
                   f"edge={edge:.4f} after fees [{venue}]"),
        strategy="arbitrage",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "yes_price": yes_p, "no_price": no_p,
                  "fee_yes": fee_fn(yes_p), "fee_no": fee_fn(no_p), "venue": venue},
    )


# ---------------------------------------------------------------------------
# Strategy 2: Fair value directional (Kalshi fast-resolution)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Normal CDF via math.erf (Abramowitz & Stegun)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _compute_fair_value(spot: float, strike: float, minutes_to_expiry: float,
                        vol: float, strike_type: str = "greater_or_equal") -> float | None:
    """
    Fair value of a binary option using simplified Black-Scholes.
    For 'greater_or_equal': P(spot >= strike at expiry).
    For 'between': P(floor <= spot <= cap) — needs both strikes.
    """
    if spot <= 0 or strike <= 0 or minutes_to_expiry <= 0 or vol <= 0:
        return None

    years = minutes_to_expiry / (365.25 * 24 * 60)
    sigma_t = vol * math.sqrt(years)

    if sigma_t < 0.0001:
        # Basically no time left — binary
        if strike_type == "greater_or_equal":
            return 1.0 if spot >= strike else 0.0
        else:
            return 1.0 if spot < strike else 0.0

    d2 = math.log(spot / strike) / sigma_t

    if strike_type == "greater_or_equal":
        return max(0.01, min(0.99, _norm_cdf(d2)))
    else:
        # For 'less' type: P(spot < strike) = 1 - P(spot >= strike)
        return max(0.01, min(0.99, 1.0 - _norm_cdf(d2)))


def _parse_expiry_minutes(market: dict) -> float | None:
    """Parse close_time/end_date and return minutes to expiry."""
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
    """Match a Kalshi market to its crypto underlying."""
    event_ticker = market.get("event_ticker", "")
    market_id = market.get("market_id", "")
    for series, crypto_id in SERIES_TO_UNDERLYING.items():
        if series in event_ticker or series in market_id:
            return crypto_id
    return None


def _check_fair_value_directional(market: dict, balance: float,
                                   spot_prices: dict) -> TradeDecision | None:
    """
    Fair value directional for Kalshi fast-resolution contracts.
    Compare contract market price to fair value computed from crypto spot.
    """
    if market.get("venue") != "kalshi":
        return None

    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 24 * 60:
        return None

    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None

    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None

    # Determine strike: floor_strike for greater_or_equal, cap_strike for less
    strike_type = market.get("strike_type", "")
    if strike_type == "greater_or_equal":
        strike = market.get("floor_strike")
    elif strike_type == "less":
        strike = market.get("cap_strike")
    elif strike_type == "between":
        # For bracket contracts, skip for now (more complex)
        return None
    else:
        # Try floor_strike as default
        strike = market.get("floor_strike") or market.get("cap_strike")

    if not strike or strike <= 0:
        return None

    vol = CRYPTO_VOL.get(underlying_id, 0.70)
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
        direction = "YES"
        entry_price = yes_price
        confidence = min(fair, 0.95)
        edge = edge_yes
    elif edge_no > MIN_FAIR_VALUE_EDGE:
        direction = "NO"
        entry_price = no_price
        confidence = min(1.0 - fair, 0.95)
        edge = edge_no
    else:
        return None

    amount = _kelly_size(confidence, entry_price, balance)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"],
        question=market.get("question", ""),
        direction=direction,
        confidence=confidence,
        reasoning=(f"FairVal: spot=${spot:,.2f} strike=${strike:,.2f} "
                   f"exp={minutes:.0f}m fair={fair:.3f} mkt={yes_price:.3f} "
                   f"edge={edge:.3f} [{underlying_id}]"),
        strategy="fair_value_directional",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "fair_value": fair, "spot": spot,
                  "strike": strike, "minutes_to_expiry": minutes,
                  "vol": vol, "venue": "kalshi", "strike_type": strike_type},
    )


# ---------------------------------------------------------------------------
# Strategy 3: Near-expiry momentum (both venues)
# ---------------------------------------------------------------------------

def _check_near_expiry_momentum(market: dict, balance: float) -> TradeDecision | None:
    """
    Bet on continuation when price is strongly directional
    and market is close to resolution.
    EXCLUDES Kalshi bracket contracts — their price volatility kills stop-losses.
    """
    # Skip bracket contracts — they have extreme price swings that trigger stops
    if market.get("strike_type") == "between":
        return None

    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 0:
        return None

    hours_to_expiry = minutes / 60.0
    if hours_to_expiry > NEAR_EXPIRY_HOURS:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None

    venue = market.get("venue", "polymarket")
    fee_fn = _kalshi_fee if venue == "kalshi" else _fee

    # Time boost: closer to expiry = more confident in current price
    time_boost = max(0, 1.0 - hours_to_expiry / NEAR_EXPIRY_HOURS) * 0.05

    if yes_price >= MIN_MOMENTUM_PRICE:
        direction = "YES"
        entry_price = yes_price
        confidence = min(yes_price + time_boost, 0.95)
        fee = fee_fn(entry_price)
        edge = confidence - (entry_price + fee)
    elif yes_price <= (1.0 - MIN_MOMENTUM_PRICE):
        direction = "NO"
        entry_price = 1.0 - yes_price
        confidence = min(entry_price + time_boost, 0.95)
        fee = fee_fn(entry_price)
        edge = confidence - (entry_price + fee)
    else:
        return None

    if edge <= MIN_EDGE:
        return None

    amount = _kelly_size(confidence, entry_price, balance)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"],
        question=market.get("question", ""),
        direction=direction,
        confidence=confidence,
        reasoning=(f"NearExpiry: {hours_to_expiry:.1f}h left, "
                   f"yes={yes_price:.3f}, edge={edge:.3f} [{venue}]"),
        strategy="near_expiry_momentum",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "hours_to_expiry": hours_to_expiry,
                  "yes_price": yes_price, "venue": venue},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze(markets: list[dict], wallet: dict[str, Any],
            spot_prices: dict | None = None) -> list[TradeDecision]:
    """
    Main entry point. Runs all strategies on all markets.
    spot_prices: {coingecko_id: price_usd} for fair_value strategy.
    """
    balance = wallet.get("balance", 1000.0)
    spot = spot_prices or {}
    decisions = []
    stats = {"integrity": 0, "arb": 0, "fair_value": 0, "near_expiry": 0}

    for market in markets:
        reason = _validate_market(market)
        if reason:
            stats["integrity"] += 1
            continue

        # Strategy 1: Cross-outcome arb
        d = _check_arbitrage(market, balance)
        if d:
            decisions.append(d)
            stats["arb"] += 1
            continue

        # Strategy 2: Fair value directional (Kalshi)
        d = _check_fair_value_directional(market, balance, spot)
        if d:
            decisions.append(d)
            stats["fair_value"] += 1
            continue

        # Strategy 3: Near-expiry momentum (both venues)
        d = _check_near_expiry_momentum(market, balance)
        if d:
            decisions.append(d)
            stats["near_expiry"] += 1

    if stats["integrity"]:
        print(f"[rivalclaw/brain] Integrity rejected: {stats['integrity']}")
    print(f"[rivalclaw/brain] Signals: arb={stats['arb']} fv={stats['fair_value']} "
          f"expiry={stats['near_expiry']} (total={len(decisions)})")

    return sorted(decisions, key=lambda d: d.confidence, reverse=True)
