#!/usr/bin/env python3
"""
Strategy Lab backtester — replays historical events.jsonl with candidate parameters.

Compares candidate vs baseline on the same data window.
Verdict: PROMISING / INCONCLUSIVE / REJECTED
"""
from __future__ import annotations

import datetime
import json
import math
import os
from collections import defaultdict
from pathlib import Path

LOGS_DIR = Path(os.environ.get("RIVALCLAW_LOGS_DIR", Path(__file__).resolve().parent.parent / "logs"))
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "strategy_registry.json"

# Execution sim constants (match paper_wallet.py)
SLIPPAGE_BPS = 50
FEE_RATE_POLY = 0.02
FEE_RATE_KALSHI = 0.07
MAX_POSITION_PCT = 0.10
STARTING_BALANCE = 1000.0


def _load_events(days: int = 14) -> list[dict]:
    """Load events from events.jsonl for the specified window."""
    path = LOGS_DIR / "events.jsonl"
    if not path.exists():
        return []
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "") >= cutoff:
                        events.append(rec)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return events


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"strategies": []}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except json.JSONDecodeError:
        return {"strategies": []}


def _get_baseline_params(parent_version: str) -> dict | None:
    """Get parameters for a strategy version from the registry."""
    registry = _load_registry()
    for s in registry.get("strategies", []):
        if s["id"] == parent_version:
            return s.get("params", {})
    return None


def _simulate_strategy(events: list[dict], strategy_family: str,
                       params: dict, balance: float = STARTING_BALANCE) -> dict:
    """
    Replay events with given strategy parameters.
    Returns performance metrics.
    """
    snapshots = [e for e in events if e["type"] == "market_snapshot"]
    outcomes_by_market = defaultdict(list)
    for e in events:
        if e["type"] == "outcome":
            outcomes_by_market[e.get("market_id", "")].append(e)

    # Group snapshots by market for price history
    market_prices = defaultdict(list)
    for s in snapshots:
        mid = s.get("market_id", "")
        market_prices[mid].append({
            "ts": s.get("ts", ""),
            "yes_price": s.get("yes_price"),
            "no_price": s.get("no_price"),
            "volume": s.get("volume"),
            "platform": s.get("platform", "polymarket"),
            "time_to_resolution_hours": s.get("time_to_resolution_hours"),
        })

    trades = []
    current_balance = balance
    open_positions = {}

    # Get strategy-relevant parameters
    min_edge = params.get("min_edge", params.get("min_fv_edge", 0.04))
    min_confidence = params.get("min_confidence", 0.55)
    min_resolution_hours = params.get("min_resolution_hours", 0)
    max_hold_hours = params.get("max_hold_hours", 48)
    size_pct = params.get("size_pct", MAX_POSITION_PCT)

    # Replay signals from event log for this strategy
    signals = [e for e in events
                if e["type"] == "signal" and e.get("strategy", "") == strategy_family]

    for sig in signals:
        mid = sig.get("market_id", "")
        if mid in open_positions:
            continue

        confidence = sig.get("confidence", 0)
        edge = sig.get("edge_estimate", 0)

        # Apply candidate parameters as filters
        if confidence < min_confidence:
            continue
        if edge < min_edge:
            continue

        # Check resolution time filter
        prices = market_prices.get(mid, [])
        if prices and min_resolution_hours > 0:
            ttl = prices[-1].get("time_to_resolution_hours")
            if ttl is not None and ttl < min_resolution_hours:
                continue

        # Simulate entry
        direction = sig.get("direction", "YES")
        price_data = prices[-1] if prices else {}
        entry_price = price_data.get("yes_price", 0.5) if direction == "YES" else (
            1.0 - (price_data.get("yes_price", 0.5) or 0.5))

        if entry_price <= 0.01 or entry_price >= 0.99:
            continue

        # Apply slippage
        slippage = SLIPPAGE_BPS / 10000.0
        adj_price = min(0.99, entry_price + entry_price * slippage)

        # Size with position limit
        position_size = min(current_balance * size_pct, current_balance * 0.10)
        if position_size < 1.0:
            continue

        # Fee
        platform = price_data.get("platform", "polymarket")
        fee_rate = FEE_RATE_KALSHI if platform == "kalshi" else FEE_RATE_POLY
        fees = fee_rate * min(adj_price, 1.0 - adj_price) * position_size

        trade = {
            "market_id": mid, "direction": direction, "entry_price": adj_price,
            "size": position_size, "fees": fees, "ts": sig.get("ts", ""),
            "strategy": strategy_family,
        }
        open_positions[mid] = trade
        trades.append(trade)

    # Resolve trades using outcome events
    resolved_pnls = []
    for t in trades:
        mid = t["market_id"]
        market_outcomes = outcomes_by_market.get(mid, [])
        if market_outcomes:
            # Use first matching outcome
            o = market_outcomes[0]
            resolved_price = o.get("resolved_price", t["entry_price"])
            pnl_gross = (t["size"] / t["entry_price"]) * (resolved_price - t["entry_price"])
            pnl_net = pnl_gross - t["fees"]
            resolved_pnls.append(pnl_net)
            t["pnl_net"] = pnl_net
            t["resolved"] = True
        else:
            t["resolved"] = False

    # Compute metrics
    total_pnl = sum(resolved_pnls)
    wins = sum(1 for p in resolved_pnls if p > 0)
    win_rate = wins / len(resolved_pnls) if resolved_pnls else 0

    # Sharpe
    sharpe = 0.0
    if len(resolved_pnls) >= 5:
        mean_pnl = total_pnl / len(resolved_pnls)
        var = sum((p - mean_pnl) ** 2 for p in resolved_pnls) / len(resolved_pnls)
        std = math.sqrt(var) if var > 0 else 0.001
        sharpe = (mean_pnl / std) * math.sqrt(252)

    # Max drawdown
    max_dd = 0.0
    peak = 0.0
    cumulative = 0.0
    for p in resolved_pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    return {
        "trade_count": len(trades),
        "resolved_count": len(resolved_pnls),
        "pnl": total_pnl,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "avg_pnl": total_pnl / len(resolved_pnls) if resolved_pnls else 0,
    }


def run_backtest(hypothesis: dict, data_days: int = 14) -> dict:
    """
    Backtest a hypothesis: compare candidate vs baseline on same data.
    Returns experiment result with verdict.
    """
    events = _load_events(data_days)
    if not events:
        return {"error": "no events available for backtest"}

    parent_version = hypothesis.get("parent_version", "")
    baseline_params = _get_baseline_params(parent_version)
    if baseline_params is None:
        baseline_params = {}

    # Determine strategy family from parent version
    family = parent_version.split("_v")[0] if "_v" in parent_version else parent_version

    # Build candidate params (baseline + proposed changes)
    candidate_params = dict(baseline_params)
    proposed = hypothesis.get("proposed_change", "")
    mutation = hypothesis.get("mutation_type", "")

    # Apply mutation based on type
    if mutation == "threshold_adjust" or mutation == "param_tune":
        # Parse param changes from the hypothesis
        # The hypothesis should specify which param and new value
        for key in ["min_edge", "min_fv_edge", "min_confidence", "size_pct",
                     "min_resolution_hours", "max_hold_hours", "min_reversion_edge",
                     "min_decay_edge", "min_vol_skew_edge"]:
            if key in hypothesis:
                candidate_params[key] = hypothesis[key]
    elif mutation == "filter_addition":
        if "min_resolution_hours" in hypothesis:
            candidate_params["min_resolution_hours"] = hypothesis["min_resolution_hours"]
        if "min_confidence" in hypothesis:
            candidate_params["min_confidence"] = hypothesis["min_confidence"]
    elif mutation == "filter_removal":
        for key in ["min_resolution_hours", "min_confidence"]:
            if key in hypothesis:
                candidate_params.pop(key, None)

    # Run both simulations
    baseline_result = _simulate_strategy(events, family, baseline_params)
    candidate_result = _simulate_strategy(events, family, candidate_params)

    # Determine verdict
    verdict = "INCONCLUSIVE"
    min_trades = 20

    if candidate_result["resolved_count"] >= min_trades and baseline_result["resolved_count"] >= min_trades:
        better_pnl = candidate_result["pnl"] > baseline_result["pnl"]
        better_sharpe = candidate_result["sharpe"] > baseline_result["sharpe"]
        better_dd = candidate_result["max_dd"] <= baseline_result["max_dd"] * 1.05

        if better_pnl and better_sharpe and better_dd:
            verdict = "PROMISING"
        elif not better_pnl and not better_sharpe:
            verdict = "REJECTED"
    elif candidate_result["resolved_count"] < min_trades:
        verdict = "INCONCLUSIVE"

    today = datetime.date.today().isoformat()
    window_start = (datetime.datetime.utcnow() - datetime.timedelta(days=data_days)).strftime("%Y-%m-%d")

    result = {
        "experiment_id": f"exp-{today.replace('-', '')}-{hypothesis.get('id', 'unknown')[-3:]}",
        "hypothesis_id": hypothesis.get("id", "unknown"),
        "candidate": hypothesis.get("candidate_version", "unknown"),
        "baseline": parent_version,
        "data_window": f"{window_start} to {today}",
        "trade_count_candidate": candidate_result["trade_count"],
        "trade_count_baseline": baseline_result["trade_count"],
        "pnl_candidate": round(candidate_result["pnl"], 2),
        "pnl_baseline": round(baseline_result["pnl"], 2),
        "win_rate_candidate": round(candidate_result["win_rate"], 3),
        "win_rate_baseline": round(baseline_result["win_rate"], 3),
        "sharpe_candidate": round(candidate_result["sharpe"], 2),
        "sharpe_baseline": round(baseline_result["sharpe"], 2),
        "max_dd_candidate": round(candidate_result["max_dd"], 2),
        "max_dd_baseline": round(baseline_result["max_dd"], 2),
        "verdict": verdict,
        "notes": f"Mutation: {hypothesis.get('mutation_type', '')} — {hypothesis.get('proposed_change', '')}",
    }

    print(f"[strategy_lab/backtest] {hypothesis.get('candidate_version', '?')}: "
          f"PnL ${candidate_result['pnl']:+.2f} vs ${baseline_result['pnl']:+.2f} → {verdict}")
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        hyp_file = sys.argv[1]
        hyp = json.loads(Path(hyp_file).read_text())
    else:
        hyp = {
            "id": "hyp-test-001",
            "parent_version": "fair_value_directional_v1.0",
            "candidate_version": "fair_value_directional_v1.1",
            "mutation_type": "threshold_adjust",
            "proposed_change": "lower min_fv_edge from 0.04 to 0.03",
            "min_fv_edge": 0.03,
        }
    result = run_backtest(hyp)
    print(json.dumps(result, indent=2))
