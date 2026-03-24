#!/usr/bin/env python3
"""
RivalClaw simulator — cron orchestrator with per-cycle metrics.
Mirofish run_loop() shape: fetch -> analyze -> trade -> stops -> snapshot.
Stripped to arb-only organs. Adds granular timing instrumentation.
"""
from __future__ import annotations
import os
import sys
import sqlite3
import time
import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
EXPERIMENT_ID = os.environ.get("RIVALCLAW_EXPERIMENT_ID", "arb-bakeoff-2026-03")
INSTANCE_ID = os.environ.get("RIVALCLAW_INSTANCE_ID", "rivalclaw")


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT,
    yes_price REAL,
    no_price REAL,
    volume REAL,
    end_date TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_data_market_time
    ON market_data(market_id, fetched_at);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    direction TEXT NOT NULL,
    shares REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    amount_usd REAL NOT NULL,
    pnl REAL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    reasoning TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT 'arbitrage',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    experiment_id TEXT,
    instance_id TEXT,
    cycle_started_at_ms REAL,
    decision_generated_at_ms REAL,
    trade_executed_at_ms REAL,
    signal_to_trade_latency_ms REAL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    balance REAL NOT NULL,
    open_positions INTEGER,
    realized_pnl REAL,
    unrealized_pnl REAL,
    total_trades INTEGER,
    win_rate REAL,
    roi_pct REAL
);

CREATE TABLE IF NOT EXISTS context (
    chat_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS cycle_metrics (
    id INTEGER PRIMARY KEY,
    experiment_id TEXT,
    instance_id TEXT,
    cycle_started_at TEXT,
    markets_fetched INTEGER,
    opportunities_detected INTEGER,
    opportunities_qualified INTEGER,
    trades_executed INTEGER,
    stops_closed INTEGER,
    fetch_ms REAL,
    analyze_ms REAL,
    wallet_ms REAL,
    total_cycle_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_cycle_metrics_time ON cycle_metrics(cycle_started_at);

INSERT OR IGNORE INTO context (chat_id, key, value)
VALUES ('rivalclaw', 'starting_balance', '1000.00');
"""


def migrate():
    with _get_conn() as conn:
        conn.executescript(MIGRATION_SQL)
    print(f"[rivalclaw] Migration complete. DB: {DB_PATH}")


def run_loop():
    """Full simulation loop with per-cycle timing instrumentation."""
    sys.path.insert(0, str(Path(__file__).parent))
    import polymarket_feed as feed
    import paper_wallet as wallet
    import trading_brain as brain
    import graduation as grad

    cycle_started_at_ms = time.time() * 1000
    cycle_started_iso = datetime.datetime.utcnow().isoformat()
    print(f"[rivalclaw] Run loop starting — {cycle_started_iso}")

    # 1. Fetch market data (timed)
    t0 = time.time()
    markets = feed.fetch_markets()
    fetch_ms = (time.time() - t0) * 1000

    if not markets:
        print("[rivalclaw] No markets available. Skipping trades.")
        _log_cycle_metrics(cycle_started_iso, 0, 0, 0, 0, 0, fetch_ms, 0, 0,
                           (time.time() * 1000 - cycle_started_at_ms))
        return

    # 2. Get wallet state
    state = wallet.get_state()
    print(f"[rivalclaw] Wallet: ${state['balance']:.2f} | "
          f"Positions: {state['open_positions']} | "
          f"Win rate: {state['win_rate']*100:.0f}%")

    # 3. Analyze markets (timed)
    t0 = time.time()
    decisions = brain.analyze(markets, state)
    analyze_ms = (time.time() - t0) * 1000
    opportunities_detected = len(decisions)
    print(f"[rivalclaw] Brain returned {opportunities_detected} arb signals")

    # 4. Execute trades (timed)
    t0 = time.time()
    open_ids = wallet.get_open_market_ids()
    trades_executed = 0
    opportunities_qualified = 0
    for d in decisions:
        if d.market_id in open_ids:
            continue
        opportunities_qualified += 1
        result = wallet.execute_trade(d, cycle_started_at_ms=cycle_started_at_ms)
        if result:
            open_ids.add(d.market_id)
            trades_executed += 1
            print(f"[rivalclaw] Executed: {d.direction} ${result['amount_usd']:.0f} "
                  f"on '{d.question[:50]}' [{d.strategy}]")
        else:
            print(f"[rivalclaw] Rejected: {d.market_id} (cap or kelly)")
    wallet_ms = (time.time() - t0) * 1000

    # 5. Check stops (always runs)
    try:
        current_prices = feed.get_latest_prices()
        closed = wallet.check_stops(current_prices)
        for c in closed:
            sign = "+" if (c["pnl"] or 0) >= 0 else ""
            print(f"[rivalclaw] Stop closed: {c['market_id']} -> {c['status']} "
                  f"{sign}${c['pnl']:.2f}")
    except Exception as exc:
        print(f"[rivalclaw] Stop check failed: {exc}")
        closed = []

    # 6. Daily snapshot + graduation check
    grad.maybe_snapshot()

    # 7. Log cycle metrics
    total_cycle_ms = time.time() * 1000 - cycle_started_at_ms
    _log_cycle_metrics(
        cycle_started_iso, len(markets), opportunities_detected,
        opportunities_qualified, trades_executed, len(closed),
        fetch_ms, analyze_ms, wallet_ms, total_cycle_ms,
    )

    print(f"[rivalclaw] Run complete — fetch={fetch_ms:.0f}ms analyze={analyze_ms:.0f}ms "
          f"wallet={wallet_ms:.0f}ms total={total_cycle_ms:.0f}ms")


def _log_cycle_metrics(started_at, markets, detected, qualified, executed,
                       closed, fetch_ms, analyze_ms, wallet_ms, total_ms):
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO cycle_metrics
            (experiment_id, instance_id, cycle_started_at,
             markets_fetched, opportunities_detected, opportunities_qualified,
             trades_executed, stops_closed,
             fetch_ms, analyze_ms, wallet_ms, total_cycle_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (EXPERIMENT_ID, INSTANCE_ID, started_at,
              markets, detected, qualified, executed, closed,
              fetch_ms, analyze_ms, wallet_ms, total_ms))
        conn.commit()
    finally:
        conn.close()
