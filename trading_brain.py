#!/usr/bin/env python3
"""
RivalClaw trading brain — arb-only with integrity guards.
Arb math is identical to ArbClaw (same fee computation, same Kelly, same thresholds).
Wrapped in Mirofish's TradeDecision/analyze() pattern.
"""
from __future__ import annotations
import os
import time
from dataclasses import dataclass, field
from typing import Any

# Shared arb constants — MUST match ArbClaw for experiment parity
POLYMARKET_FEE_RATE = float(os.environ.get("ARB_FEE_RATE", "0.02"))
MIN_EDGE = float(os.environ.get("ARB_MIN_EDGE", "0.005"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))

# Integrity guard thresholds (adjustment #3)
STALE_THRESHOLD_MINUTES = float(os.environ.get("RIVALCLAW_STALE_MINUTES", "30"))


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


def _fee(price: float) -> float:
    """Polymarket fee: 2% of min(price, 1-price). Identical to ArbClaw."""
    return POLYMARKET_FEE_RATE * min(price, 1.0 - price)


def _kelly_size(confidence: float, entry_price: float, balance: float) -> float | None:
    """Kelly criterion for prediction markets. Identical to ArbClaw."""
    if entry_price <= 0 or entry_price >= 1:
        return None
    b = (1.0 / entry_price) - 1.0
    kelly = (confidence * b - (1.0 - confidence)) / b
    if kelly <= 0:
        return None
    return min(kelly * balance, MAX_POSITION_PCT * balance)


def _validate_market(market: dict) -> str | None:
    """Integrity guards (adjustment #3). Returns rejection reason or None if valid."""
    # Malformed payload
    if not market.get("market_id") or not market.get("question"):
        return "malformed: missing market_id or question"

    yes_p = market.get("yes_price")
    no_p = market.get("no_price")

    # Missing prices
    if yes_p is None or no_p is None:
        return "missing prices"

    # Impossible prices
    if not (0 < yes_p < 1) or not (0 < no_p < 1):
        return f"impossible prices: yes={yes_p} no={no_p}"

    # Sum sanity — if way out of bounds, data is broken
    total = yes_p + no_p
    if total > 2.0 or total < 0.01:
        return f"sum sanity: yes+no={total:.3f}"

    return None


def _check_arbitrage(market: dict, balance: float) -> TradeDecision | None:
    """
    Cross-outcome arb detection. Same math as ArbClaw:
    edge = 1.0 - (yes + no + fee_yes + fee_no)
    """
    yes_p = market.get("yes_price", 0) or 0
    no_p = market.get("no_price", 0) or 0

    # Cost to buy both sides including fees
    total_cost = yes_p + no_p + _fee(yes_p) + _fee(no_p)
    edge = 1.0 - total_cost

    if edge <= MIN_EDGE:
        return None

    # Buy the underpriced side
    if no_p < (1.0 - yes_p):
        direction, entry_price = "NO", no_p
    else:
        direction, entry_price = "YES", yes_p

    # Kelly sizing — use edge as confidence proxy (same as ArbClaw)
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
                   f"edge={edge:.4f} after fees"),
        strategy="arbitrage",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "yes_price": yes_p, "no_price": no_p,
                  "fee_yes": _fee(yes_p), "fee_no": _fee(no_p)},
    )


def analyze(markets: list[dict], wallet: dict[str, Any]) -> list[TradeDecision]:
    """
    Main entry point. Matches Mirofish analyze() signature.
    Returns list of TradeDecisions sorted by confidence desc.
    """
    balance = wallet.get("balance", 1000.0)
    decisions = []
    rejected = {"integrity": 0, "no_edge": 0, "kelly_negative": 0}

    for market in markets:
        # Integrity guards
        reason = _validate_market(market)
        if reason:
            rejected["integrity"] += 1
            continue

        decision = _check_arbitrage(market, balance)
        if decision:
            decisions.append(decision)

    if rejected["integrity"] > 0:
        print(f"[rivalclaw/brain] Integrity rejected: {rejected['integrity']} markets")

    return sorted(decisions, key=lambda d: d.confidence, reverse=True)
