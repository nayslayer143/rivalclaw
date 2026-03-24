#!/usr/bin/env python3
"""
RivalClaw graduation gates — exact parity with Clawmpson.
Thresholds and window match Mirofish dashboard.py check_graduation().
"""
from __future__ import annotations
import os
import sqlite3
import datetime
from pathlib import Path
from statistics import mean, stdev

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Graduation thresholds — frozen parity with Clawmpson (adjustment #6)
MIN_HISTORY_DAYS = int(os.environ.get("RIVALCLAW_MIN_HISTORY_DAYS", "7"))
WIN_RATE_THRESHOLD = 0.55
SHARPE_THRESHOLD = 1.0
MAX_DRAWDOWN_LIMIT = 0.25
ROI_7D_THRESHOLD = 0.0


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def check_graduation():
    """Evaluate graduation criteria. Returns status dict. Identical gates to Clawmpson."""
    conn = _get_conn()
    try:
        all_pnl = conn.execute(
            "SELECT date, balance, roi_pct FROM daily_pnl ORDER BY date ASC"
        ).fetchall()
        closed = conn.execute(
            "SELECT status FROM paper_trades WHERE status != 'open'"
        ).fetchall()
    finally:
        conn.close()

    history_days = len(all_pnl)
    has_min_history = history_days >= MIN_HISTORY_DAYS

    last7 = all_pnl[-7:] if len(all_pnl) >= 7 else all_pnl
    roi_7d = sum(r["roi_pct"] for r in last7 if r["roi_pct"] is not None)

    total_closed = len(closed)
    wins = sum(1 for t in closed if t["status"] == "closed_win")
    win_rate = wins / total_closed if total_closed > 0 else 0.0

    returns = [r["roi_pct"] for r in all_pnl if r["roi_pct"] is not None]
    sharpe = None
    if len(returns) >= MIN_HISTORY_DAYS:
        s = stdev(returns)
        if s > 0:
            sharpe = mean(returns) / s

    balances = [r["balance"] for r in all_pnl]
    max_dd = 0.0
    if balances:
        peak = balances[0]
        for b in balances:
            peak = max(peak, b)
            dd = (peak - b) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

    criteria = {
        "min_history": has_min_history,
        "roi_7d_positive": roi_7d > ROI_7D_THRESHOLD,
        "win_rate_55pct": win_rate > WIN_RATE_THRESHOLD,
        "sharpe_above_1": sharpe is not None and sharpe > SHARPE_THRESHOLD,
        "drawdown_below_25pct": max_dd < MAX_DRAWDOWN_LIMIT,
    }
    ready = has_min_history and all(criteria.values())

    return {
        "ready": ready, "has_minimum_history": has_min_history,
        "history_days": history_days, "roi_7d": roi_7d,
        "win_rate": win_rate, "sharpe_all_time": sharpe,
        "max_drawdown": max_dd, "criteria": criteria,
    }


def maybe_snapshot():
    """Write daily_pnl row if not written today. Returns True if snapshot written."""
    today = datetime.date.today().isoformat()
    import paper_wallet as pw
    import polymarket_feed as feed

    state = pw.get_state()
    prices = feed.get_latest_prices()
    starting = state["starting_balance"]
    balance = state["balance"]

    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM daily_pnl WHERE date = ?", (today,)
        ).fetchone()
        if existing:
            return False

        yesterday = conn.execute(
            "SELECT balance FROM daily_pnl ORDER BY date DESC LIMIT 1"
        ).fetchone()
        prev_balance = yesterday["balance"] if yesterday else starting
        roi_pct = ((balance - prev_balance) / prev_balance * 100) if prev_balance > 0 else 0.0

        conn.execute("""
            INSERT INTO daily_pnl
            (date, balance, open_positions, realized_pnl, unrealized_pnl,
             total_trades, win_rate, roi_pct)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            today, balance, state["open_positions"],
            balance - starting,  # realized approximation
            0.0,  # unrealized already in balance
            state["total_trades"], state["win_rate"], roi_pct,
        ))
        conn.commit()
    finally:
        conn.close()

    grad = check_graduation()
    status = "READY" if grad["ready"] else f"NOT READY ({sum(v for v in grad['criteria'].values())}/5)"
    print(f"[rivalclaw/grad] Day {grad['history_days']}/{MIN_HISTORY_DAYS} | "
          f"ROI7d={grad['roi_7d']:.2f}% WR={grad['win_rate']:.0%} "
          f"Sharpe={grad['sharpe_all_time'] or 0:.2f} DD={grad['max_drawdown']:.1%} | {status}")
    return True
