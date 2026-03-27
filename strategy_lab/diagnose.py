#!/usr/bin/env python3
"""
Strategy Lab diagnostician — reads events.jsonl and produces per-strategy diagnostics.

Output: strategy_lab/reports/diagnostic-{date}.json
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
LOOKBACK_DAYS = int(os.environ.get("STRATEGY_LAB_LOOKBACK_DAYS", "14"))


def classify_degradation(strategy_id: str, events: list[dict],
                         drift_flags: dict, regime_perf: dict) -> str:
    """
    Mandate 4: Structural-Decay Detector.
    Classifies underperformance to prevent zombie strategy polishing.

    Returns one of:
    - "variance" — normal fluctuation, sample too small
    - "temporary_drift" — recent regime unfavorable, may recover
    - "parameter_drift" — tunable parameters are stale, self-tuner can fix
    - "structural_decay" — edge is dying, strategy family may need retirement
    - "data_issue" — missing/stale data causing false degradation signal
    - "bug" — behavior doesn't match expected logic
    """
    trades = [e for e in events if e["type"] == "trade" and not e.get("shadow")
              and e.get("strategy") == strategy_id]
    outcomes = [e for e in events if e["type"] == "outcome"]
    errors = [e for e in events if e["type"] == "error"]
    outcome_map = {str(o.get("trade_id", "")): o for o in outcomes}

    # Not enough data → variance
    if len(trades) < 10:
        return "variance"

    pnls = [outcome_map[str(t.get("trade_id", ""))].get("pnl_net", 0)
            for t in trades if str(t.get("trade_id", "")) in outcome_map]

    if len(pnls) < 5:
        return "variance"

    # Check for data issues: lots of errors from feeds?
    feed_errors = [e for e in errors if "feed" in e.get("module", "")]
    if len(feed_errors) > len(trades) * 0.3:
        return "data_issue"

    # Check if drift is regime-specific
    drift = drift_flags.get(strategy_id)
    if drift:
        # If the strategy does well in some regimes but not others → temporary drift
        regime_data = {}
        for e in events:
            if e["type"] == "regime":
                regime_data[e.get("ts", "")] = e.get("label", "unknown")
        if regime_data:
            # Recent regime might be unfavorable
            recent_regimes = sorted(regime_data.items(), reverse=True)[:20]
            recent_labels = [r[1] for r in recent_regimes]
            if len(set(recent_labels)) <= 2:
                return "temporary_drift"

    # Check if parameters are stale (self-tuner could fix)
    total_pnl = sum(pnls)
    recent_pnls = pnls[-10:]
    older_pnls = pnls[:-10] if len(pnls) > 10 else []

    if older_pnls and sum(older_pnls) > 0 and sum(recent_pnls) < 0:
        # Used to work, now doesn't — could be parameter drift
        decline_rate = abs(sum(recent_pnls)) / abs(sum(older_pnls))
        if decline_rate < 0.5:
            return "parameter_drift"

    # Structural decay: consistently losing across multiple regimes and time periods
    if total_pnl < 0 and len(pnls) >= 20:
        # Check if ANY regime is profitable
        any_regime_profitable = False
        for regime_label, data in regime_perf.items():
            if data.get("pnl", 0) > 0:
                any_regime_profitable = True
                break
        if not any_regime_profitable:
            return "structural_decay"

    # Default: could be parameter drift (self-tuner territory)
    if total_pnl < 0:
        return "parameter_drift"
    return "variance"


def _load_events(days: int = LOOKBACK_DAYS) -> list[dict]:
    """Load events from the last N days of events.jsonl."""
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


def _sharpe(pnls: list[float]) -> float:
    """Annualized Sharpe from daily PnLs (assumes ~4 trades/day cadence)."""
    if len(pnls) < 5:
        return 0.0
    mean_pnl = sum(pnls) / len(pnls)
    var = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
    std = math.sqrt(var) if var > 0 else 0.001
    return (mean_pnl / std) * math.sqrt(252)


def run_diagnostic(days: int = LOOKBACK_DAYS) -> dict:
    """Produce a full diagnostic report from event log data."""
    events = _load_events(days)
    if not events:
        return {"error": "no events found", "date": datetime.date.today().isoformat()}

    # Separate by type
    signals = [e for e in events if e["type"] == "signal"]
    decisions = [e for e in events if e["type"] == "decision"]
    trades = [e for e in events if e["type"] == "trade" and not e.get("shadow")]
    outcomes = [e for e in events if e["type"] == "outcome"]
    regimes = [e for e in events if e["type"] == "regime"]
    snapshots = [e for e in events if e["type"] == "market_snapshot"]

    # Build outcome lookup by trade_id
    outcome_by_trade = {str(o["trade_id"]): o for o in outcomes}

    # Per-strategy metrics
    strat_trades = defaultdict(list)
    for t in trades:
        strat_trades[t.get("strategy", "unknown")].append(t)

    strategy_report = {}
    for strat, strat_trade_list in strat_trades.items():
        pnls = []
        wins = 0
        max_dd = 0.0
        peak = 0.0
        cumulative = 0.0
        for t in strat_trade_list:
            o = outcome_by_trade.get(str(t.get("trade_id", "")))
            if o:
                pnl = o.get("pnl_net", 0)
                pnls.append(pnl)
                cumulative += pnl
                peak = max(peak, cumulative)
                dd = peak - cumulative
                max_dd = max(max_dd, dd)
                if o.get("was_correct"):
                    wins += 1

        trade_count = len(strat_trade_list)
        resolved = len(pnls)
        strategy_report[strat] = {
            "trade_count": trade_count,
            "resolved": resolved,
            "win_rate": wins / resolved if resolved > 0 else 0,
            "avg_pnl": sum(pnls) / resolved if resolved > 0 else 0,
            "total_pnl": sum(pnls),
            "sharpe": _sharpe(pnls),
            "max_drawdown": max_dd,
            "abstain_rate": 0,  # filled below
        }

    # Abstain rate per strategy
    strat_decisions = defaultdict(lambda: {"total": 0, "abstain": 0})
    for d in decisions:
        s = d.get("strategy", "none")
        strat_decisions[s]["total"] += 1
        if d.get("action") == "abstain":
            strat_decisions[s]["abstain"] += 1
    for s, counts in strat_decisions.items():
        if s in strategy_report and counts["total"] > 0:
            strategy_report[s]["abstain_rate"] = counts["abstain"] / counts["total"]

    # Regime breakdown — global AND per-strategy (Mandate 3: regime-conditional scorecards)
    regime_perf = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    regime_at_time = {}
    for r in sorted(regimes, key=lambda x: x.get("ts", "")):
        regime_at_time[r["ts"]] = r.get("label", "unknown")
    regime_labels = sorted(regime_at_time.keys())

    def _assign_regime(ts: str) -> str:
        label = "unknown"
        for rts in regime_labels:
            if rts <= ts:
                label = regime_at_time[rts]
        return label

    # Per-strategy × per-regime scorecards
    strat_regime = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "pnls": [], "trades": 0}))
    for t in trades:
        ts = t.get("ts", "")
        label = _assign_regime(ts)
        strat = t.get("strategy", "unknown")
        o = outcome_by_trade.get(str(t.get("trade_id", "")))
        regime_perf[label]["trades"] += 1
        strat_regime[strat][label]["trades"] += 1
        if o:
            pnl = o.get("pnl_net", 0)
            regime_perf[label]["pnl"] += pnl
            strat_regime[strat][label]["pnls"].append(pnl)
            if o.get("was_correct"):
                strat_regime[strat][label]["wins"] += 1

    # Build regime scorecards for each strategy
    for strat in strategy_report:
        by_regime = {}
        for regime_label, data in strat_regime.get(strat, {}).items():
            pnls_r = data["pnls"]
            resolved_r = len(pnls_r)
            by_regime[regime_label] = {
                "win_rate": data["wins"] / resolved_r if resolved_r > 0 else 0,
                "sharpe": _sharpe(pnls_r),
                "pnl": sum(pnls_r),
                "trades": data["trades"],
            }
        strategy_report[strat]["by_regime"] = by_regime

    # Drift detection: 7-day vs full window
    recent_cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    drift_flags = {}
    for strat, strat_trade_list in strat_trades.items():
        recent_pnls = []
        older_pnls = []
        for t in strat_trade_list:
            o = outcome_by_trade.get(str(t.get("trade_id", "")))
            if not o:
                continue
            pnl = o.get("pnl_net", 0)
            if t.get("ts", "") >= recent_cutoff:
                recent_pnls.append(pnl)
            else:
                older_pnls.append(pnl)
        if len(recent_pnls) >= 5 and len(older_pnls) >= 5:
            recent_avg = sum(recent_pnls) / len(recent_pnls)
            older_avg = sum(older_pnls) / len(older_pnls)
            if older_avg > 0 and recent_avg < older_avg * 0.5:
                drift_flags[strat] = {
                    "recent_avg_pnl": recent_avg,
                    "older_avg_pnl": older_avg,
                    "severity": "degraded" if recent_avg < 0 else "declining",
                }

    # Missed opportunities: markets with >20% price move but no trade
    traded_markets = {t.get("market_id") for t in trades}
    missed = []
    snap_by_market = defaultdict(list)
    for s in snapshots:
        snap_by_market[s.get("market_id", "")].append(s)
    for mid, snaps in snap_by_market.items():
        if mid in traded_markets:
            continue
        prices = [s.get("yes_price") for s in snaps if s.get("yes_price") is not None]
        if len(prices) >= 2:
            move = abs(prices[-1] - prices[0])
            if move > 0.20:
                missed.append({"market_id": mid, "price_move": move,
                               "title": snaps[0].get("title", "")[:80]})
    missed.sort(key=lambda x: x["price_move"], reverse=True)

    # Signal calibration: confidence vs actual outcome
    calibration = {"total": 0, "correct": 0, "avg_confidence": 0}
    confidences = []
    for s in signals:
        o = None
        for t in trades:
            if t.get("market_id") == s.get("market_id") and t.get("strategy") == s.get("strategy"):
                o = outcome_by_trade.get(str(t.get("trade_id", "")))
                break
        if o:
            calibration["total"] += 1
            confidences.append(s.get("confidence", 0))
            if o.get("was_correct"):
                calibration["correct"] += 1
    if confidences:
        calibration["avg_confidence"] = sum(confidences) / len(confidences)
    if calibration["total"] > 0:
        calibration["actual_win_rate"] = calibration["correct"] / calibration["total"]
        calibration["overconfidence"] = calibration["avg_confidence"] - calibration["actual_win_rate"]

    # Mandate 4: Structural-decay classification for every strategy
    degradation_flags = {}
    for strat in strategy_report:
        strat_regime_data = strat_regime.get(strat, {})
        strat_regime_summary = {k: {"pnl": sum(v["pnls"]), "trades": v["trades"]}
                                 for k, v in strat_regime_data.items()}
        classification = classify_degradation(strat, events, drift_flags, strat_regime_summary)
        if classification != "variance":
            degradation_flags[strat] = classification
        strategy_report[strat]["degradation"] = classification

    # Abstain reason breakdown
    abstain_reasons = defaultdict(int)
    for d in decisions:
        if d.get("action") == "abstain":
            abstain_reasons[d.get("reason", "unknown")] += 1

    report = {
        "date": datetime.date.today().isoformat(),
        "lookback_days": days,
        "event_count": len(events),
        "strategies": strategy_report,
        "regime_performance": dict(regime_perf),
        "drift_flags": drift_flags,
        "degradation_flags": degradation_flags,
        "missed_opportunities": missed[:10],
        "calibration": calibration,
        "abstain_breakdown": dict(abstain_reasons),
    }

    # Save report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"diagnostic-{datetime.date.today().isoformat()}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"[strategy_lab/diagnose] Report saved: {out}")
    return report


if __name__ == "__main__":
    report = run_diagnostic()
    print(json.dumps(report, indent=2, default=str))
