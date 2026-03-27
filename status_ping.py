#!/usr/bin/env python3
"""
RivalClaw 15-minute status ping — quick Telegram summary.
Runs via cron every 15 minutes. Lightweight, no full report.
"""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))


def ping():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    now = datetime.utcnow()
    last_15 = (now - timedelta(minutes=15)).isoformat()

    starting = float(conn.execute(
        "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
    ).fetchone()[0])
    closed_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'").fetchone()[0]
    balance = starting + closed_pnl

    recent = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE opened_at > ?", (last_15,)
    ).fetchone()[0]
    recent_closed = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl), 0), "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) "
        "FROM paper_trades WHERE closed_at > ? AND status != 'open'", (last_15,)
    ).fetchone()

    total_open = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]
    total_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    total_wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE pnl > 0 AND status != 'open'").fetchone()[0]
    total_closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status != 'open'").fetchone()[0]
    wr = (total_wins / total_closed * 100) if total_closed > 0 else 0

    conn.close()

    closed_count = recent_closed[0] or 0
    closed_pnl_15 = recent_closed[1] or 0
    closed_wins_15 = recent_closed[2] or 0

    ts = now.strftime("%H:%M UTC")
    msg = (
        f"📊 RIVALCLAW {ts}\n"
        f"Balance: ${balance:,.0f} ({'+' if closed_pnl >= 0 else ''}${closed_pnl:,.0f})\n"
        f"Last 15m: {recent} opened, {closed_count} closed "
        f"(W:{closed_wins_15} L:{closed_count - closed_wins_15} ${closed_pnl_15:+,.0f})\n"
        f"Total: {total_trades} trades | WR: {wr:.0f}% | Open: {total_open}"
    )

    print(msg)

    try:
        import notify
        notify.send_telegram(msg, parse_mode="")
    except Exception as e:
        print(f"[status_ping] Telegram failed: {e}")


if __name__ == "__main__":
    ping()
