#!/usr/bin/env python3
"""Paper trade monitor — cron job, sends Telegram update each run.
Watches for market-level performance and milestones toward 100 trades."""
import sqlite3
import json
import requests
from pathlib import Path

BOT_TOKEN = "8615622626:AAGwarVufm4u1TdUoKhPQUCb4-OhkJH-01A"
CHAT_ID = 1450469911
DB_PATH = Path(__file__).parent / "rivalclaw.db"
STATE_PATH = Path(__file__).parent / ".monitor_state.json"
CUTOFF = "2026-03-30T22:50"
TARGET = 100


def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_total": 0, "last_closed": 0, "doge_verdict_sent": False, "xrp_removed": True}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state))


def run():
    state = load_state()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT status, pnl, question, direction, entry_price FROM paper_trades "
        "WHERE opened_at >= ? ORDER BY opened_at", (CUTOFF,)
    ).fetchall()

    wins = sum(1 for r in rows if r["status"] == "closed_win")
    losses = sum(1 for r in rows if r["status"] == "closed_loss")
    opens = sum(1 for r in rows if r["status"] == "open")
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"] and r["status"] in ("closed_win", "closed_loss"))
    total_closed = wins + losses
    wr = wins / total_closed * 100 if total_closed else 0

    # Market breakdown
    def mkt(q):
        if not q: return "OTHER"
        for k, v in [("BNB", "BNB"), ("DOGE", "DOGE"), ("Bitcoin", "BTC"),
                      ("Ethereum", "ETH"), ("Ripple", "XRP"), ("Solana", "SOL")]:
            if k in q: return v
        if "temp" in q.lower(): return "WX"
        return "OTHER"

    markets = {}
    for r in rows:
        if r["status"] not in ("closed_win", "closed_loss"): continue
        m = mkt(r["question"])
        if m not in markets: markets[m] = {"w": 0, "l": 0, "pnl": 0}
        if r["status"] == "closed_win": markets[m]["w"] += 1
        else: markets[m]["l"] += 1
        markets[m]["pnl"] += r["pnl"]

    conn.close()

    # Always send — user wants consistent 5-min updates

    # Build message
    mkt_lines = []
    for m, d in sorted(markets.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        flag = " ⚠️" if mwr < 30 and t >= 3 else ""
        mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ({mwr:.0f}%) ${d['pnl']:+.2f}{flag}")

    header = "🎯 100 TRADES!" if len(rows) >= TARGET else f"📊 {len(rows)}/{TARGET}"
    msg = (
        f"{header}\n"
        f"{wins}W/{losses}L/{opens}open | WR:{wr:.0f}% | ${total_pnl:+.2f}\n\n"
        + "\n".join(mkt_lines)
    )

    send(msg)
    state["last_total"] = len(rows)
    state["last_closed"] = total_closed
    save_state(state)
    print(f"[monitor] Sent update: {len(rows)}/100 | {wins}W/{losses}L | ${total_pnl:+.2f}")


if __name__ == "__main__":
    run()
