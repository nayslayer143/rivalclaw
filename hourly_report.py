#!/usr/bin/env python3
"""
RivalClaw hourly report — runs after every self-tuner cycle.
Shows stats, trends, strategy leaderboard, diagnosis, and what changed.
"""
from __future__ import annotations
import datetime
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
REPORT_PATH = Path(__file__).parent / "daily" / "hourly-latest.md"


def generate():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    now = datetime.datetime.utcnow()
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    def p(s=""):
        lines.append(s)
        print(s)

    # --- Header ---
    p(f"# RIVALCLAW HOURLY REPORT — {ts}")
    p()

    # --- Current State ---
    starting = float(conn.execute("SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'").fetchone()[0])
    closed_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'").fetchone()[0]
    balance = starting + closed_pnl
    total = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status != 'open'").fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE pnl > 0 AND status != 'open'").fetchone()[0]
    losses = closed - wins
    open_pos = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]
    wr = (wins / closed * 100) if closed > 0 else 0

    p(f"## Wallet")
    p(f"| Metric | Value |")
    p(f"|--------|-------|")
    p(f"| Balance | ${balance:,.2f} |")
    p(f"| Total PnL | ${closed_pnl:+,.2f} ({closed_pnl/starting*100:+.1f}%) |")
    p(f"| Trades | {total} (W:{wins} L:{losses} Open:{open_pos}) |")
    p(f"| Win Rate | {wr:.1f}% |")
    cap_cycled = conn.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM paper_trades").fetchone()[0]
    p(f"| Capital Velocity | {cap_cycled/starting:.1f}x |")
    p()

    # --- Hourly Trend ---
    p(f"## Hourly Trend")
    p(f"| Hour | Trades | Wins | WR% | Avg PnL | Total PnL |")
    p(f"|------|--------|------|-----|---------|-----------|")
    hours = conn.execute("""
        SELECT substr(closed_at, 1, 13) as hr,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(AVG(pnl), 2) as avg_pnl,
               ROUND(SUM(pnl), 2) as total_pnl
        FROM paper_trades WHERE status != 'open' AND closed_at IS NOT NULL
        GROUP BY hr ORDER BY hr DESC LIMIT 8
    """).fetchall()
    for h in reversed(list(hours)):
        wr_h = (h["wins"] / h["trades"] * 100) if h["trades"] > 0 else 0
        p(f"| {h['hr'][5:]} | {h['trades']} | {h['wins']} | {wr_h:.0f}% | ${h['avg_pnl']:.2f} | ${h['total_pnl']:+,.2f} |")
    p()

    # --- Strategy Leaderboard ---
    p(f"## Strategy Leaderboard")
    p(f"| Strategy | Trades | WR% | W/L Ratio | PnL | Status |")
    p(f"|----------|--------|-----|-----------|-----|--------|")
    strats = conn.execute("""
        SELECT strategy,
               COUNT(*) as total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
               ROUND(SUM(pnl), 2) as pnl,
               ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2) as avg_win,
               ROUND(AVG(CASE WHEN pnl <= 0 THEN pnl END), 2) as avg_loss
        FROM paper_trades WHERE status != 'open'
        GROUP BY strategy ORDER BY pnl DESC
    """).fetchall()
    for s in strats:
        wr_s = (s["wins"] / (s["wins"] + s["losses"]) * 100) if (s["wins"] + s["losses"]) > 0 else 0
        avg_w = s["avg_win"] or 0
        avg_l = s["avg_loss"] or -1
        ratio = abs(avg_w / avg_l) if avg_l and avg_l != 0 else 0
        if s["pnl"] > 100 and wr_s > 40:
            status = "WINNING"
        elif s["pnl"] > 0:
            status = "positive"
        elif s["pnl"] > -50:
            status = "testing"
        else:
            status = "LOSING"
        p(f"| {s['strategy']} | {s['total']} | {wr_s:.0f}% | {ratio:.1f}x | ${s['pnl']:+,.2f} | {status} |")
    p()

    # --- Tuner Changes ---
    p(f"## Tuner Changes (this cycle)")
    tuner = conn.execute("""
        SELECT parameter, old_value, new_value, reason, sample_size
        FROM tuning_log ORDER BY id DESC LIMIT 5
    """).fetchall()
    has_changes = False
    for t in tuner:
        if t["parameter"] not in ("none", "cooldown"):
            p(f"- **{t['parameter']}**: {t['old_value']} -> {t['new_value']} ({t['reason'][:70]}, n={t['sample_size']})")
            has_changes = True
    if not has_changes:
        p("No parameter changes (insufficient data or within tolerance)")
    p()

    # --- Diagnosis ---
    p(f"## Diagnosis")

    # Strategies to kill
    for s in strats:
        if s["pnl"] < -100 and s["total"] > 20:
            p(f"- KILL: **{s['strategy']}** — {s['total']} trades, ${s['pnl']:.0f} PnL, dead weight")
        elif s["pnl"] < 0 and s["total"] > 10:
            p(f"- WATCH: **{s['strategy']}** — negative ${s['pnl']:.0f} after {s['total']} trades")

    # Win rate trend
    hours_list = list(hours)
    if len(hours_list) >= 4:
        recent = hours_list[:3]
        older = hours_list[3:]
        recent_trades = sum(h["trades"] for h in recent)
        recent_wins = sum(h["wins"] for h in recent)
        older_trades = sum(h["trades"] for h in older)
        older_wins = sum(h["wins"] for h in older)
        recent_wr = (recent_wins / recent_trades * 100) if recent_trades > 0 else 0
        older_wr = (older_wins / older_trades * 100) if older_trades > 0 else 0
        if recent_wr > older_wr + 5:
            p(f"- IMPROVING: Win rate trending up ({older_wr:.0f}% -> {recent_wr:.0f}%)")
        elif recent_wr < older_wr - 5:
            p(f"- DEGRADING: Win rate trending down ({older_wr:.0f}% -> {recent_wr:.0f}%)")
        else:
            p(f"- STABLE: Win rate holding ({recent_wr:.0f}%)")

    cycles = conn.execute("SELECT COUNT(*) FROM cycle_metrics").fetchone()[0]
    p(f"- Cycles run: {cycles}")

    # Regime
    try:
        import risk_engine
        regime = risk_engine.detect_regime()
        p(f"- Market regime: **{regime.get('regime', 'unknown')}** (vol={regime.get('vol', 0):.4f})")
    except Exception:
        pass

    p()
    p(f"---")
    p(f"*Next tuner: top of the hour | Next daily report: 9pm PST*")

    conn.close()

    # Write to file
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))

    # Also append to running log
    log_path = REPORT_PATH.parent / "hourly-log.md"
    with open(log_path, "a") as f:
        f.write("\n".join(lines) + "\n\n---\n\n")


if __name__ == "__main__":
    generate()
