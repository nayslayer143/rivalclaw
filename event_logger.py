#!/usr/bin/env python3
"""
RivalClaw structured event logger — machine-readable JSONL for Strategy Lab.

All modules call this instead of (or alongside) print(). Output is append-only
JSONL at ~/rivalclaw/logs/events.jsonl, rotated daily.

Record types: market_snapshot, signal, decision, trade, fill, position,
              outcome, error, regime, abstain
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

LOGS_DIR = Path(os.environ.get("RIVALCLAW_LOGS_DIR", Path(__file__).parent / "logs"))
_run_id: str | None = None

# ---------------------------------------------------------------------------
# Canonical Scoring Doctrine — ONE portfolio-level objective for ALL layers.
# The tuner, risk engine, lab, and governor all score against this.
# ---------------------------------------------------------------------------
CANONICAL_OBJECTIVE = "net_expectancy_after_costs"

# Secondary constraints (not objectives — these are guardrails, not targets):
CONSTRAINTS = {
    "max_drawdown": 0.25,       # < 25%
    "min_win_rate": 0.45,       # > 45%
    "min_sharpe": 0.8,          # > 0.8
    "complexity_penalty": True,  # simpler variant wins ties
}

# Classified abstention reasons — every no-trade must cite one of these.
ABSTAIN_REASONS = [
    "confidence_below_threshold",
    "spread_too_wide",
    "position_limit_reached",
    "exposure_cap",
    "cooldown_active",
    "liquidity_too_low",
    "conflicting_signal",
    "regime_block",
    "time_to_resolution_too_long",
    "policy_block",
]


def _get_log_path() -> Path:
    """Daily log rotation: events-YYYY-MM-DD.jsonl symlinked from events.jsonl."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / "events.jsonl"


def start_run() -> str:
    """Begin a new simulator run. Returns the run_id."""
    global _run_id
    _run_id = str(uuid.uuid4())[:12]
    emit("run_start", {"run_id": _run_id})
    return _run_id


def end_run():
    """Mark the end of a simulator run."""
    emit("run_end", {"run_id": _run_id})


def get_run_id() -> str | None:
    return _run_id


def emit(record_type: str, data: dict | None = None, strategy_version: str = ""):
    """Append one JSONL record to the event log."""
    record = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "type": record_type,
        "strategy_version": strategy_version,
        "run_id": _run_id or "",
    }
    if data:
        record.update(data)
    try:
        path = _get_log_path()
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass  # Never crash the trading loop for logging


def market_snapshot(market: dict):
    """Emit a market_snapshot record."""
    emit("market_snapshot", {
        "market_id": market.get("market_id", ""),
        "platform": market.get("venue", "polymarket"),
        "title": (market.get("question") or "")[:120],
        "yes_price": market.get("yes_price"),
        "no_price": market.get("no_price"),
        "volume": market.get("volume"),
        "spread": abs((market.get("yes_price") or 0) - (1.0 - (market.get("no_price") or 0))),
        "time_to_resolution_hours": _ttl_hours(market),
    })


def signal(strategy: str, market_id: str, direction: str, confidence: float,
           edge_estimate: float, features: dict | None = None,
           strategy_version: str = ""):
    """Emit a signal record when a strategy produces a signal."""
    emit("signal", {
        "strategy": strategy,
        "market_id": market_id,
        "direction": direction,
        "confidence": confidence,
        "edge_estimate": edge_estimate,
        "features": features or {},
    }, strategy_version=strategy_version)


def decision(action: str, strategy: str, market_id: str, reason: str = "",
             confidence: float = 0.0, threshold: float = 0.0,
             size_proposed: float = 0.0, shadow: bool = False,
             strategy_version: str = ""):
    """Emit a decision record for every entry/exit/abstain/skip."""
    emit("decision", {
        "action": action,
        "strategy": strategy,
        "market_id": market_id,
        "reason": reason,
        "confidence": confidence,
        "threshold": threshold,
        "size_proposed": size_proposed,
        "shadow": shadow,
    }, strategy_version=strategy_version)


def trade(trade_id: str, market_id: str, strategy: str, direction: str,
          size: float, price: float, fees: float = 0.0, latency_ms: float = 0.0,
          slippage_estimate: float = 0.0, shadow: bool = False,
          strategy_version: str = ""):
    """Emit a trade record when paper wallet executes."""
    emit("trade", {
        "trade_id": str(trade_id),
        "market_id": market_id,
        "strategy": strategy,
        "direction": direction,
        "size": size,
        "price": price,
        "fees": fees,
        "latency_ms": latency_ms,
        "slippage_estimate": slippage_estimate,
        "shadow": shadow,
    }, strategy_version=strategy_version)


def outcome(trade_id: str, pnl_gross: float, pnl_net: float, fees_paid: float,
            hold_duration_hours: float, resolved_price: float, entry_price: float,
            was_correct: bool, strategy_version: str = ""):
    """Emit an outcome record when a trade resolves."""
    emit("outcome", {
        "trade_id": str(trade_id),
        "pnl_gross": pnl_gross,
        "pnl_net": pnl_net,
        "fees_paid": fees_paid,
        "hold_duration_hours": hold_duration_hours,
        "resolved_price": resolved_price,
        "entry_price": entry_price,
        "was_correct": was_correct,
    }, strategy_version=strategy_version)


def error(module: str, error_type: str, message: str,
          severity: str = "error"):
    """Emit an error record."""
    emit("error", {
        "module": module,
        "error": error_type,
        "message": str(message)[:500],
        "severity": severity,
    })


def regime(label: str, confidence: float, features: dict | None = None):
    """Emit a regime classification record."""
    emit("regime", {
        "label": label,
        "confidence": confidence,
        "features": features or {},
    })


def _ttl_hours(market: dict) -> float | None:
    """Compute hours to resolution from market data."""
    close_str = market.get("close_time") or market.get("end_date")
    if not close_str:
        return None
    try:
        close = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (close - now).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None
