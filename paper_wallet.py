#!/usr/bin/env python3
"""
RivalClaw paper wallet — frozen Mirofish semantics + granular latency tracking.
Execution simulation, stop logic, balance computation, and mark-to-market
are identical to Mirofish paper_wallet.py. Added: timing fields, experiment/instance IDs.
"""
from __future__ import annotations
import os
import random
import sqlite3
import datetime
import time
from pathlib import Path
from statistics import mean, stdev
from typing import Any

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Risk parameters — frozen parity with Mirofish
STOP_LOSS_PCT = float(os.environ.get("RIVALCLAW_STOP_LOSS_PCT", "0.20"))
TAKE_PROFIT_PCT = float(os.environ.get("RIVALCLAW_TAKE_PROFIT_PCT", "0.50"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))
MIN_HISTORY_DAYS = int(os.environ.get("RIVALCLAW_MIN_HISTORY_DAYS", "7"))
STARTING_CAPITAL = float(os.environ.get("RIVALCLAW_STARTING_CAPITAL", "1000.0"))

# Execution simulation — frozen parity with Mirofish
SLIPPAGE_BPS = float(os.environ.get("RIVALCLAW_SLIPPAGE_BPS", "50"))
LATENCY_PENALTY = float(os.environ.get("RIVALCLAW_LATENCY_PENALTY", "0.002"))
FILL_RATE_MIN = float(os.environ.get("RIVALCLAW_FILL_RATE_MIN", "0.80"))
EXECUTION_SIM = os.environ.get("RIVALCLAW_EXECUTION_SIM", "1") == "1"

# Experiment tracking (adjustment #8)
EXPERIMENT_ID = os.environ.get("RIVALCLAW_EXPERIMENT_ID", "arb-bakeoff-2026-03")
INSTANCE_ID = os.environ.get("RIVALCLAW_INSTANCE_ID", "rivalclaw")


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_starting_balance():
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
        ).fetchone()
        return float(row["value"]) if row else STARTING_CAPITAL
    except sqlite3.OperationalError:
        return STARTING_CAPITAL
    finally:
        conn.close()


def _compute_balance(starting, prices):
    """Derive current balance from trade history + mark-to-market. Identical to Mirofish."""
    conn = _get_conn()
    try:
        closed = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM paper_trades WHERE status != 'open'"
        ).fetchone()["total"]
        open_trades = conn.execute(
            "SELECT market_id, direction, shares, entry_price FROM paper_trades WHERE status='open'"
        ).fetchall()
    finally:
        conn.close()

    unrealized = 0.0
    for t in open_trades:
        p = prices.get(t["market_id"], {})
        price = p.get("yes_price" if t["direction"] == "YES" else "no_price", t["entry_price"])
        unrealized += t["shares"] * (price - t["entry_price"])

    return starting + closed + unrealized


def _get_all_latest_prices():
    """Combined latest prices from all venues."""
    prices = {}
    try:
        import polymarket_feed
        prices.update(polymarket_feed.get_latest_prices())
    except Exception:
        pass
    try:
        import kalshi_feed
        prices.update(kalshi_feed.get_latest_prices())
    except Exception:
        pass
    return prices


def get_state():
    """Return full wallet state. Balance always derived, never cached. Identical to Mirofish."""
    starting = _get_starting_balance()
    prices = _get_all_latest_prices()
    balance = _compute_balance(starting, prices)

    conn = _get_conn()
    try:
        closed_trades = conn.execute(
            "SELECT status FROM paper_trades WHERE status != 'open'"
        ).fetchall()
        open_positions = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE status='open'"
        ).fetchone()["cnt"]
        daily_rows = conn.execute(
            "SELECT balance, roi_pct FROM daily_pnl ORDER BY date ASC"
        ).fetchall()
    finally:
        conn.close()

    total_closed = len(closed_trades)
    wins = sum(1 for t in closed_trades if t["status"] == "closed_win")
    win_rate = wins / total_closed if total_closed > 0 else 0.0

    returns = [r["roi_pct"] for r in daily_rows if r["roi_pct"] is not None]
    sharpe = None
    if len(returns) >= MIN_HISTORY_DAYS:
        s = stdev(returns)
        if s > 0:
            sharpe = mean(returns) / s

    balances = [r["balance"] for r in daily_rows]
    max_dd = 0.0
    if balances:
        peak = balances[0]
        for b in balances:
            peak = max(peak, b)
            dd = (peak - b) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

    return {
        "balance": balance, "starting_balance": starting,
        "open_positions": open_positions, "win_rate": win_rate,
        "sharpe_ratio": sharpe, "max_drawdown": max_dd,
        "total_trades": total_closed,
    }


def _simulate_execution(entry_price, amount_usd, shares, direction):
    """Execution simulation — identical to Mirofish."""
    ideal_price = entry_price

    slippage_pct = SLIPPAGE_BPS / 10000.0
    adjusted_price = ideal_price + ideal_price * slippage_pct
    adjusted_price = max(0.01, min(0.99, adjusted_price))

    adjusted_price = min(0.99, adjusted_price + ideal_price * LATENCY_PENALTY)

    fill_rate = random.uniform(FILL_RATE_MIN, 1.0)
    adjusted_amount = amount_usd * fill_rate
    adjusted_shares = adjusted_amount / adjusted_price if adjusted_price > 0 else 0

    sim_metadata = {
        "ideal_price": ideal_price, "adjusted_price": adjusted_price,
        "slippage_bps": SLIPPAGE_BPS, "latency_penalty": LATENCY_PENALTY,
        "fill_rate": fill_rate, "ideal_amount": amount_usd,
        "adjusted_amount": adjusted_amount,
        "price_impact_pct": ((adjusted_price - ideal_price) / ideal_price * 100)
                            if ideal_price > 0 else 0,
    }
    return adjusted_price, adjusted_amount, adjusted_shares, sim_metadata


def execute_trade(decision, cycle_started_at_ms=0.0):
    """Execute a paper trade with execution simulation. Frozen Mirofish semantics."""
    state = get_state()
    cap = state["balance"] * MAX_POSITION_PCT
    if decision.amount_usd > cap:
        return None

    entry_price = decision.entry_price
    amount_usd = decision.amount_usd
    shares = decision.shares
    sim_metadata = None

    if EXECUTION_SIM:
        entry_price, amount_usd, shares, sim_metadata = _simulate_execution(
            entry_price, amount_usd, shares, decision.direction,
        )
        if amount_usd > cap:
            return None

    trade_executed_at_ms = time.time() * 1000
    signal_to_trade_ms = trade_executed_at_ms - decision.decision_generated_at_ms if decision.decision_generated_at_ms else 0

    reasoning = getattr(decision, "reasoning", "")
    if sim_metadata:
        reasoning += (f" [sim: price {sim_metadata['ideal_price']:.3f}->"
                      f"{sim_metadata['adjusted_price']:.3f}, fill {sim_metadata['fill_rate']:.0%}]")

    ts = datetime.datetime.utcnow().isoformat()
    venue = (decision.metadata or {}).get("venue", "polymarket")
    expected_edge = (decision.metadata or {}).get("edge", 0.0) if decision.metadata else 0.0
    conn = _get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO paper_trades
            (market_id, question, direction, shares, entry_price, amount_usd,
             status, confidence, reasoning, strategy, opened_at,
             experiment_id, instance_id,
             cycle_started_at_ms, decision_generated_at_ms,
             trade_executed_at_ms, signal_to_trade_latency_ms, venue, expected_edge)
            VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            decision.market_id, decision.question, decision.direction,
            shares, entry_price, amount_usd,
            getattr(decision, "confidence", 1.0),
            reasoning, getattr(decision, "strategy", "arbitrage"), ts,
            EXPERIMENT_ID, INSTANCE_ID,
            cycle_started_at_ms, decision.decision_generated_at_ms,
            trade_executed_at_ms, signal_to_trade_ms, venue, expected_edge,
        ))
        conn.commit()
        trade_id = cur.lastrowid
    finally:
        conn.close()

    result = {
        "id": trade_id, "status": "open", "amount_usd": amount_usd,
        "market_id": decision.market_id, "direction": decision.direction,
    }
    if sim_metadata:
        result["execution_sim"] = sim_metadata
    return result


def check_stops(current_prices):
    """Check SL/TP/expiry on open positions. Identical to Mirofish."""
    now = datetime.datetime.utcnow()
    closed = []
    closed_updates = []

    conn = _get_conn()
    try:
        open_trades = conn.execute("""
            SELECT pt.*, md.end_date
            FROM paper_trades pt
            LEFT JOIN (
                SELECT market_id, end_date, MAX(fetched_at) AS latest
                FROM market_data GROUP BY market_id
            ) md ON pt.market_id = md.market_id
            WHERE pt.status = 'open'
        """).fetchall()
    finally:
        conn.close()

    for t in open_trades:
        p = current_prices.get(t["market_id"], {})
        current_price = p.get(
            "yes_price" if t["direction"] == "YES" else "no_price",
            t["entry_price"],
        )

        unrealized_pnl = t["shares"] * (current_price - t["entry_price"])
        pnl_pct = round(unrealized_pnl / t["amount_usd"], 10) if t["amount_usd"] > 0 else 0.0

        expired = False
        if t["end_date"]:
            try:
                end = datetime.datetime.fromisoformat(t["end_date"].replace("Z", ""))
                expired = now > end
            except (ValueError, AttributeError):
                pass

        should_close = (
            pnl_pct <= -STOP_LOSS_PCT or
            pnl_pct >= TAKE_PROFIT_PCT or
            expired
        )

        if should_close:
            status = "expired" if expired else ("closed_win" if unrealized_pnl >= 0 else "closed_loss")
            ts = now.isoformat()
            closed_updates.append((current_price, unrealized_pnl, status, ts, t["id"]))
            closed.append({
                "id": t["id"], "market_id": t["market_id"],
                "status": status, "exit_price": current_price, "pnl": unrealized_pnl,
            })

    if closed_updates:
        conn = _get_conn()
        try:
            conn.executemany(
                "UPDATE paper_trades SET exit_price=?, pnl=?, status=?, closed_at=? WHERE id=?",
                closed_updates,
            )
            conn.commit()
        finally:
            conn.close()

    return closed


def get_open_market_ids():
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT market_id FROM paper_trades WHERE status='open'").fetchall()
        return {r["market_id"] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
