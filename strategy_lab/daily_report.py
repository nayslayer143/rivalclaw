#!/usr/bin/env python3
"""
Strategy Lab daily report generator.

Outputs: ~/rivalclaw/daily/strategy-lab-{date}.md

Sections:
  1. System Health — active/shadow/degraded/retired counts
  2. Key Findings — top positive, top negative, regime observations
  3. Candidate Hypotheses — active experiments
  4. Evaluation Results — backtest summaries
  5. Promotions/Demotions — state changes
  6. Memory Updates — new lessons
  7. Next Actions — what to test next
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

DAILY_DIR = Path(__file__).resolve().parent.parent / "daily"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "strategy_registry.json"
MEMORY_PATH = Path(__file__).resolve().parent / "memory.json"
LEDGER_PATH = Path(__file__).resolve().parent.parent / "experiments" / "ledger.json"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _latest_diagnostic() -> dict | None:
    diags = sorted(REPORTS_DIR.glob("diagnostic-*.json"), reverse=True)
    if not diags:
        return None
    try:
        return json.loads(diags[0].read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def _latest_cycle() -> dict | None:
    cycles = sorted(REPORTS_DIR.glob("cycle-*.json"), reverse=True)
    if not cycles:
        return None
    try:
        return json.loads(cycles[0].read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return None


def generate_daily_report() -> str:
    """Generate the Strategy Lab daily report as markdown."""
    today = datetime.date.today().isoformat()
    registry = _load_json(REGISTRY_PATH)
    memory = _load_json(MEMORY_PATH)
    ledger = _load_json(LEDGER_PATH)
    diagnostic = _latest_diagnostic()
    cycle = _latest_cycle()

    strategies = registry.get("strategies", [])

    # Section 1: System Health
    status_counts = {}
    for s in strategies:
        st = s.get("status", "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1

    lines = [
        f"# Strategy Lab Report — {today}",
        "",
        "## 1. System Health",
        "",
        f"| Status | Count |",
        f"|--------|-------|",
    ]
    for st in ["production", "shadow_live", "probationary", "degraded", "retired", "rolled_back"]:
        if status_counts.get(st, 0) > 0:
            lines.append(f"| {st} | {status_counts[st]} |")
    lines.append(f"| **Total** | **{len(strategies)}** |")
    lines.append("")

    # Section 2: Key Findings
    lines.append("## 2. Key Findings")
    lines.append("")
    if diagnostic:
        strats = diagnostic.get("strategies", {})
        if strats:
            # Top performer
            best = max(strats.items(), key=lambda x: x[1].get("total_pnl", 0), default=None)
            if best:
                lines.append(f"- **Top performer:** {best[0]} — "
                             f"${best[1].get('total_pnl', 0):+.2f} PnL, "
                             f"{best[1].get('win_rate', 0):.0%} WR")
            # Worst performer
            worst = min(strats.items(), key=lambda x: x[1].get("total_pnl", 0), default=None)
            if worst and worst[0] != (best[0] if best else ""):
                lines.append(f"- **Worst performer:** {worst[0]} — "
                             f"${worst[1].get('total_pnl', 0):+.2f} PnL, "
                             f"{worst[1].get('win_rate', 0):.0%} WR")

        # Drift flags
        drift = diagnostic.get("drift_flags", {})
        if drift:
            for s, info in drift.items():
                lines.append(f"- **Drift detected:** {s} — {info.get('severity', 'unknown')} "
                             f"(recent avg ${info.get('recent_avg_pnl', 0):.2f} vs "
                             f"older avg ${info.get('older_avg_pnl', 0):.2f})")

        # Regime
        regime_perf = diagnostic.get("regime_performance", {})
        if regime_perf:
            for r, info in regime_perf.items():
                lines.append(f"- **Regime {r}:** {info.get('trades', 0)} trades, "
                             f"${info.get('pnl', 0):+.2f} PnL")

        # Calibration
        cal = diagnostic.get("calibration", {})
        if cal.get("total", 0) > 10:
            overconf = cal.get("overconfidence", 0)
            label = "overconfident" if overconf > 0.05 else "well-calibrated" if abs(overconf) < 0.05 else "underconfident"
            lines.append(f"- **Signal calibration:** {label} "
                         f"(confidence {cal.get('avg_confidence', 0):.2f} vs "
                         f"actual {cal.get('actual_win_rate', 0):.2f})")
    else:
        lines.append("- No diagnostic data available yet")
    lines.append("")

    # Section 3: Candidate Hypotheses
    lines.append("## 3. Candidate Hypotheses")
    lines.append("")
    if cycle:
        hyps = cycle.get("hypotheses", [])
        if hyps:
            for h in hyps:
                lines.append(f"- **{h.get('id', '?')}** ({h.get('mutation_type', '?')}): "
                             f"{h.get('proposed_change', '')[:80]}")
        else:
            lines.append("- No hypotheses generated this cycle")
    else:
        lines.append("- No research cycle run today")
    lines.append("")

    # Section 4: Evaluation Results
    lines.append("## 4. Evaluation Results")
    lines.append("")
    if cycle:
        results = cycle.get("backtest_results", [])
        if results:
            lines.append("| Candidate | Baseline | PnL Δ | Sharpe Δ | Verdict |")
            lines.append("|-----------|----------|-------|----------|---------|")
            for r in results:
                pnl_delta = r.get("pnl_candidate", 0) - r.get("pnl_baseline", 0)
                sharpe_delta = r.get("sharpe_candidate", 0) - r.get("sharpe_baseline", 0)
                lines.append(f"| {r.get('candidate', '?')} | {r.get('baseline', '?')} | "
                             f"${pnl_delta:+.2f} | {sharpe_delta:+.2f} | "
                             f"**{r.get('verdict', '?')}** |")
        else:
            lines.append("- No backtests completed")
    else:
        lines.append("- No evaluations available")
    lines.append("")

    # Section 5: Promotions/Demotions
    lines.append("## 5. Promotions / Demotions")
    lines.append("")
    # Check today's lessons for promotion/demotion events
    today_lessons = [l for l in memory.get("lessons", [])
                     if l.get("date") == today]
    if today_lessons:
        for l in today_lessons:
            lines.append(f"- **{l.get('outcome', '?')}**: {l.get('experiment_id', '?')} — "
                         f"{l.get('mutation', '')[:80]}")
    else:
        lines.append("- No state changes today")
    lines.append("")

    # Section 6: Memory Updates
    lines.append("## 6. Memory Updates")
    lines.append("")
    recent_lessons = memory.get("lessons", [])[-5:]
    if recent_lessons:
        for l in recent_lessons:
            lines.append(f"- [{l.get('date', '?')}] {l.get('lesson', '')[:100]}")
    else:
        lines.append("- No lessons recorded yet")
    lines.append("")

    # Section 7: Next Actions
    lines.append("## 7. Next Actions")
    lines.append("")
    shadow_count = status_counts.get("shadow_live", 0)
    probation_count = status_counts.get("probationary", 0)
    if shadow_count > 0:
        lines.append(f"- Monitor {shadow_count} shadow candidate(s) for promotion readiness")
    if probation_count > 0:
        lines.append(f"- Evaluate {probation_count} probationary strategy(ies) for full promotion")
    if diagnostic and diagnostic.get("drift_flags"):
        lines.append(f"- Investigate {len(diagnostic['drift_flags'])} drifting strategy(ies)")
    if diagnostic and diagnostic.get("missed_opportunities"):
        missed = diagnostic["missed_opportunities"][:3]
        for m in missed:
            lines.append(f"- Missed: {m.get('title', '?')[:50]} (Δ{m.get('price_move', 0):.0%})")
    if not any([shadow_count, probation_count,
                diagnostic and diagnostic.get("drift_flags")]):
        lines.append("- Continue data collection for next research cycle")
    lines.append("")

    # Mandate 7: Weekly Cemetery Review (every Monday)
    if datetime.date.today().weekday() == 0:  # Monday
        lines.append("## 8. Weekly Cemetery Review")
        lines.append("")

        # Strategies in degraded or retired state
        dead = [s for s in strategies
                if s.get("status") in ("degraded", "retired", "rolled_back")]
        if dead:
            lines.append("### Degraded / Retired Strategies")
            lines.append("")
            for s in dead:
                lines.append(f"- **{s['id']}** ({s.get('status', '?')}) — "
                             f"{s.get('notes', 'no notes')[:80]}")
        else:
            lines.append("- No strategies in cemetery")
        lines.append("")

        # Recurring causes of death
        all_lessons = memory.get("lessons", [])
        if all_lessons:
            death_reasons = {}
            for l in all_lessons:
                family = l.get("strategy_family", "unknown")
                death_reasons.setdefault(family, []).append(l.get("outcome", ""))
            recurring = {f: reasons for f, reasons in death_reasons.items()
                         if len(reasons) >= 2}
            if recurring:
                lines.append("### Recurring Failure Patterns")
                lines.append("")
                for family, reasons in recurring.items():
                    lines.append(f"- **{family}**: {len(reasons)} failures — "
                                 f"{reasons[-1][:60]}")
            lines.append("")

        # Reconsideration candidates: regime changed or analysis was inconclusive
        recon = [s for s in dead if s.get("status") == "retired"]
        if recon:
            lines.append("### Reconsideration Candidates")
            lines.append("")
            for s in recon:
                lines.append(f"- {s['id']}: review if regime has shifted or "
                             f"original failure was INCONCLUSIVE")
        lines.append("")

        # Complexity creep check: count active variants
        production = [s for s in strategies if s.get("status") == "production"]
        shadow = [s for s in strategies if s.get("status") == "shadow_live"]
        probation = [s for s in strategies if s.get("status") == "probationary"]
        lines.append(f"### Complexity Check")
        lines.append("")
        lines.append(f"- Production: {len(production)} strategies")
        lines.append(f"- Shadow: {len(shadow)} candidates")
        lines.append(f"- Probation: {len(probation)} candidates")
        lines.append(f"- Cemetery: {len(dead)} strategies")
        lines.append(f"- **Total variants: {len(strategies)}** "
                     f"(watch for >20 = complexity creep)")
        lines.append("")

    report = "\n".join(lines)

    # Save
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out = DAILY_DIR / f"strategy-lab-{today}.md"
    out.write_text(report)
    print(f"[strategy_lab/daily_report] Saved: {out}")
    return report


if __name__ == "__main__":
    report = generate_daily_report()
    print(report)
