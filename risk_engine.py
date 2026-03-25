#!/usr/bin/env python3
"""
RivalClaw risk engine — dynamic capital allocation, regime detection, and portfolio risk.

This is the layer between the brain (signal generation) and the wallet (execution).
It answers: "given these signals and current market conditions, HOW MUCH should we bet?"

Three subsystems:
  1. Regime detector — classifies market as trending/volatile/calm
  2. Strategy tournament — allocates capital based on rolling performance
  3. Portfolio risk limiter — caps correlated exposure, total risk
"""
from __future__ import annotations
import datetime
import math
import os
import sqlite3
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Risk limits
MAX_CRYPTO_EXPOSURE_PCT = float(os.environ.get("RIVALCLAW_MAX_CRYPTO_PCT", "0.40"))  # 40% of balance
MAX_SINGLE_ASSET_PCT = float(os.environ.get("RIVALCLAW_MAX_ASSET_PCT", "0.25"))  # 25% per asset
TOURNAMENT_LOOKBACK = int(os.environ.get("RIVALCLAW_TOURNAMENT_LOOKBACK", "200"))  # trades (wide window to survive batch events)
MIN_TOURNAMENT_TRADES = 5  # Minimum trades before scoring a strategy

# Regime thresholds
VOL_CALM_THRESHOLD = 0.002  # 15-min return std < 0.2% = calm
VOL_HIGH_THRESHOLD = 0.008  # 15-min return std > 0.8% = volatile


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1. Regime Detector
# ---------------------------------------------------------------------------

def detect_regime() -> dict:
    """
    Classify current market regime from recent spot data.
    Returns: {regime: 'calm'|'trending'|'volatile', vol: float, trend: float}
    """
    conn = _get_conn()
    try:
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)).isoformat()
        rows = conn.execute(
            "SELECT crypto_id, price_usd FROM spot_prices WHERE crypto_id='bitcoin' "
            "AND fetched_at > ? ORDER BY fetched_at ASC", (cutoff,)
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 5:
        return {"regime": "unknown", "vol": 0, "trend": 0}

    prices = [r["price_usd"] for r in rows if r["price_usd"] and r["price_usd"] > 0]
    if len(prices) < 5:
        return {"regime": "unknown", "vol": 0, "trend": 0}

    # Compute returns
    returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))
               if prices[i] > 0 and prices[i-1] > 0]
    if len(returns) < 3:
        return {"regime": "unknown", "vol": 0, "trend": 0}

    vol = math.sqrt(sum(r**2 for r in returns) / len(returns))
    trend = sum(returns) / len(returns)  # Positive = up, negative = down

    if vol > VOL_HIGH_THRESHOLD:
        regime = "volatile"
    elif abs(trend) > vol * 0.5 and vol > VOL_CALM_THRESHOLD:
        regime = "trending"
    else:
        regime = "calm"

    return {"regime": regime, "vol": vol, "trend": trend}


# ---------------------------------------------------------------------------
# 2. Strategy Tournament
# ---------------------------------------------------------------------------

def get_strategy_scores() -> dict[str, float]:
    """
    Score each strategy based on recent performance.
    Returns: {strategy: score} where score is a capital allocation multiplier.
    Score = 1.0 means normal allocation, >1.0 means overweight, <1.0 means underweight.
    Dead strategies get 0.0.
    """
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT strategy, pnl, amount_usd
            FROM paper_trades WHERE status != 'open'
            ORDER BY closed_at DESC LIMIT ?
        """, (TOURNAMENT_LOOKBACK * 5,)).fetchall()
    finally:
        conn.close()

    # Group by strategy, take last N trades per strategy
    by_strategy = defaultdict(list)
    for r in rows:
        if len(by_strategy[r["strategy"]]) < TOURNAMENT_LOOKBACK:
            by_strategy[r["strategy"]].append({
                "pnl": r["pnl"] or 0,
                "amount": r["amount_usd"] or 1,
            })

    scores = {}
    for strat, trades in by_strategy.items():
        if len(trades) < MIN_TOURNAMENT_TRADES:
            scores[strat] = 0.5  # Untested — half allocation
            continue

        # ROI-based scoring
        total_pnl = sum(t["pnl"] for t in trades)
        total_capital = sum(t["amount"] for t in trades)
        roi = total_pnl / total_capital if total_capital > 0 else 0

        # Win rate
        wins = sum(1 for t in trades if t["pnl"] > 0)
        wr = wins / len(trades)

        # ROI-DRIVEN scoring (not WR-gated)
        # Fair value buys cheap brackets: 25% WR but 17% ROI = great.
        # WR is irrelevant if ROI is positive — magnitude matters, not frequency.
        if roi < -0.10:
            scores[strat] = 0.0  # Kill it — losing >10% of capital
        elif roi < 0:
            scores[strat] = 0.25  # Underweight — losing money
        elif roi > 0.10:
            scores[strat] = 1.5  # Overweight — strong positive ROI
        elif wr > 0.40 and roi > 0:
            scores[strat] = 1.0  # Normal
        else:
            scores[strat] = 0.5  # Below average

    return scores


# ---------------------------------------------------------------------------
# 3. Portfolio Risk Limiter
# ---------------------------------------------------------------------------

def get_portfolio_exposure() -> dict:
    """
    Compute current portfolio exposure by asset.
    Returns: {asset: exposure_usd, 'total': total_exposure}
    """
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT market_id, amount_usd FROM paper_trades WHERE status='open'
        """).fetchall()
    finally:
        conn.close()

    exposure = defaultdict(float)
    for r in rows:
        mid = r["market_id"]
        if "BTC" in mid or "KXBTC" in mid:
            exposure["BTC"] += r["amount_usd"]
        elif "ETH" in mid or "KXETH" in mid:
            exposure["ETH"] += r["amount_usd"]
        elif "DOGE" in mid:
            exposure["DOGE"] += r["amount_usd"]
        elif "BNB" in mid:
            exposure["BNB"] += r["amount_usd"]
        else:
            exposure["OTHER"] += r["amount_usd"]

    exposure["total_crypto"] = sum(v for k, v in exposure.items() if k != "OTHER")
    exposure["total"] = sum(exposure.values())
    return dict(exposure)


def check_risk_limits(decision, balance: float) -> tuple[bool, str]:
    """
    Check if a new trade would violate portfolio risk limits.
    Returns: (allowed, reason)
    """
    exposure = get_portfolio_exposure()
    amount = decision.amount_usd

    # Total crypto exposure limit
    max_crypto = balance * MAX_CRYPTO_EXPOSURE_PCT
    new_crypto = exposure.get("total_crypto", 0) + amount
    if new_crypto > max_crypto:
        return False, f"crypto exposure ${new_crypto:.0f} > {MAX_CRYPTO_EXPOSURE_PCT:.0%} of ${balance:.0f}"

    # Single asset limit
    mid = decision.market_id
    if "BTC" in mid or "KXBTC" in mid:
        asset = "BTC"
    elif "ETH" in mid or "KXETH" in mid:
        asset = "ETH"
    elif "DOGE" in mid:
        asset = "DOGE"
    elif "BNB" in mid:
        asset = "BNB"
    else:
        asset = "OTHER"

    max_asset = balance * MAX_SINGLE_ASSET_PCT
    new_asset = exposure.get(asset, 0) + amount
    if new_asset > max_asset:
        return False, f"{asset} exposure ${new_asset:.0f} > {MAX_SINGLE_ASSET_PCT:.0%} of ${balance:.0f}"

    return True, "ok"


# ---------------------------------------------------------------------------
# 4. Dynamic sizing — integrates regime + tournament + risk
# ---------------------------------------------------------------------------

def adjust_decision(decision, balance: float, regime: dict,
                    strategy_scores: dict) -> object | None:
    """
    Adjust a trade decision based on regime, tournament score, and risk limits.
    Returns modified decision or None if trade should be blocked.
    """
    # Check risk limits first
    allowed, reason = check_risk_limits(decision, balance)
    if not allowed:
        return None

    # Get strategy score
    score = strategy_scores.get(decision.strategy, 0.5)
    if score <= 0.0:
        return None  # Dead strategy

    # Regime adjustment
    regime_mult = 1.0
    r = regime.get("regime", "unknown")
    if r == "volatile":
        # High vol: reduce size (more uncertainty), but boost fair_value (wider mispricings)
        if decision.strategy in ("fair_value_directional", "bracket_cone"):
            regime_mult = 1.2  # Vol creates mispricings — lean in
        else:
            regime_mult = 0.6  # Other strategies suffer in vol
    elif r == "trending":
        # Trending: boost momentum, reduce mean reversion
        if decision.strategy == "spot_momentum":
            regime_mult = 1.5
        elif decision.strategy == "mean_reversion":
            regime_mult = 0.5
    elif r == "calm":
        # Calm: boost time decay and mean reversion
        if decision.strategy in ("time_decay", "mean_reversion"):
            regime_mult = 1.3
        elif decision.strategy == "spot_momentum":
            regime_mult = 0.5

    # Speed-based sizing: fast markets get full size, slow markets get less
    # Polymarket gets a floor — different venue, different dynamics, don't starve it
    venue = (decision.metadata or {}).get("venue", "kalshi")
    priority = (decision.metadata or {}).get("priority_score", 0)
    if venue == "polymarket":
        speed_mult = 0.5  # Polymarket: half size (longer-dated but we need data)
    elif priority >= 14:
        speed_mult = 1.0
    elif priority >= 10:
        speed_mult = 0.5
    else:
        speed_mult = 0.25

    final_mult = score * regime_mult * speed_mult
    max_position = balance * float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.04")) * 0.95
    decision.amount_usd = min(decision.amount_usd * final_mult, max_position)
    decision.shares = decision.amount_usd / decision.entry_price if decision.entry_price > 0 else 0

    if decision.amount_usd < 1:
        return None

    return decision
