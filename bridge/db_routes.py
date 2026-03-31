"""DB read routes — exposes rivalclaw.db tables over HTTP."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/db", tags=["db"])

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path.home() / "rivalclaw" / "rivalclaw.db"))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ── Wallet ──────────────────────────────────────────────────────────────────


@router.get("/wallet")
async def wallet(source: str = Query("paper", pattern="^(paper|live)$")):
    try:
        conn = _get_conn()
        try:
            if source == "paper":
                # Get starting balance from context table
                row = conn.execute(
                    "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
                ).fetchone()
                starting_balance = float(row["value"]) if row else 1000.0

                # Closed trades PnL
                closed = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) as total_pnl, COUNT(*) as total_trades "
                    "FROM paper_trades WHERE status IN ('closed_win','closed_loss','expired')"
                ).fetchone()
                closed_pnl = closed["total_pnl"]
                total_trades = closed["total_trades"]

                # Win rate
                wins = conn.execute(
                    "SELECT COUNT(*) as cnt FROM paper_trades WHERE status='closed_win'"
                ).fetchone()["cnt"]
                win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

                # Open positions
                open_positions = conn.execute(
                    "SELECT COUNT(*) as cnt FROM paper_trades WHERE status='open'"
                ).fetchone()["cnt"]

                balance = starting_balance + closed_pnl

                losses = total_trades - wins

                return {
                    "balance": round(balance, 2),
                    "starting_balance": starting_balance,
                    "closed_pnl": round(closed_pnl, 2),
                    "open_positions": open_positions,
                    "win_rate": round(win_rate, 2),
                    "total_trades": total_trades,
                    "wins": wins,
                    "losses": losses,
                }
            else:
                # Live mode — compute from live_orders + account_snapshots
                filled = conn.execute(
                    "SELECT COUNT(*) as total, "
                    "COALESCE(SUM(fill_count), 0) as total_fills "
                    "FROM live_orders WHERE status = 'filled'"
                ).fetchone()
                total_trades = filled["total"]

                # Latest account snapshot for balance
                snap = conn.execute(
                    "SELECT * FROM account_snapshots ORDER BY fetched_at DESC LIMIT 1"
                ).fetchone()
                balance = float(snap["balance_cents"]) / 100 if snap else 0.0

                pending = conn.execute(
                    "SELECT COUNT(*) as cnt FROM live_orders WHERE status IN ('pending','resting')"
                ).fetchone()["cnt"]

                return {
                    "balance": round(balance, 2),
                    "starting_balance": 0.0,
                    "closed_pnl": 0.0,
                    "open_positions": pending,
                    "win_rate": 0.0,
                    "total_trades": total_trades,
                }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Trades ──────────────────────────────────────────────────────────────────


@router.get("/trades")
async def trades(
    status: str = Query("all", pattern="^(open|closed|all)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    strategy: Optional[str] = Query(None),
    source: str = Query("paper", pattern="^(paper|live)$"),
):
    try:
        conn = _get_conn()
        try:
            if source == "paper":
                where_parts = []
                params: list = []

                if status == "closed":
                    where_parts.append("status IN ('closed_win','closed_loss','expired')")
                elif status == "open":
                    where_parts.append("status = 'open'")
                if strategy:
                    where_parts.append("strategy = ?")
                    params.append(strategy)

                where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                params.extend([limit, offset])

                rows = conn.execute(
                    f"SELECT * FROM paper_trades {where_clause} "
                    f"ORDER BY opened_at DESC LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
                return _rows_to_dicts(rows)
            else:
                # Live mode — query live_orders, map to similar shape
                where_parts = []
                params: list = []

                if status == "closed":
                    where_parts.append("status = 'filled'")
                elif status == "open":
                    where_parts.append("status IN ('pending','resting')")
                if strategy:
                    where_parts.append("strategy = ?")
                    params.append(strategy)

                where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                params.extend([limit, offset])

                rows = conn.execute(
                    f"SELECT id, ticker as market_id, market_question as question, "
                    f"side as direction, count as shares, "
                    f"CAST(yes_price AS REAL)/100.0 as entry_price, "
                    f"CAST(fill_price AS REAL)/100.0 as exit_price, "
                    f"0 as amount_usd, 0 as pnl, status, strategy, "
                    f"'kalshi' as venue, submitted_at as opened_at, filled_at as closed_at "
                    f"FROM live_orders {where_clause} "
                    f"ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
                return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Live Orders ─────────────────────────────────────────────────────────────


@router.get("/live-orders")
async def live_orders(
    mode: str = Query("all", pattern="^(shadow|live|all)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        conn = _get_conn()
        try:
            if mode != "all":
                rows = conn.execute(
                    "SELECT * FROM live_orders WHERE mode = ? "
                    "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                    (mode, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_orders ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Reconciliation ──────────────────────────────────────────────────────────


@router.get("/reconciliation")
async def reconciliation():
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM live_reconciliation ORDER BY reconciled_at DESC LIMIT 50"
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Strategies ──────────────────────────────────────────────────────────────


@router.get("/strategies")
async def strategies(source: str = Query("paper", pattern="^(paper|live)$")):
    try:
        conn = _get_conn()
        try:
            if source == "paper":
                rows = conn.execute("""
                    SELECT
                        strategy,
                        COUNT(*) as count,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                        ROUND(AVG(pnl), 4) as avg_pnl,
                        ROUND(SUM(pnl), 4) as total_pnl,
                        ROUND(SUM(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) / COUNT(*) * 100, 2) as win_rate
                    FROM paper_trades
                    WHERE status IN ('closed_win','closed_loss','expired')
                    GROUP BY strategy
                    ORDER BY total_pnl DESC
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT
                        strategy,
                        COUNT(*) as count,
                        0 as wins,
                        0 as losses,
                        0 as avg_pnl,
                        0 as total_pnl,
                        0 as win_rate
                    FROM live_orders
                    WHERE status = 'filled' AND strategy IS NOT NULL
                    GROUP BY strategy
                    ORDER BY count DESC
                """).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Cycles ──────────────────────────────────────────────────────────────────


@router.get("/cycles")
async def cycles(limit: int = Query(50, ge=1, le=500)):
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM cycle_metrics ORDER BY cycle_started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Daily PnL ───────────────────────────────────────────────────────────────


@router.get("/daily-pnl")
async def daily_pnl(source: str = Query("paper", pattern="^(paper|live)$")):
    try:
        conn = _get_conn()
        try:
            if source == "paper":
                rows = conn.execute(
                    "SELECT * FROM daily_pnl ORDER BY date ASC"
                ).fetchall()
            else:
                # Live mode — aggregate from live_orders by date
                rows = conn.execute("""
                    SELECT
                        DATE(submitted_at) as date,
                        0 as balance,
                        0 as roi_pct,
                        0 as win_rate,
                        0 as realized_pnl,
                        COUNT(*) as total_trades,
                        0 as open_positions
                    FROM live_orders
                    WHERE status = 'filled'
                    GROUP BY DATE(submitted_at)
                    ORDER BY date ASC
                """).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Errors ──────────────────────────────────────────────────────────────────


@router.get("/errors")
async def errors(limit: int = Query(100, ge=1, le=1000)):
    try:
        conn = _get_conn()
        try:
            # Check if event_log table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'"
            ).fetchone()
            if not table_check:
                return []
            rows = conn.execute(
                "SELECT * FROM event_log ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Account Snapshots ───────────────────────────────────────────────────────


@router.get("/account-snapshots")
async def account_snapshots():
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM account_snapshots ORDER BY fetched_at DESC LIMIT 50"
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Market Data ─────────────────────────────────────────────────────────────


@router.get("/market-data")
async def market_data(
    venue: str = Query("kalshi"),
    limit: int = Query(50, ge=1, le=500),
):
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM market_data WHERE venue = ? "
                "ORDER BY fetched_at DESC LIMIT ?",
                (venue, limit),
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


# ── Daily Detail ───────────────────────────────────────────────────────────


@router.get("/daily-detail")
async def daily_detail(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    source: str = Query("paper", pattern="^(paper|live)$"),
):
    try:
        conn = _get_conn()
        try:
            if source == "paper":
                trades = conn.execute(
                    "SELECT * FROM paper_trades WHERE status IN ('closed_win','closed_loss','expired') AND DATE(closed_at) = ? "
                    "ORDER BY closed_at DESC",
                    (date,),
                ).fetchall()

                strategies = conn.execute(
                    "SELECT strategy, COUNT(*) as trade_count, "
                    "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
                    "ROUND(SUM(pnl), 4) as total_pnl, "
                    "ROUND(SUM(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) / COUNT(*) * 100, 2) as win_rate "
                    "FROM paper_trades WHERE status IN ('closed_win','closed_loss','expired') AND DATE(closed_at) = ? "
                    "GROUP BY strategy ORDER BY total_pnl DESC",
                    (date,),
                ).fetchall()
            else:
                trades = conn.execute(
                    "SELECT id, ticker as market_id, market_question as question, "
                    "side as direction, count as shares, "
                    "CAST(yes_price AS REAL)/100.0 as entry_price, "
                    "CAST(fill_price AS REAL)/100.0 as exit_price, "
                    "0 as amount_usd, 0 as pnl, status, strategy, "
                    "'kalshi' as venue, submitted_at as opened_at, filled_at as closed_at "
                    "FROM live_orders WHERE status = 'filled' AND DATE(submitted_at) = ? "
                    "ORDER BY submitted_at DESC",
                    (date,),
                ).fetchall()

                strategies = conn.execute(
                    "SELECT strategy, COUNT(*) as trade_count, "
                    "0 as wins, 0 as losses, 0 as total_pnl, 0 as win_rate "
                    "FROM live_orders WHERE status = 'filled' AND DATE(submitted_at) = ? "
                    "GROUP BY strategy ORDER BY trade_count DESC",
                    (date,),
                ).fetchall()

            return {
                "trades": _rows_to_dicts(trades),
                "strategies": _rows_to_dicts(strategies),
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}
