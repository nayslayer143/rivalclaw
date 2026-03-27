#!/usr/bin/env python3
"""
RivalClaw auto-changelog — appends to CHANGELOG.md on every significant event.
Called by the hourly tuner and daily report. Builds a continuous record
that future bot iterations can study.
"""
from __future__ import annotations
import datetime
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
CHANGELOG_PATH = Path(__file__).parent / "CHANGELOG.md"


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def append_hourly_entry():
    """Append an hourly summary to CHANGELOG.md. Called after each tuner cycle."""
    conn = _get_conn()
    now = datetime.datetime.utcnow()
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    hour_start = now.replace(minute=0, second=0).isoformat()
    prev_hour = (now - datetime.timedelta(hours=1)).replace(minute=0, second=0).isoformat()

    # This hour's trades
    hour_trades = conn.execute("""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               COALESCE(ROUND(SUM(pnl), 2), 0) as pnl
        FROM paper_trades WHERE closed_at >= ? AND closed_at < ? AND status != 'open'
    """, (prev_hour, hour_start)).fetchone()

    opened = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE opened_at >= ? AND opened_at < ?",
        (prev_hour, hour_start)).fetchone()[0]

    # Current totals
    starting = float(conn.execute(
        "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
    ).fetchone()[0])
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'").fetchone()[0]
    balance = starting + total_pnl
    total_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    open_pos = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]

    # Tuner changes this cycle
    tuner_changes = conn.execute("""
        SELECT parameter, old_value, new_value, reason
        FROM tuning_log WHERE parameter NOT IN ('none', 'cooldown')
        AND tuned_at >= ? ORDER BY id DESC LIMIT 3
    """, (prev_hour,)).fetchall()

    # Strategy scores
    try:
        import risk_engine
        scores = risk_engine.get_strategy_scores()
        regime = risk_engine.detect_regime()
    except Exception:
        scores = {}
        regime = {"regime": "unknown"}

    conn.close()

    # Build entry
    closed = hour_trades["cnt"] or 0
    wins = hour_trades["wins"] or 0
    pnl = hour_trades["pnl"] or 0
    wr = (wins / closed * 100) if closed > 0 else 0

    hour_label = now.strftime("%H:00")
    entry = f"\n**{ts}** | bal=${balance:,.0f} | opened={opened} closed={closed} W:{wins} L:{closed-wins} | pnl=${pnl:+,.0f} | WR={wr:.0f}% | open={open_pos} | regime={regime.get('regime', '?')}"

    if tuner_changes:
        for t in tuner_changes:
            entry += f"\n  - TUNED: {t['parameter']}: {t['old_value']} → {t['new_value']} ({t['reason'][:60]})"

    # Check for notable events
    if pnl > 1000:
        entry += f"\n  - 🔥 GREAT HOUR: +${pnl:,.0f}"
    elif pnl < -500:
        entry += f"\n  - ⚠️ BAD HOUR: ${pnl:,.0f}"
    if closed == 0 and opened == 0:
        entry += f"\n  - ⏸️ NO ACTIVITY"

    # Append to changelog
    # Find the right place to insert (after the day header)
    today = now.strftime("%Y-%m-%d")
    content = CHANGELOG_PATH.read_text() if CHANGELOG_PATH.exists() else ""

    day_header = f"## {today}"
    if day_header not in content:
        # New day — insert after the main header
        insert_marker = "---\n\n## "
        if insert_marker in content:
            idx = content.index(insert_marker) + 4
            day_section = f"\n## {today} — Day {_day_number(today)}\n\n### Hourly Log\n{entry}\n"
            content = content[:idx] + day_section + content[idx:]
        else:
            content += f"\n## {today} — Day {_day_number(today)}\n\n### Hourly Log\n{entry}\n"
    else:
        # Existing day — append to hourly log
        # Find end of the hourly log section for this day
        day_idx = content.index(day_header)
        # Find next ## or end of file
        next_section = content.find("\n## ", day_idx + len(day_header))
        if next_section == -1:
            content += entry + "\n"
        else:
            content = content[:next_section] + entry + "\n" + content[next_section:]

    CHANGELOG_PATH.write_text(content)
    print(f"[auto_changelog] Appended hourly entry: {ts}")


def _day_number(date_str):
    start = datetime.date(2026, 3, 24)
    current = datetime.date.fromisoformat(date_str)
    return (current - start).days + 1


if __name__ == "__main__":
    append_hourly_entry()
