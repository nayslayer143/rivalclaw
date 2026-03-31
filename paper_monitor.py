#!/usr/bin/env python3
"""RivalClaw monitor — 5-min Telegram reports.
Live trades section on top, paper trades below."""
import sqlite3
import json
import os
import requests
from pathlib import Path

BOT_TOKEN = "8615622626:AAGwarVufm4u1TdUoKhPQUCb4-OhkJH-01A"
CHAT_ID = 1450469911
DB_PATH = Path(__file__).parent / "rivalclaw.db"
STATE_PATH = Path(__file__).parent / ".monitor_state.json"
PAPER_CUTOFF = "2026-03-30T22:50"
PAPER_TARGET = 100


def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state))


def _mkt_name(q):
    if not q:
        return "OTHER"
    for k, v in [("BNB", "BNB"), ("DOGE", "DOGE"), ("Bitcoin", "BTC"),
                  ("Ethereum", "ETH"), ("Ripple", "XRP"), ("Solana", "SOL")]:
        if k in q:
            return v
    if "temp" in q.lower() or "max" in q.lower():
        return "WX"
    return "OTHER"


def _market_breakdown(rows):
    markets = {}
    for r in rows:
        if r["status"] not in ("closed_win", "closed_loss"):
            continue
        m = _mkt_name(r["question"])
        if m not in markets:
            markets[m] = {"w": 0, "l": 0, "pnl": 0}
        if r["status"] == "closed_win":
            markets[m]["w"] += 1
        else:
            markets[m]["l"] += 1
        markets[m]["pnl"] += r["pnl"]
    return markets


def run():
    state = load_state()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # =====================================================
    # LIVE SECTION
    # =====================================================
    # Get Kalshi balance
    try:
        os.environ.setdefault("KALSHI_API_KEY_ID", "44dd8633-1448-4777-b41b-7f69a295b1e3")
        os.environ.setdefault("KALSHI_API_ENV", "prod")
        os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "/Users/nayslayer/.kalshi/live-private.pem")
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        import kalshi_executor
        bal = kalshi_executor.get_balance()
        balance_usd = bal.get("balance", 0) / 100
    except Exception:
        balance_usd = None

    # Live trades today (from paper_trades with live execution)
    live_today = conn.execute(
        "SELECT status, pnl, question FROM paper_trades "
        "WHERE venue='kalshi' AND trade_executed_at_ms IS NOT NULL "
        "AND opened_at >= date('now') ORDER BY opened_at"
    ).fetchall()
    live_w = sum(1 for r in live_today if r["status"] == "closed_win")
    live_l = sum(1 for r in live_today if r["status"] == "closed_loss")
    live_o = sum(1 for r in live_today if r["status"] == "open")
    live_pnl = sum(r["pnl"] for r in live_today if r["pnl"] and r["status"] in ("closed_win", "closed_loss"))
    live_wr = live_w / (live_w + live_l) * 100 if (live_w + live_l) else 0

    # Live market breakdown
    live_mkts = _market_breakdown(live_today)

    # Kill switch state
    env_path = Path(__file__).parent / ".env"
    kill_switch = "?"
    for line in env_path.read_text().splitlines():
        if "RIVALCLAW_LIVE_KILL_SWITCH=" in line and not line.startswith("#"):
            kill_switch = "ON" if "=1" in line else "OFF"

    # Recent fills from live_orders
    recent_fills = conn.execute(
        "SELECT ticker, status, side FROM live_orders "
        "WHERE submitted_at >= datetime('now', '-10 minutes') AND status='filled' "
        "ORDER BY rowid DESC LIMIT 3"
    ).fetchall()
    recent_str = ""
    if recent_fills:
        fills_list = [f"{r['ticker'][:20]}" for r in recent_fills]
        recent_str = f"\nRecent: {', '.join(fills_list)}"

    # Resting orders
    resting = conn.execute(
        "SELECT COUNT(*) as cnt FROM live_orders WHERE status='resting'"
    ).fetchone()["cnt"]

    # Build live section
    bal_str = f"${balance_usd:.2f}" if balance_usd is not None else "?"
    live_mkt_lines = []
    for m, d in sorted(live_mkts.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        live_mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ${d['pnl']:+.2f}")

    live_section = (
        f"🔴 LIVE | {bal_str} | Kill:{kill_switch}\n"
        f"{live_w}W/{live_l}L/{live_o}open | WR:{live_wr:.0f}% | ${live_pnl:+.2f}"
    )
    if resting:
        live_section += f" | {resting} resting"
    if live_mkt_lines:
        live_section += "\n" + "\n".join(live_mkt_lines)
    if recent_str:
        live_section += recent_str

    # =====================================================
    # PAPER SECTION
    # =====================================================
    paper_rows = conn.execute(
        "SELECT status, pnl, question FROM paper_trades "
        "WHERE opened_at >= ? ORDER BY opened_at", (PAPER_CUTOFF,)
    ).fetchall()

    p_w = sum(1 for r in paper_rows if r["status"] == "closed_win")
    p_l = sum(1 for r in paper_rows if r["status"] == "closed_loss")
    p_o = sum(1 for r in paper_rows if r["status"] == "open")
    p_pnl = sum(r["pnl"] for r in paper_rows if r["pnl"] and r["status"] in ("closed_win", "closed_loss"))
    p_wr = p_w / (p_w + p_l) * 100 if (p_w + p_l) else 0

    paper_mkts = _market_breakdown(paper_rows)
    paper_mkt_lines = []
    for m, d in sorted(paper_mkts.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        flag = " ⚠️" if mwr < 30 and t >= 3 else ""
        paper_mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ({mwr:.0f}%) ${d['pnl']:+.2f}{flag}")

    p_header = "🎯 100!" if len(paper_rows) >= PAPER_TARGET else f"{len(paper_rows)}/{PAPER_TARGET}"
    paper_section = (
        f"\n📋 PAPER | {p_header}\n"
        f"{p_w}W/{p_l}L/{p_o}open | WR:{p_wr:.0f}% | ${p_pnl:+.2f}\n"
        + "\n".join(paper_mkt_lines)
    )

    conn.close()

    # =====================================================
    # SEND
    # =====================================================
    msg = live_section + "\n" + paper_section
    send(msg)
    save_state({"last_total": len(paper_rows), "last_closed": p_w + p_l})
    print(f"[monitor] Live: {live_w}W/{live_l}L ${live_pnl:+.2f} | Paper: {len(paper_rows)}/{PAPER_TARGET} ${p_pnl:+.2f}")


if __name__ == "__main__":
    run()
