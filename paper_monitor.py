#!/usr/bin/env python3
"""Paper trade monitor — sends Telegram updates every 15 min.
Watches for DOGE verdict and trade milestones toward 100."""
import sqlite3
import time
import requests
import os
from pathlib import Path

BOT_TOKEN = "8615622626:AAGwarVufm4u1TdUoKhPQUCb4-OhkJH-01A"
CHAT_ID = 1450469911
DB_PATH = Path(__file__).parent / "rivalclaw.db"
CUTOFF = "2026-03-30T22:50"
TARGET = 100
DOGE_THRESHOLD = 10
CHECK_INTERVAL = 900  # 15 min

_last_total = 0
_doge_verdict_sent = False


def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


def check():
    global _last_total, _doge_verdict_sent
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT status, pnl, question FROM paper_trades "
        "WHERE opened_at >= ? ORDER BY opened_at", (CUTOFF,)
    ).fetchall()

    wins = sum(1 for r in rows if r["status"] == "closed_win")
    losses = sum(1 for r in rows if r["status"] == "closed_loss")
    opens = sum(1 for r in rows if r["status"] == "open")
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"] and r["status"] in ("closed_win", "closed_loss"))
    total = wins + losses
    wr = wins / total * 100 if total else 0

    # DOGE stats
    doge = [r for r in rows if "DOGE" in (r["question"] or "") and r["status"] in ("closed_win", "closed_loss")]
    dw = sum(1 for r in doge if r["status"] == "closed_win")
    dl = sum(1 for r in doge if r["status"] == "closed_loss")
    dpnl = sum(r["pnl"] for r in doge)

    # Market breakdown
    def market_name(q):
        if not q: return "OTHER"
        for k in ["BNB", "DOGE", "Bitcoin", "Ethereum", "Ripple", "Solana"]:
            if k in q:
                return k.upper() if k != "Bitcoin" else "BTC"
        if "temp" in q.lower(): return "WEATHER"
        return "OTHER"

    markets = {}
    for r in rows:
        if r["status"] not in ("closed_win", "closed_loss"):
            continue
        m = market_name(r["question"])
        if m not in markets:
            markets[m] = {"w": 0, "l": 0, "pnl": 0}
        if r["status"] == "closed_win":
            markets[m]["w"] += 1
        else:
            markets[m]["l"] += 1
        markets[m]["pnl"] += r["pnl"]

    conn.close()

    # DOGE verdict
    if dw + dl >= DOGE_THRESHOLD and not _doge_verdict_sent:
        _doge_verdict_sent = True
        dwr = dw / (dw + dl) * 100
        if dwr < 40:
            send(
                f"🚨 DOGE VERDICT: {dw}W/{dl}L ({dwr:.0f}%) = ${dpnl:+.2f}\n"
                f"RECOMMENDATION: REMOVE or REVERSE\n"
                f"A reversal (YES instead of NO) would have won {dl} of {dw+dl} trades."
            )
        else:
            send(f"✅ DOGE passed review: {dw}W/{dl}L ({dwr:.0f}%) — keeping")

    # Milestone alerts (every 10 trades)
    new_milestone = (len(rows) // 10) > (_last_total // 10)

    # Build market table
    mkt_lines = []
    for m, d in sorted(markets.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ({mwr:.0f}%) ${d['pnl']:+.2f}")

    if new_milestone or len(rows) >= TARGET:
        msg = (
            f"📊 Paper Test: {len(rows)}/100\n"
            f"{wins}W/{losses}L/{opens} open | WR: {wr:.0f}% | PnL: ${total_pnl:+.2f}\n"
            f"DOGE: {dw}W/{dl}L ({dw+dl}/{DOGE_THRESHOLD})\n\n"
            + "\n".join(mkt_lines)
        )
        if len(rows) >= TARGET:
            msg = "🎯 100 TRADES REACHED!\n\n" + msg
        send(msg)

    _last_total = len(rows)

    # Print to log too
    print(f"[monitor] {len(rows)}/100 | {wins}W/{losses}L/{opens}open | ${total_pnl:+.2f} | DOGE {dw}W/{dl}L")


if __name__ == "__main__":
    print("[monitor] Starting paper trade monitor")
    send("🦞 Paper monitor started. Checking every 15 min.")
    while True:
        try:
            check()
        except Exception as e:
            print(f"[monitor] Error: {e}")
        time.sleep(CHECK_INTERVAL)
