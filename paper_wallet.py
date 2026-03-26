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

import event_logger as elog

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Risk parameters — frozen parity with Mirofish
STOP_LOSS_PCT = float(os.environ.get("RIVALCLAW_STOP_LOSS_PCT", "0.20"))
TAKE_PROFIT_PCT = float(os.environ.get("RIVALCLAW_TAKE_PROFIT_PCT", "0.50"))
MAX_POSITION_PCT = float(os.environ.get("RIVALCLAW_MAX_POSITION_PCT", "0.10"))
MIN_HISTORY_DAYS = int(os.environ.get("RIVALCLAW_MIN_HISTORY_DAYS", "7"))
STARTING_CAPITAL = float(os.environ.get("RIVALCLAW_STARTING_CAPITAL", "1000.0"))

# Hard cap: no single trade can exceed this regardless of balance growth
# Prevents paper-trading fantasy where $1K starting capital balloons to $440K
# and suddenly allows $44K single bets
MAX_TRADE_USD = float(os.environ.get("RIVALCLAW_MAX_TRADE_USD", "500.0"))

# Execution simulation — frozen parity with Mirofish
SLIPPAGE_BPS = float(os.environ.get("RIVALCLAW_SLIPPAGE_BPS", "50"))
LATENCY_PENALTY = float(os.environ.get("RIVALCLAW_LATENCY_PENALTY", "0.002"))
FILL_RATE_MIN = float(os.environ.get("RIVALCLAW_FILL_RATE_MIN", "0.80"))
EXECUTION_SIM = os.environ.get("RIVALCLAW_EXECUTION_SIM", "1") == "1"

# Venue fee rates — applied to notional * min(price, 1-price) per contract
POLYMARKET_FEE_RATE = float(os.environ.get("RIVALCLAW_POLYMARKET_FEE", "0.02"))
KALSHI_TAKER_FEE_RATE = float(os.environ.get("RIVALCLAW_KALSHI_FEE", "0.07"))

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
        price = p.get("yes_price" if t["direction"] == "YES" else "no_price")
        if price is None:
            price = t["entry_price"]  # No price data → assume flat (don't crash)
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


def _simulate_execution(entry_price, amount_usd, shares, direction, venue="polymarket"):
    """Execution simulation with venue-aware fee deduction."""
    ideal_price = entry_price

    slippage_pct = SLIPPAGE_BPS / 10000.0
    adjusted_price = ideal_price + ideal_price * slippage_pct
    adjusted_price = max(0.01, min(0.99, adjusted_price))

    adjusted_price = min(0.99, adjusted_price + ideal_price * LATENCY_PENALTY)

    fill_rate = random.uniform(FILL_RATE_MIN, 1.0)
    adjusted_amount = amount_usd * fill_rate

    # Deduct venue fees from the fill amount
    fee_rate = KALSHI_TAKER_FEE_RATE if venue == "kalshi" else POLYMARKET_FEE_RATE
    entry_fee = adjusted_amount * fee_rate * min(adjusted_price, 1.0 - adjusted_price)
    adjusted_amount -= entry_fee
    adjusted_shares = adjusted_amount / adjusted_price if adjusted_price > 0 else 0

    sim_metadata = {
        "ideal_price": ideal_price, "adjusted_price": adjusted_price,
        "slippage_bps": SLIPPAGE_BPS, "latency_penalty": LATENCY_PENALTY,
        "fill_rate": fill_rate, "ideal_amount": amount_usd,
        "adjusted_amount": adjusted_amount, "entry_fee": entry_fee,
        "price_impact_pct": ((adjusted_price - ideal_price) / ideal_price * 100)
                            if ideal_price > 0 else 0,
    }
    return adjusted_price, adjusted_amount, adjusted_shares, sim_metadata


def execute_trade(decision, cycle_started_at_ms=0.0):
    """Execute a paper trade with execution simulation. Frozen Mirofish semantics."""
    state = get_state()
    pct_cap = state["balance"] * MAX_POSITION_PCT
    cap = min(pct_cap, MAX_TRADE_USD)  # Hard ceiling prevents paper-trading fantasy
    if decision.amount_usd > cap:
        print(f"[rivalclaw/wallet] REJECT {decision.market_id[:30]}: amount=${decision.amount_usd:.1f} > cap=${cap:.1f} (hard max=${MAX_TRADE_USD:.0f}, bal=${state['balance']:.1f})")
        return None

    entry_price = decision.entry_price
    amount_usd = decision.amount_usd
    shares = decision.shares
    sim_metadata = None

    venue = (decision.metadata or {}).get("venue", "polymarket")

    if EXECUTION_SIM:
        entry_price, amount_usd, shares, sim_metadata = _simulate_execution(
            entry_price, amount_usd, shares, decision.direction, venue=venue,
        )
        if amount_usd > cap:
            return None

    entry_fee = sim_metadata.get("entry_fee", 0.0) if sim_metadata else 0.0

    trade_executed_at_ms = time.time() * 1000
    signal_to_trade_ms = trade_executed_at_ms - decision.decision_generated_at_ms if decision.decision_generated_at_ms else 0

    reasoning = getattr(decision, "reasoning", "")
    if sim_metadata:
        reasoning += (f" [sim: price {sim_metadata['ideal_price']:.3f}->"
                      f"{sim_metadata['adjusted_price']:.3f}, fill {sim_metadata['fill_rate']:.0%}"
                      f", fee ${entry_fee:.2f}]")

    ts = datetime.datetime.utcnow().isoformat()
    expected_edge = (decision.metadata or {}).get("edge", 0.0) if decision.metadata else 0.0
    conn = _get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO paper_trades
            (market_id, question, direction, shares, entry_price, amount_usd,
             status, confidence, reasoning, strategy, opened_at,
             experiment_id, instance_id,
             cycle_started_at_ms, decision_generated_at_ms,
             trade_executed_at_ms, signal_to_trade_latency_ms, venue, expected_edge,
             entry_fee)
            VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            decision.market_id, decision.question, decision.direction,
            shares, entry_price, amount_usd,
            getattr(decision, "confidence", 1.0),
            reasoning, getattr(decision, "strategy", "arbitrage"), ts,
            EXPERIMENT_ID, INSTANCE_ID,
            cycle_started_at_ms, decision.decision_generated_at_ms,
            trade_executed_at_ms, signal_to_trade_ms, venue, expected_edge,
            entry_fee,
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

    # Strategy Lab: emit trade event
    elog.trade(
        trade_id=trade_id, market_id=decision.market_id,
        strategy=getattr(decision, "strategy", "arbitrage"),
        direction=decision.direction, size=amount_usd, price=entry_price,
        fees=entry_fee,
        latency_ms=signal_to_trade_ms,
        slippage_estimate=sim_metadata.get("slippage_bps", 0) / 10000 if sim_metadata else 0,
    )
    return result


def check_stops(current_prices):
    """
    Check SL/TP/expiry on open positions.
    KEY INSIGHT: Stop-losses are DISABLED for fast-resolving contracts (<60 min).
    On 15-min contracts, stop-losses kill winners and can't save losers (price gaps
    between cycles). Let them ride to expiry — the EV is what matters, not the path.
    """
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
        current_price = p.get("yes_price" if t["direction"] == "YES" else "no_price")
        if current_price is None:
            current_price = t["entry_price"]  # No price data → assume flat

        unrealized_pnl = t["shares"] * (current_price - t["entry_price"])
        pnl_pct = round(unrealized_pnl / t["amount_usd"], 10) if t["amount_usd"] > 0 else 0.0

        expired = False
        minutes_to_expiry = float('inf')
        if t["end_date"]:
            try:
                end = datetime.datetime.fromisoformat(t["end_date"].replace("Z", ""))
                expired = now > end
                minutes_to_expiry = (end - now).total_seconds() / 60.0
            except (ValueError, AttributeError):
                pass

        # Fast-resolving contracts (<60 min): NO stop-loss. Let them expire.
        # Stop-losses are counterproductive on fast markets — they kill winners
        # and can't limit losers (price gaps between 2-min cycles).
        is_fast = minutes_to_expiry < 60

        if is_fast:
            should_close = expired
        else:
            should_close = (
                pnl_pct <= -STOP_LOSS_PCT or
                pnl_pct >= TAKE_PROFIT_PCT or
                expired
            )

        if should_close:
            # Compute exit fee (same formula as entry)
            venue = t["venue"] if t["venue"] else "polymarket"
            fee_rate = KALSHI_TAKER_FEE_RATE if venue == "kalshi" else POLYMARKET_FEE_RATE
            exit_notional = t["shares"] * current_price if current_price > 0 else 0
            exit_fee = exit_notional * fee_rate * min(current_price, 1.0 - current_price)
            entry_fee = t["entry_fee"] if t["entry_fee"] else 0.0
            total_fees = entry_fee + exit_fee

            # Net PnL after fees
            pnl_gross = unrealized_pnl
            pnl_net = pnl_gross - total_fees

            status = "expired" if expired else ("closed_win" if pnl_net >= 0 else "closed_loss")
            ts = now.isoformat()
            closed_updates.append((current_price, pnl_net, status, ts, exit_fee, t["id"]))
            closed.append({
                "id": t["id"], "market_id": t["market_id"],
                "status": status, "exit_price": current_price, "pnl": pnl_net,
            })
            # Strategy Lab: emit outcome event
            opened_at = t["opened_at"] if t["opened_at"] else ts
            try:
                hold_h = (now - datetime.datetime.fromisoformat(opened_at)).total_seconds() / 3600
            except (ValueError, TypeError):
                hold_h = 0
            elog.outcome(
                trade_id=t["id"], pnl_gross=pnl_gross, pnl_net=pnl_net,
                fees_paid=total_fees, hold_duration_hours=hold_h,
                resolved_price=current_price, entry_price=t["entry_price"],
                was_correct=pnl_net >= 0,
                strategy_version=t["strategy"] if t["strategy"] else "",
            )

    if closed_updates:
        conn = _get_conn()
        try:
            conn.executemany(
                "UPDATE paper_trades SET exit_price=?, pnl=?, status=?, closed_at=?, exit_fee=? WHERE id=?",
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
