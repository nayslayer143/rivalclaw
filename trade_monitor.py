#!/usr/bin/env python3
"""Trade monitor — detects new live orders, fills, and resolutions and prints commentary."""
import os, sqlite3, json
from pathlib import Path

STATE_FILE = Path(__file__).parent / ".monitor_state.json"
DB_PATH = Path(__file__).parent / "rivalclaw.db"

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_order_ids": [], "seen_order_statuses": {}, "seen_resolved_ids": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state))

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def run():
    state = load_state()
    conn = get_conn()
    lines = []

    # --- Check live orders for new submissions and status changes ---
    orders = conn.execute(
        "SELECT * FROM live_orders ORDER BY id ASC"
    ).fetchall()

    for o in orders:
        oid = str(o["id"])
        status = o["status"]
        ticker = o["ticker"]
        side = o["side"].upper()
        count = int(o["count"]) if o["count"] else 0
        yes_price = o["yes_price"] or 0
        price = yes_price/100 if side == "YES" else (100 - yes_price)/100
        cost = round(count * price, 2)
        strategy = o["strategy"] or "?"

        # New order
        if oid not in state["seen_order_ids"]:
            state["seen_order_ids"].append(oid)
            state["seen_order_statuses"][oid] = status
            if status == "filled":
                lines.append(f"🟢 LIVE FILL: {side} {count}x {ticker} @ ${price:.2f} (cost ${cost:.2f}) [{strategy}]")
            elif status == "resting":
                lines.append(f"📤 ORDER PLACED: {side} {count}x {ticker} @ ${price:.2f} (cost ${cost:.2f}) [{strategy}]")
            elif status == "rejected":
                reason = o["rejection_reason"] or "unknown"
                lines.append(f"❌ REJECTED: {ticker} — {reason}")
            continue

        # Status change on existing order
        prev_status = state["seen_order_statuses"].get(oid)
        if prev_status != status:
            state["seen_order_statuses"][oid] = status
            fill_price = o["fill_price"]
            fill_count = o["fill_count"]
            if status == "filled":
                fp = fill_price/100 if fill_price else price
                lines.append(f"✅ FILLED: {side} {count}x {ticker} @ ${fp:.2f} (cost ${cost:.2f}) [{strategy}]")
            elif status == "cancelled":
                lines.append(f"🔁 EXPIRED/CANCELLED: {side} {count}x {ticker} — refunded ${cost:.2f}")

    # --- Check paper_trades for live-linked resolutions ---
    # Look at recently closed trades that correspond to live orders
    live_tickers = {o["ticker"] for o in orders if o["status"] == "filled"}
    if live_tickers:
        placeholders = ",".join("?" * len(live_tickers))
        resolved = conn.execute(
            f"""SELECT rowid, market_id, direction, entry_price, exit_price, pnl,
                       status, strategy, reasoning, closed_at
                FROM paper_trades
                WHERE status IN ('closed_win','closed_loss','expired')
                AND market_id IN ({placeholders})
                ORDER BY closed_at DESC LIMIT 50""",
            list(live_tickers)
        ).fetchall()

        for r in resolved:
            rid = str(r["rowid"])
            if rid in state["seen_resolved_ids"]:
                continue
            state["seen_resolved_ids"].append(rid)
            pnl = r["pnl"]
            sign = "+" if pnl >= 0 else ""
            result_emoji = "✅ WIN" if r["status"] == "closed_win" else ("⏰ EXPIRED" if r["status"] == "expired" else "❌ LOSS")
            lines.append(
                f"{result_emoji}: {r['direction']} on {r['market_id']} "
                f"entry={r['entry_price']:.3f} exit={r['exit_price'] or 0:.3f} "
                f"PnL={sign}${pnl:.4f} [{r['strategy']}]"
            )
            # Analysis line
            if r["reasoning"]:
                lines.append(f"   Analysis: {r['reasoning'][:120]}")

    conn.close()
    save_state(state)

    for line in lines:
        print(line)

if __name__ == "__main__":
    run()
