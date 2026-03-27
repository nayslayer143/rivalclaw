#!/usr/bin/env python3
"""
Strategy Lab research cycle orchestrator.
Runs: diagnose → hypothesize → backtest → report

CLI:
  python strategy_lab/run_cycle.py                    # full cycle
  python strategy_lab/run_cycle.py --diagnose-only     # just diagnostic
  python strategy_lab/run_cycle.py --hypothesis hyp-id  # backtest specific hypothesis
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy_lab.diagnose import run_diagnostic
from strategy_lab.hypothesize import generate_hypotheses
from strategy_lab.backtest import run_backtest

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
LEDGER_PATH = Path(__file__).resolve().parent.parent / "experiments" / "ledger.json"
MAX_CANDIDATES_PER_CYCLE = 10


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


def run_full_cycle() -> dict:
    """Execute a complete research cycle: diagnose → hypothesize → backtest."""
    now = datetime.datetime.utcnow()
    ts = now.strftime("%Y%m%d-%H%M")
    print(f"\n{'='*60}")
    print(f"STRATEGY LAB — Research Cycle {ts}")
    print(f"{'='*60}\n")

    # Step 1: Diagnose
    print("[1/3] Running diagnostics...")
    diagnostic = run_diagnostic()
    if diagnostic.get("error"):
        print(f"[strategy_lab] Diagnostic failed: {diagnostic['error']}")
        return {"error": diagnostic["error"]}

    strats = diagnostic.get("strategies", {})
    print(f"  → {len(strats)} strategies analyzed, "
          f"{len(diagnostic.get('drift_flags', {}))} drift flags, "
          f"{len(diagnostic.get('missed_opportunities', []))} missed opps")

    # Step 2: Generate hypotheses
    print("\n[2/3] Generating hypotheses...")
    hypotheses = generate_hypotheses(diagnostic)
    if not hypotheses:
        print("  → No hypotheses generated (Ollama may be offline)")
        cycle_report = {
            "cycle_ts": now.isoformat(),
            "diagnostic": diagnostic,
            "hypotheses": [],
            "backtest_results": [],
            "summary": "No hypotheses generated",
        }
        _save_cycle_report(ts, cycle_report)
        return cycle_report

    print(f"  → {len(hypotheses)} hypotheses generated")
    for h in hypotheses:
        print(f"    • {h.get('id', '?')}: {h.get('proposed_change', '')[:60]}")

    # Step 3: Backtest each hypothesis
    print(f"\n[3/3] Backtesting {len(hypotheses)} hypotheses...")
    backtest_results = []
    ledger = _load_ledger()

    for h in hypotheses[:MAX_CANDIDATES_PER_CYCLE]:
        print(f"\n  Testing: {h.get('candidate_version', '?')}...")
        result = run_backtest(h)
        backtest_results.append(result)
        # Record in ledger (Mandate 5: first-class experiment object)
        ledger["experiments"].append({
            "experiment_id": result.get("experiment_id", ""),
            "parent_version": h.get("parent_version", ""),
            "candidate_version": h.get("candidate_version", ""),
            "hypothesis": h,
            "mutation_type": h.get("mutation_type", ""),
            "data_window_train": result.get("data_window", ""),
            "data_window_test": "",  # populated if shadow-tested
            "evaluation_status": "complete",
            "key_metrics": {
                "baseline": {
                    "pnl": result.get("pnl_baseline", 0),
                    "sharpe": result.get("sharpe_baseline", 0),
                    "win_rate": result.get("win_rate_baseline", 0),
                    "max_dd": result.get("max_dd_baseline", 0),
                    "trade_count": result.get("trade_count_baseline", 0),
                },
                "candidate": {
                    "pnl": result.get("pnl_candidate", 0),
                    "sharpe": result.get("sharpe_candidate", 0),
                    "win_rate": result.get("win_rate_candidate", 0),
                    "max_dd": result.get("max_dd_candidate", 0),
                    "trade_count": result.get("trade_count_candidate", 0),
                },
            },
            "verdict": result.get("verdict", "INCONCLUSIVE"),
            "verdict_reason": result.get("notes", ""),
            "rollback_status": None,
            "lesson": "",  # populated by governor on rollback
            "run_at": now.isoformat(),
        })

    _save_ledger(ledger)

    # Summary
    promising = [r for r in backtest_results if r.get("verdict") == "PROMISING"]
    rejected = [r for r in backtest_results if r.get("verdict") == "REJECTED"]
    inconclusive = [r for r in backtest_results if r.get("verdict") == "INCONCLUSIVE"]

    print(f"\n{'='*60}")
    print(f"CYCLE COMPLETE: {len(promising)} promising, "
          f"{len(rejected)} rejected, {len(inconclusive)} inconclusive")
    print(f"{'='*60}\n")

    cycle_report = {
        "cycle_ts": now.isoformat(),
        "diagnostic": diagnostic,
        "hypotheses": hypotheses,
        "backtest_results": backtest_results,
        "summary": {
            "promising": len(promising),
            "rejected": len(rejected),
            "inconclusive": len(inconclusive),
        },
    }
    _save_cycle_report(ts, cycle_report)
    return cycle_report


def _save_cycle_report(ts: str, report: dict):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"cycle-{ts}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"[strategy_lab] Cycle report saved: {out}")


def main():
    parser = argparse.ArgumentParser(description="Strategy Lab research cycle")
    parser.add_argument("--diagnose-only", action="store_true",
                        help="Run diagnostic only (no hypotheses or backtests)")
    parser.add_argument("--hypothesis", type=str, default=None,
                        help="Backtest a specific hypothesis ID from the ledger")
    args = parser.parse_args()

    if args.diagnose_only:
        report = run_diagnostic()
        print(json.dumps(report, indent=2, default=str))
        return

    if args.hypothesis:
        # Find hypothesis in ledger
        ledger = _load_ledger()
        hyp = None
        for exp in ledger.get("experiments", []):
            h = exp.get("hypothesis", {})
            if h.get("id") == args.hypothesis:
                hyp = h
                break
        if hyp is None:
            print(f"Hypothesis {args.hypothesis} not found in ledger")
            sys.exit(1)
        result = run_backtest(hyp)
        print(json.dumps(result, indent=2))
        return

    run_full_cycle()


if __name__ == "__main__":
    main()
