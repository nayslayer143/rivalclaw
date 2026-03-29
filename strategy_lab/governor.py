#!/usr/bin/env python3
"""
Strategy Lab promotion governor — manages strategy lifecycle.

States: draft → simulated → shadow_live → probationary → production → degraded → retired

Promotion rules (ALL must pass):
  - Shadow PnL > baseline PnL over 14 days
  - Shadow Sharpe > baseline Sharpe
  - Shadow max DD < baseline max DD (or within 5%)
  - Shadow win rate > 50%
  - At least 30 shadow trades
  - No single trade > 40% of total PnL
  - Robustness: remove best trade, still profitable

Demotion triggers (ANY):
  - 7-day rolling PnL negative
  - Drawdown > 25% of allocated capital
  - Win rate < 45% over 30+ trades
  - 3+ consecutive losses exceeding stop-loss
"""
from __future__ import annotations

import datetime
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import event_logger as elog

try:
    from catalog_reader import StrategyCatalog
except ImportError:
    StrategyCatalog = None  # type: ignore[assignment,misc]

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "strategy_registry.json"
MEMORY_PATH = Path(__file__).resolve().parent / "memory.json"
LEDGER_PATH = Path(__file__).resolve().parent.parent / "experiments" / "ledger.json"
LOGS_DIR = Path(os.environ.get("RIVALCLAW_LOGS_DIR",
                                Path(__file__).resolve().parent.parent / "logs"))

# Promotion thresholds
MIN_SHADOW_DAYS = int(os.environ.get("STRATEGY_LAB_MIN_SHADOW_DAYS", "14"))
MIN_SHADOW_TRADES = int(os.environ.get("STRATEGY_LAB_MIN_SHADOW_TRADES", "30"))
MIN_WIN_RATE = 0.50
MAX_SINGLE_TRADE_PCT = 0.40  # No single trade > 40% of total PnL
PROBATION_DAYS = 7
PROBATION_SIZE_MULT = 0.50  # 50% normal size during probation

# Demotion thresholds
DEMOTION_DD_PCT = 0.25
DEMOTION_WIN_RATE = 0.45
DEMOTION_MIN_TRADES = 30
DEMOTION_CONSECUTIVE_LOSSES = 3

# ---------------------------------------------------------------------------
# Mandate 6: Autonomy Boundary Enforcement
# Explicitly define what the lab CAN and CANNOT change.
# ---------------------------------------------------------------------------
LAB_CAN_CHANGE = [
    "entry_threshold", "exit_threshold", "confidence_threshold",
    "max_hold_hours", "min_resolution_hours", "min_liquidity",
    "size_pct", "cooldown_minutes", "regime_filter",
    "abstain_conditions", "strategy_status",
]
LAB_CANNOT_CHANGE = [
    "max_position_pct",     # global 10% cap
    "stop_loss_pct",        # global -20%
    "take_profit_pct",      # global +50%
    "kelly_cap",            # global Kelly ceiling
    "starting_capital",     # wallet settings
    "api_credentials",      # security
    "cron_schedule",        # operational
    "kill_switch",          # safety
]


def validate_mutation(hypothesis: dict) -> tuple[bool, str]:
    """
    Mandate 6: Reject any mutation that attempts to touch a CANNOT_CHANGE parameter.
    Returns (is_valid, reason).
    """
    proposed = hypothesis.get("proposed_change", "").lower()
    mutation_type = hypothesis.get("mutation_type", "")

    # Check for forbidden mutation types
    forbidden_types = {"full_rewrite", "new_strategy", "risk_override", "multi_model"}
    if mutation_type in forbidden_types:
        return False, f"forbidden mutation type: {mutation_type}"

    # Check if the proposed change touches any CANNOT_CHANGE parameter
    for param in LAB_CANNOT_CHANGE:
        if param in proposed:
            return False, f"autonomy violation: cannot modify {param}"

    return True, "ok"


def get_catalog_context(strategy_name: str) -> dict | None:
    """Look up a strategy in the openclaw-strategies catalog by name.

    Returns a dict with known_failure_modes, favorable/unfavorable regimes,
    and parameter definitions — useful context for governor decisions.
    Returns None if the catalog is unavailable or strategy not found.
    """
    if StrategyCatalog is None:
        return None
    try:
        catalog = StrategyCatalog()
    except Exception:
        return None
    if catalog.count == 0:
        return None

    # Search by name (case-insensitive partial match)
    matches = catalog.search(strategy_name)
    if not matches:
        return None

    entry = matches[0]  # Best match
    regimes = entry.get("market_regimes", {})
    return {
        "strategy_id": entry.get("strategy_id"),
        "name": entry.get("name"),
        "family": entry.get("family"),
        "known_failure_modes": entry.get("known_failure_modes", []),
        "favorable_regimes": regimes.get("favorable", []),
        "unfavorable_regimes": regimes.get("unfavorable", []),
        "parameters": entry.get("parameters", {}),
        "complexity_score": entry.get("complexity_score"),
        "alpha_type": entry.get("alpha_type"),
    }


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"strategies": []}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except json.JSONDecodeError:
        return {"strategies": []}


def _save_registry(registry: dict):
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2, default=str)


def _load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {"lessons": []}
    try:
        return json.loads(MEMORY_PATH.read_text())
    except json.JSONDecodeError:
        return {"lessons": []}


def _save_memory(memory: dict):
    with open(MEMORY_PATH, "w") as f:
        json.dump(memory, f, indent=2, default=str)


def _load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {"experiments": []}
    try:
        return json.loads(LEDGER_PATH.read_text())
    except json.JSONDecodeError:
        return {"experiments": []}


def _save_ledger(ledger: dict):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2, default=str)


def _update_ledger_rollback(candidate_id: str, reason: str):
    """Mandate 5: Update ledger with rollback info for this candidate."""
    ledger = _load_ledger()
    for exp in ledger.get("experiments", []):
        if exp.get("candidate_version") == candidate_id:
            exp["rollback_status"] = "rolled_back"
            exp["lesson"] = reason
            exp["evaluation_status"] = "complete"
    _save_ledger(ledger)


def _load_events(days: int = 30) -> list[dict]:
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


def get_shadow_candidates() -> list[dict]:
    """Return strategies in shadow_live state for the simulator to run."""
    registry = _load_registry()
    return [s for s in registry.get("strategies", [])
            if s.get("status") == "shadow_live"]


def _compute_shadow_metrics(candidate_id: str, events: list[dict]) -> dict | None:
    """Compute performance metrics for a shadow candidate from event logs."""
    shadow_trades = [e for e in events
                     if e["type"] == "trade" and e.get("shadow")
                     and e.get("strategy_version") == candidate_id]
    shadow_outcomes = [e for e in events
                       if e["type"] == "outcome"
                       and e.get("strategy_version") == candidate_id]

    if not shadow_trades:
        return None

    # Match outcomes to trades
    outcome_by_tid = {str(o.get("trade_id", "")): o for o in shadow_outcomes}
    pnls = []
    for t in shadow_trades:
        o = outcome_by_tid.get(str(t.get("trade_id", "")))
        if o:
            pnls.append(o.get("pnl_net", 0))

    if not pnls:
        return {"trade_count": len(shadow_trades), "resolved": 0}

    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) if pnls else 0

    # Sharpe
    sharpe = 0.0
    if len(pnls) >= 5:
        mean_p = total_pnl / len(pnls)
        var = sum((p - mean_p) ** 2 for p in pnls) / len(pnls)
        std = math.sqrt(var) if var > 0 else 0.001
        sharpe = (mean_p / std) * math.sqrt(252)

    # Max drawdown
    max_dd = 0.0
    peak = cumulative = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    # Single trade concentration
    max_single = max(pnls) if pnls else 0
    single_trade_pct = max_single / total_pnl if total_pnl > 0 else 1.0

    # Robustness: remove best trade
    if len(pnls) > 1:
        without_best = sorted(pnls)[:-1]
        robust_pnl = sum(without_best)
    else:
        robust_pnl = 0

    # First and last trade timestamps
    first_ts = shadow_trades[0].get("ts", "")
    last_ts = shadow_trades[-1].get("ts", "")
    try:
        first_dt = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        last_dt = datetime.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        days_active = (last_dt - first_dt).days
    except (ValueError, TypeError):
        days_active = 0

    return {
        "trade_count": len(shadow_trades),
        "resolved": len(pnls),
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "single_trade_pct": single_trade_pct,
        "robust_pnl": robust_pnl,
        "days_active": days_active,
    }


def _compute_production_metrics(family: str, events: list[dict]) -> dict | None:
    """Compute baseline metrics for a production strategy."""
    prod_trades = [e for e in events
                   if e["type"] == "trade" and not e.get("shadow")
                   and e.get("strategy") == family]
    outcomes = [e for e in events if e["type"] == "outcome"]
    outcome_by_tid = {str(o.get("trade_id", "")): o for o in outcomes}

    pnls = []
    for t in prod_trades:
        o = outcome_by_tid.get(str(t.get("trade_id", "")))
        if o:
            pnls.append(o.get("pnl_net", 0))

    if not pnls:
        return None

    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    sharpe = 0.0
    if len(pnls) >= 5:
        mean_p = total_pnl / len(pnls)
        var = sum((p - mean_p) ** 2 for p in pnls) / len(pnls)
        std = math.sqrt(var) if var > 0 else 0.001
        sharpe = (mean_p / std) * math.sqrt(252)

    max_dd = peak = cumulative = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    return {
        "total_pnl": total_pnl,
        "win_rate": wins / len(pnls) if pnls else 0,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "trade_count": len(pnls),
    }


def evaluate_promotion(candidate_id: str) -> dict:
    """Evaluate whether a shadow candidate can be promoted to probationary."""
    events = _load_events(days=30)
    shadow = _compute_shadow_metrics(candidate_id, events)
    if not shadow or shadow.get("resolved", 0) == 0:
        return {"verdict": "WAIT", "reason": "no resolved shadow trades yet"}

    registry = _load_registry()
    candidate = None
    for s in registry.get("strategies", []):
        if s["id"] == candidate_id:
            candidate = s
            break
    if not candidate:
        return {"verdict": "ERROR", "reason": f"candidate {candidate_id} not found"}

    family = candidate.get("family", "")
    baseline = _compute_production_metrics(family, events)

    checks = {}

    # Check minimum shadow period
    checks["min_days"] = shadow["days_active"] >= MIN_SHADOW_DAYS
    checks["min_trades"] = shadow["resolved"] >= MIN_SHADOW_TRADES
    checks["win_rate"] = shadow["win_rate"] >= MIN_WIN_RATE

    if baseline:
        checks["beats_pnl"] = shadow["total_pnl"] > baseline["total_pnl"]
        checks["beats_sharpe"] = shadow["sharpe"] > baseline["sharpe"]
        checks["beats_dd"] = shadow["max_dd"] <= baseline["max_dd"] * 1.05
    else:
        # No baseline data — candidate must be profitable on its own
        checks["beats_pnl"] = shadow["total_pnl"] > 0
        checks["beats_sharpe"] = shadow["sharpe"] > 0.5
        checks["beats_dd"] = shadow["max_dd"] < 50

    checks["no_outlier"] = shadow["single_trade_pct"] <= MAX_SINGLE_TRADE_PCT
    checks["robust"] = shadow["robust_pnl"] > 0

    all_pass = all(checks.values())
    failed = [k for k, v in checks.items() if not v]

    # Enrich with catalog context (known failure modes, regime info)
    catalog_ctx = get_catalog_context(family) if family else None

    result = {
        "verdict": "PROMOTE" if all_pass else "WAIT",
        "checks": checks,
        "failed": failed,
        "shadow_metrics": shadow,
        "baseline_metrics": baseline,
    }
    if catalog_ctx:
        result["catalog_context"] = catalog_ctx
    return result


def evaluate_demotion(strategy_id: str) -> dict:
    """Check if a production strategy should be demoted."""
    events = _load_events(days=30)

    registry = _load_registry()
    strategy = None
    for s in registry.get("strategies", []):
        if s["id"] == strategy_id:
            strategy = s
            break
    if not strategy:
        return {"verdict": "OK", "reason": "strategy not found"}

    family = strategy.get("family", "")
    metrics = _compute_production_metrics(family, events)
    if not metrics:
        return {"verdict": "OK", "reason": "insufficient data"}

    # 7-day rolling PnL
    recent_cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    recent_trades = [e for e in events
                     if e["type"] == "trade" and not e.get("shadow")
                     and e.get("strategy") == family and e.get("ts", "") >= recent_cutoff]
    outcomes = [e for e in events if e["type"] == "outcome"]
    outcome_by_tid = {str(o.get("trade_id", "")): o for o in outcomes}
    recent_pnls = []
    for t in recent_trades:
        o = outcome_by_tid.get(str(t.get("trade_id", "")))
        if o:
            recent_pnls.append(o.get("pnl_net", 0))

    triggers = {}
    triggers["rolling_pnl_negative"] = (len(recent_pnls) >= 5 and sum(recent_pnls) < 0)
    triggers["high_drawdown"] = metrics["max_dd"] > DEMOTION_DD_PCT * 1000  # rough cap ref
    triggers["low_win_rate"] = (metrics["trade_count"] >= DEMOTION_MIN_TRADES
                                 and metrics["win_rate"] < DEMOTION_WIN_RATE)

    # Check consecutive losses
    all_pnls = []
    prod_trades = [e for e in events
                   if e["type"] == "trade" and not e.get("shadow")
                   and e.get("strategy") == family]
    for t in sorted(prod_trades, key=lambda x: x.get("ts", "")):
        o = outcome_by_tid.get(str(t.get("trade_id", "")))
        if o:
            all_pnls.append(o.get("pnl_net", 0))
    consecutive_losses = 0
    max_consecutive = 0
    for p in all_pnls:
        if p < 0:
            consecutive_losses += 1
            max_consecutive = max(max_consecutive, consecutive_losses)
        else:
            consecutive_losses = 0
    triggers["consecutive_losses"] = max_consecutive >= DEMOTION_CONSECUTIVE_LOSSES

    any_triggered = any(triggers.values())
    triggered = [k for k, v in triggers.items() if v]

    # Enrich with catalog context (failure modes help explain demotion causes)
    catalog_ctx = get_catalog_context(family) if family else None

    result = {
        "verdict": "DEMOTE" if any_triggered else "OK",
        "triggers": triggers,
        "triggered": triggered,
        "metrics": metrics,
    }
    if catalog_ctx:
        result["catalog_context"] = catalog_ctx
    return result


def promote_candidate(candidate_id: str, to_state: str = "probationary"):
    """Move a candidate to a new state in the registry."""
    registry = _load_registry()
    for s in registry.get("strategies", []):
        if s["id"] == candidate_id:
            old_status = s["status"]
            s["status"] = to_state
            if to_state == "production":
                s["promoted_at"] = datetime.date.today().isoformat()
            _save_registry(registry)
            elog.emit("promotion", {
                "candidate_id": candidate_id,
                "from_state": old_status,
                "to_state": to_state,
            })
            print(f"[governor] {candidate_id}: {old_status} → {to_state}")
            return True
    return False


def demote_strategy(strategy_id: str, reason: str = ""):
    """Demote a strategy to degraded status."""
    registry = _load_registry()
    for s in registry.get("strategies", []):
        if s["id"] == strategy_id:
            old_status = s["status"]
            s["status"] = "degraded"
            _save_registry(registry)
            elog.emit("demotion", {
                "strategy_id": strategy_id,
                "from_state": old_status,
                "to_state": "degraded",
                "reason": reason,
            })
            print(f"[governor] {strategy_id}: {old_status} → degraded ({reason})")
            return True
    return False


def rollback_candidate(candidate_id: str, parent_version: str, reason: str = ""):
    """Revert a candidate back to its parent version."""
    registry = _load_registry()
    for s in registry.get("strategies", []):
        if s["id"] == candidate_id:
            s["status"] = "rolled_back"
            _save_registry(registry)
            break

    # Log the rollback
    elog.emit("rollback", {
        "candidate_id": candidate_id,
        "parent_version": parent_version,
        "reason": reason,
    })

    # Add lesson to memory (Mandate 3: regime-tagged)
    # Get current regime for context
    events = _load_events(days=1)
    current_regime = "unknown"
    for e in reversed(events):
        if e.get("type") == "regime":
            current_regime = e.get("label", "unknown")
            break

    memory = _load_memory()
    memory["lessons"].append({
        "date": datetime.date.today().isoformat(),
        "experiment_id": candidate_id,
        "strategy_family": candidate_id.split("_v")[0] if "_v" in candidate_id else candidate_id,
        "mutation": reason,
        "outcome": f"ROLLED_BACK — {reason}",
        "lesson": f"Candidate {candidate_id} failed in probation: {reason}",
        "regime_at_failure": current_regime,
        "do_not_repeat_unless": f"regime changes from {current_regime}",
    })
    _save_memory(memory)

    # Mandate 5: Update ledger with rollback status
    _update_ledger_rollback(candidate_id, reason)

    print(f"[governor] ROLLBACK: {candidate_id} → reverted to {parent_version}")


def auto_promote_cycle():
    """Run auto-promotion and demotion checks across all strategies."""
    registry = _load_registry()
    actions = []

    for s in registry.get("strategies", []):
        sid = s["id"]
        status = s.get("status", "")

        # Check shadow candidates for promotion
        if status == "shadow_live":
            result = evaluate_promotion(sid)
            if result["verdict"] == "PROMOTE":
                promote_candidate(sid, "probationary")
                actions.append(f"PROMOTED {sid} to probationary")
                memory = _load_memory()
                memory["lessons"].append({
                    "date": datetime.date.today().isoformat(),
                    "experiment_id": sid,
                    "strategy_family": s.get("family", ""),
                    "mutation": f"promoted from shadow to probationary",
                    "outcome": "PROMOTED",
                    "lesson": f"{sid} passed all promotion checks",
                    "do_not_repeat_unless": "",
                })
                _save_memory(memory)

        # Check probationary strategies
        elif status == "probationary":
            # Check if probation period is over
            promoted_at = s.get("promoted_at", "")
            if promoted_at:
                try:
                    promo_date = datetime.date.fromisoformat(promoted_at)
                    if (datetime.date.today() - promo_date).days >= PROBATION_DAYS:
                        # Probation period over — check demotion
                        demotion = evaluate_demotion(sid)
                        if demotion["verdict"] == "DEMOTE":
                            parent = sid.rsplit("_v", 1)[0] + "_v1.0"
                            rollback_candidate(sid, parent,
                                               f"Failed probation: {', '.join(demotion['triggered'])}")
                            actions.append(f"ROLLED_BACK {sid}")
                        else:
                            # Passed probation — promote to production
                            promote_candidate(sid, "production")
                            actions.append(f"PROMOTED {sid} to production")
                except (ValueError, TypeError):
                    pass
            else:
                # Check demotion immediately if no promoted_at date
                demotion = evaluate_demotion(sid)
                if demotion["verdict"] == "DEMOTE":
                    parent = sid.rsplit("_v", 1)[0] + "_v1.0"
                    rollback_candidate(sid, parent,
                                       f"Demotion triggered: {', '.join(demotion['triggered'])}")
                    actions.append(f"ROLLED_BACK {sid}")

        # Check production strategies for degradation
        elif status == "production":
            demotion = evaluate_demotion(sid)
            if demotion["verdict"] == "DEMOTE":
                demote_strategy(sid, f"Triggers: {', '.join(demotion['triggered'])}")
                actions.append(f"DEMOTED {sid}")

    return actions


def add_shadow_candidate(hypothesis: dict, backtest_result: dict):
    """Register a promising candidate for shadow testing."""
    # Mandate 6: Enforce autonomy boundaries before entering pipeline
    valid, reason = validate_mutation(hypothesis)
    if not valid:
        print(f"[governor] REJECTED: {hypothesis.get('candidate_version', '?')} — {reason}")
        return False
    registry = _load_registry()
    candidate = {
        "id": hypothesis.get("candidate_version", "unknown_v0.1"),
        "family": hypothesis.get("parent_version", "").split("_v")[0],
        "version": hypothesis.get("candidate_version", "").split("_v")[-1] if "_v" in hypothesis.get("candidate_version", "") else "0.1",
        "status": "shadow_live",
        "params": {},  # Will be populated from hypothesis
        "created_at": datetime.date.today().isoformat(),
        "promoted_at": None,
        "baseline_metrics": backtest_result,
        "notes": hypothesis.get("proposed_change", ""),
    }
    registry["strategies"].append(candidate)
    _save_registry(registry)
    print(f"[governor] Registered shadow candidate: {candidate['id']}")


if __name__ == "__main__":
    print("Running auto-promotion cycle...")
    actions = auto_promote_cycle()
    if actions:
        for a in actions:
            print(f"  → {a}")
    else:
        print("  No actions taken")
