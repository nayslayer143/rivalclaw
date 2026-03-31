#!/usr/bin/env python3
"""RivalClaw monitor — 5-min Telegram reports.
LIVE: from Kalshi API (settlements + balance). Pure real-money data.
PAPER: from paper_trades table. Separate data source, no mixing."""
import sqlite3
import datetime
import json
import os
import sys
import requests
from pathlib import Path

BOT_TOKEN = "8615622626:AAGwarVufm4u1TdUoKhPQUCb4-OhkJH-01A"
CHAT_ID = 1450469911
DB_PATH = Path(__file__).parent / "rivalclaw.db"
PAPER_CUTOFF = "2026-03-30T22:50"
PAPER_TARGET = 100

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("KALSHI_API_KEY_ID", "44dd8633-1448-4777-b41b-7f69a295b1e3")
os.environ.setdefault("KALSHI_API_ENV", "prod")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "/Users/nayslayer/.kalshi/live-private.pem")


def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


def _series_name(ticker):
    """Map ticker to display name."""
    t = (ticker or "").upper()
    if "BNB15M" in t: return "BNB"
    if "DOGE15M" in t: return "DOGE"
    if "BTC15M" in t: return "BTC15M"
    if "ETH15M" in t: return "ETH15M"
    if "BTCD" in t: return "BTC-D"
    if "ETHD" in t: return "ETH-D"
    if "SOLD" in t: return "SOL"
    if "BTC" in t: return "BTC"
    if "ETH" in t: return "ETH"
    if "TEMP" in t or "HIGH" in t or "LOWT" in t: return "WX"
    return "OTHER"


def _paper_mkt(question):
    """Map question text to display name."""
    if not question: return "OTHER"
    for k, v in [("BNB", "BNB"), ("DOGE", "DOGE"), ("Bitcoin", "BTC"),
                  ("Ethereum", "ETH"), ("Solana", "SOL")]:
        if k in question: return v
    if "temp" in question.lower() or "max" in question.lower(): return "WX"
    return "OTHER"


def get_live_section():
    """Build live report from Kalshi API data only."""
    import kalshi_executor

    # Balance
    try:
        bal = kalshi_executor.get_balance()
        balance_usd = bal.get("balance", 0) / 100
    except Exception:
        balance_usd = None

    # Kill switch
    env_path = Path(__file__).parent / ".env"
    kill_switch = "?"
    for line in env_path.read_text().splitlines():
        if "RIVALCLAW_LIVE_KILL_SWITCH=" in line and not line.startswith("#"):
            kill_switch = "ON" if "=1" in line else "OFF"

    # Settlements from Kalshi (real W/L) — today only
    today = datetime.date.today().isoformat()  # e.g. "2026-03-31"
    try:
        base = kalshi_executor._get_api_base()
        path = "/portfolio/settlements"
        headers = kalshi_executor._get_kalshi_auth_headers("GET", path)
        resp = requests.get(f"{base}{path}", headers=headers, params={"limit": 200}, timeout=30)
        all_settlements = resp.json().get("settlements", [])
        # Filter to today only
        settlements = [s for s in all_settlements if s.get("settled_time", "").startswith(today)]
    except Exception:
        settlements = []

    markets = {}
    wins = losses = 0
    total_pnl = 0.0
    for s in settlements:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0) / 100
        no_cost = float(s.get("no_total_cost_dollars", "0"))
        yes_cost = float(s.get("yes_total_cost_dollars", "0"))
        fee = float(s.get("fee_cost", "0"))
        cost = no_cost + yes_cost + fee
        pnl = revenue - cost

        m = _series_name(ticker)
        if m not in markets:
            markets[m] = {"w": 0, "l": 0, "pnl": 0}
        if pnl > 0:
            wins += 1
            markets[m]["w"] += 1
        elif pnl < 0:
            losses += 1
            markets[m]["l"] += 1
        markets[m]["pnl"] += pnl
        total_pnl += pnl

    # Open positions
    try:
        pos = kalshi_executor.get_positions()
        open_pos = sum(1 for p in pos.get("event_positions", []) if p.get("event_exposure", 0) != 0)
    except Exception:
        open_pos = 0

    # Resting orders
    try:
        conn = sqlite3.connect(str(kalshi_executor.DB_PATH))
        conn.row_factory = sqlite3.Row
        resting = conn.execute("SELECT COUNT(*) as cnt FROM live_orders WHERE status='resting'").fetchone()["cnt"]
        conn.close()
    except Exception:
        resting = 0

    total = wins + losses
    wr = wins / total * 100 if total else 0
    bal_str = f"${balance_usd:.2f}" if balance_usd is not None else "?"

    mkt_lines = []
    for m, d in sorted(markets.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ({mwr:.0f}%) ${d['pnl']:+.2f}")

    section = f"🔴 LIVE | {bal_str} | Kill:{kill_switch}"
    section += f"\n{wins}W/{losses}L/{open_pos}open | WR:{wr:.0f}% | ${total_pnl:+.2f}"
    if resting:
        section += f" | {resting} resting"
    if mkt_lines:
        section += "\n" + "\n".join(mkt_lines)

    return section, total_pnl


def get_paper_section():
    """Build paper report from paper_trades table only."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT status, pnl, question FROM paper_trades "
        "WHERE opened_at >= ? ORDER BY opened_at", (PAPER_CUTOFF,)
    ).fetchall()

    p_w = sum(1 for r in rows if r["status"] == "closed_win")
    p_l = sum(1 for r in rows if r["status"] == "closed_loss")
    p_o = sum(1 for r in rows if r["status"] == "open")
    p_pnl = sum(r["pnl"] for r in rows if r["pnl"] and r["status"] in ("closed_win", "closed_loss"))
    p_wr = p_w / (p_w + p_l) * 100 if (p_w + p_l) else 0

    markets = {}
    for r in rows:
        if r["status"] not in ("closed_win", "closed_loss"):
            continue
        m = _paper_mkt(r["question"])
        if m not in markets:
            markets[m] = {"w": 0, "l": 0, "pnl": 0}
        if r["status"] == "closed_win":
            markets[m]["w"] += 1
        else:
            markets[m]["l"] += 1
        markets[m]["pnl"] += r["pnl"]

    conn.close()

    mkt_lines = []
    for m, d in sorted(markets.items(), key=lambda x: x[1]["pnl"], reverse=True):
        t = d["w"] + d["l"]
        mwr = d["w"] / t * 100 if t else 0
        flag = " ⚠️" if mwr < 30 and t >= 3 else ""
        mkt_lines.append(f"  {m}: {d['w']}W/{d['l']}L ({mwr:.0f}%) ${d['pnl']:+.2f}{flag}")

    p_header = "🎯 100!" if len(rows) >= PAPER_TARGET else f"{len(rows)}/{PAPER_TARGET}"
    section = (
        f"📋 PAPER | {p_header}\n"
        f"{p_w}W/{p_l}L/{p_o}open | WR:{p_wr:.0f}% | ${p_pnl:+.2f}\n"
        + "\n".join(mkt_lines)
    )

    return section, p_pnl


def run():
    live_section, live_pnl = get_live_section()
    paper_section, paper_pnl = get_paper_section()

    msg = live_section + "\n\n" + paper_section
    send(msg)
    print(f"[monitor] Live: ${live_pnl:+.2f} | Paper: ${paper_pnl:+.2f}")


if __name__ == "__main__":
    run()
