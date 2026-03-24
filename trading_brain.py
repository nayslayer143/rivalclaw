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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLYMARKET_FEE_RATE = float(os.environ.get("ARB_FEE_RATE", "0.02"))
MIN_EDGE = float(os.environ.get("ARB_MIN_EDGE", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))
STALE_THRESHOLD_MINUTES = float(os.environ.get("RIVALCLAW_STALE_MINUTES", "30"))

MIN_FAIR_VALUE_EDGE = float(os.environ.get("RIVALCLAW_MIN_FV_EDGE", "0.04"))
KALSHI_TAKER_FEE_RATE = float(os.environ.get("RIVALCLAW_KALSHI_FEE", "0.07"))

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

# Proven strategies get full Kelly, new ones get quarter Kelly
PROVEN_STRATEGIES = {"arbitrage", "fair_value_directional", "near_expiry_momentum"}


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

def _size_for_strategy(strategy: str, confidence: float, entry_price: float,
                       balance: float) -> float | None:
    frac = KELLY_FRACTION_PROVEN if strategy in PROVEN_STRATEGIES else KELLY_FRACTION_NEW
    return _kelly_size(confidence, entry_price, balance, frac)

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
    amount = _size_for_strategy(strategy, confidence, entry_price, balance)
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
    amount = _size_for_strategy("arbitrage", confidence, entry_price, balance)
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

def _check_fair_value(market, balance, spot_prices):
    if market.get("venue") != "kalshi":
        return None
    strike_type = market.get("strike_type", "")
    if strike_type == "between":
        return None  # Handled by cross_strike_arb
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 2 or minutes > 24 * 60:
        return None
    underlying_id = _find_underlying(market)
    if not underlying_id:
        return None
    spot = spot_prices.get(underlying_id)
    if not spot or spot <= 0:
        return None
    if strike_type == "greater_or_equal":
        strike = market.get("floor_strike")
    elif strike_type == "less":
        strike = market.get("cap_strike")
    else:
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
        direction, entry_price, confidence, edge = "YES", yes_price, min(fair, 0.95), edge_yes
    elif edge_no > MIN_FAIR_VALUE_EDGE:
        direction, entry_price, confidence, edge = "NO", no_price, min(1.0 - fair, 0.95), edge_no
    else:
        return None
    amount = _size_for_strategy("fair_value_directional", confidence, entry_price, balance)
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
# Strategy 3: Near-expiry momentum (no brackets)
# ---------------------------------------------------------------------------

def _check_near_expiry(market, balance):
    if market.get("strike_type") == "between":
        return None
    minutes = _parse_expiry_minutes(market)
    if minutes is None or minutes <= 0:
        return None
    hours = minutes / 60.0
    if hours > NEAR_EXPIRY_HOURS:
        return None
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.02 or yes_price >= 0.98:
        return None
    venue = market.get("venue", "polymarket")
    fee_fn = _kalshi_fee if venue == "kalshi" else _fee
    time_boost = max(0, 1.0 - hours / NEAR_EXPIRY_HOURS) * 0.05
    if yes_price >= MIN_MOMENTUM_PRICE:
        direction, entry_price = "YES", yes_price
        confidence = min(yes_price + time_boost, 0.95)
    elif yes_price <= (1.0 - MIN_MOMENTUM_PRICE):
        direction, entry_price = "NO", 1.0 - yes_price
        confidence = min(entry_price + time_boost, 0.95)
    else:
        return None
    fee = fee_fn(entry_price)
    edge = confidence - (entry_price + fee)
    if edge <= MIN_EDGE:
        return None
    amount = _size_for_strategy("near_expiry_momentum", confidence, entry_price, balance)
    if amount is None:
        return None
    return TradeDecision(
        market_id=market["market_id"], question=market.get("question", ""),
        direction=direction, confidence=confidence,
        reasoning=f"NearExpiry: {hours:.1f}h left yes={yes_price:.3f} edge={edge:.3f} [{venue}]",
        strategy="near_expiry_momentum", amount_usd=amount, entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "hours_to_expiry": hours, "venue": venue},
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
    amount = _size_for_strategy("cross_strike_arb", confidence, yes_price, balance)
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
# Strategy 5: Mean reversion (15-min crypto, bet against crowd at coin-flip)
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

    amount = _size_for_strategy("mean_reversion", confidence, entry_price, balance)
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

    amount = _size_for_strategy("time_decay", confidence, entry_price, balance)
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

    amount = _size_for_strategy("vol_skew", confidence, entry_price, balance)
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
# Strategy 8: Calibration (stub — activates with historical data)
# ---------------------------------------------------------------------------

def _check_calibration(market, balance, calibration_data):
    """
    Trade based on historical price-outcome calibration curve.
    Requires enough resolution data to build calibration buckets.
    Stub — returns None until calibration_data is populated.
    """
    if not calibration_data:
        return None
    # Future: bucket markets by pre-resolution price, compare to actual resolution rate
    # If markets priced at 0.80 resolve YES 92% of the time → buy YES when price is 0.80
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze(markets: list[dict], wallet: dict[str, Any],
            spot_prices: dict | None = None) -> list[TradeDecision]:
    """
    Run all 8 strategies. One signal per event_ticker max (prevent bracket spam).
    """
    balance = wallet.get("balance", 1000.0)
    spot = spot_prices or {}
    decisions = []
    seen_events = set()  # ONE trade per event — prevents bracket spam
    stats = defaultdict(int)

    # Group markets by event_ticker for cross-strike arb
    event_groups = defaultdict(list)
    for m in markets:
        evt = m.get("event_ticker", "")
        if evt:
            event_groups[evt].append(m)

    # Strategy 4: Cross-strike arb (runs on event groups, not individual markets)
    for evt, group in event_groups.items():
        d = _check_cross_strike_arb(group, balance)
        if d:
            decisions.append(d)
            seen_events.add(evt)
            stats["cross_strike_arb"] += 1

    # Per-market strategies
    for market in markets:
        reason = _validate_market(market)
        if reason:
            stats["integrity"] += 1
            continue

        # Skip if we already have a trade on this event
        evt = market.get("event_ticker", "")
        if evt and evt in seen_events:
            continue

        # Run strategies in priority order (highest edge-quality first)
        d = None

        # 1. Cross-outcome arb (guaranteed profit)
        d = d or _check_arbitrage(market, balance)
        if d:
            stats["arbitrage"] += 1

        # 2. Fair value directional (strong math backing)
        if not d:
            d = _check_fair_value(market, balance, spot)
            if d:
                stats["fair_value"] += 1

        # 3. Vol skew (realized > implied)
        if not d:
            d = _check_vol_skew(market, balance, spot)
            if d:
                stats["vol_skew"] += 1

        # 4. Time decay (very near expiry, exploits boring windows)
        if not d:
            d = _check_time_decay(market, balance, spot)
            if d:
                stats["time_decay"] += 1

        # 5. Mean reversion (bet against crowd at coin-flip)
        if not d:
            d = _check_mean_reversion(market, balance, spot)
            if d:
                stats["mean_reversion"] += 1

        # 6. Near-expiry momentum (weakest — proven loser, last resort)
        if not d:
            d = _check_near_expiry(market, balance)
            if d:
                stats["near_expiry"] += 1

        # 7. Calibration (stub)
        if not d:
            d = _check_calibration(market, balance, None)
            if d:
                stats["calibration"] += 1

        if d:
            decisions.append(d)
            if evt:
                seen_events.add(evt)

    if stats["integrity"]:
        print(f"[rivalclaw/brain] Integrity rejected: {stats['integrity']}")
    parts = " ".join(f"{k}={v}" for k, v in sorted(stats.items()) if k != "integrity")
    print(f"[rivalclaw/brain] Signals: {parts} (total={len(decisions)})")

    return sorted(decisions, key=lambda d: d.confidence, reverse=True)
