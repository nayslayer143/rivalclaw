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

CREATE TABLE IF NOT EXISTS kalshi_extra (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    event_ticker TEXT,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume_24h REAL,
    open_interest REAL,
    close_time TEXT,
    strike_type TEXT,
    cap_strike REAL,
    floor_strike REAL,
    rules_primary TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kalshi_extra_market_time ON kalshi_extra(market_id, fetched_at);

CREATE TABLE IF NOT EXISTS spot_prices (
    id INTEGER PRIMARY KEY,
    crypto_id TEXT NOT NULL,
    price_usd REAL NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_prices_crypto_time ON spot_prices(crypto_id, fetched_at);

CREATE TABLE IF NOT EXISTS tuning_log (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    parameter TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    tuned_at TEXT NOT NULL
);
"""


def migrate():
    with _get_conn() as conn:
        conn.executescript(MIGRATION_SQL)
        # Add venue columns (idempotent — ignore if already exist)
        for tbl in ("market_data", "paper_trades"):
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN venue TEXT DEFAULT 'polymarket'")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN expected_edge REAL")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    print(f"[rivalclaw] Migration complete. DB: {DB_PATH}")


def run_loop():
    """Full simulation loop with per-cycle timing instrumentation."""
    sys.path.insert(0, str(Path(__file__).parent))
    import polymarket_feed as poly_feed
    import kalshi_feed
    import spot_feed
    import paper_wallet as wallet
    import trading_brain as brain
    import graduation as grad

    cycle_started_at_ms = time.time() * 1000
    cycle_started_iso = datetime.datetime.utcnow().isoformat()
    print(f"[rivalclaw] Run loop starting — {cycle_started_iso}")

    # Auto-reload wallet if balance drops below $100 (learning must continue)
    RELOAD_THRESHOLD = float(os.environ.get("RIVALCLAW_RELOAD_THRESHOLD", "100"))
    RELOAD_AMOUNT = float(os.environ.get("RIVALCLAW_RELOAD_AMOUNT", "1000"))
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
        ).fetchone()
        current_starting = float(row["value"]) if row else 1000.0
        # Quick balance estimate: starting + closed pnl
        closed_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'"
        ).fetchone()[0]
        est_balance = current_starting + closed_pnl
        if est_balance < RELOAD_THRESHOLD:
            new_starting = current_starting + RELOAD_AMOUNT
            conn.execute(
                "UPDATE context SET value=? WHERE chat_id='rivalclaw' AND key='starting_balance'",
                (str(new_starting),))
            conn.commit()
            print(f"[rivalclaw] WALLET RELOAD: ${current_starting:.0f} -> ${new_starting:.0f} "
                  f"(balance was ${est_balance:.0f}, injected ${RELOAD_AMOUNT:.0f})")
    finally:
        conn.close()

    # 1. Fetch market data from both venues (timed)
    t0 = time.time()
    poly_markets = poly_feed.fetch_markets()
    kalshi_markets = kalshi_feed.fetch_markets()
    markets = poly_markets + kalshi_markets
    fetch_ms = (time.time() - t0) * 1000
    print(f"[rivalclaw] Fetched: {len(poly_markets)} Polymarket + "
          f"{len(kalshi_markets)} Kalshi = {len(markets)} total")

    if not markets:
        print("[rivalclaw] No markets available. Skipping trades.")
        _log_cycle_metrics(cycle_started_iso, 0, 0, 0, 0, 0, fetch_ms, 0, 0,
                           (time.time() * 1000 - cycle_started_at_ms))
        return

    # 1b. Classify and filter markets by resolution speed
    import market_classifier
    markets = market_classifier.classify_and_filter(markets)

    # 2. Get wallet state + spot prices for fair value
    state = wallet.get_state()
    spot_prices = spot_feed.get_spot_prices()
    # Log spot prices for realized vol computation (self-tuner)
    if spot_prices:
        conn = _get_conn()
        try:
            for crypto_id, price in spot_prices.items():
                conn.execute(
                    "INSERT INTO spot_prices (crypto_id, price_usd, fetched_at) VALUES (?,?,?)",
                    (crypto_id, price, cycle_started_iso))
            conn.commit()
        finally:
            conn.close()
    print(f"[rivalclaw] Wallet: ${state['balance']:.2f} | "
          f"Positions: {state['open_positions']} | "
          f"Win rate: {state['win_rate']*100:.0f}% | "
          f"Spots: {len(spot_prices)} cryptos")

    # 3. Analyze all markets with all strategies (timed)
    t0 = time.time()
    decisions = brain.analyze(markets, state, spot_prices=spot_prices)
    analyze_ms = (time.time() - t0) * 1000
    opportunities_detected = len(decisions)

    # 3b. Risk engine: regime detection + strategy tournament + position limits
    import risk_engine
    regime = risk_engine.detect_regime()
    scores = risk_engine.get_strategy_scores()
    adjusted = []
    blocked = 0
    for d in decisions:
        result = risk_engine.adjust_decision(d, state["balance"], regime, scores)
        if result:
            adjusted.append(result)
        else:
            blocked += 1
    decisions = adjusted
    regime_str = regime.get("regime", "?")
    score_str = " ".join(f"{k}={v:.1f}" for k, v in sorted(scores.items())[:4])
    print(f"[rivalclaw] Brain={opportunities_detected} Risk={len(decisions)} blocked={blocked} "
          f"regime={regime_str} scores=[{score_str}]")

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

    # 5. Check stops (always runs, both venues)
    try:
        current_prices = wallet._get_all_latest_prices()
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

    # 8. Real-time Telegram alerts when trades execute
    if trades_executed > 0 or closed:
        try:
            import notify
            closed_wins = [c for c in closed if (c.get("pnl") or 0) > 0]
            closed_losses = [c for c in closed if (c.get("pnl") or 0) <= 0]
            closed_pnl = sum(c.get("pnl") or 0 for c in closed)
            msg = (f"⚡ RIVALCLAW {cycle_started_iso[11:19]}\n"
                   f"Traded: {trades_executed} new | Closed: {len(closed)} "
                   f"(W:{len(closed_wins)} L:{len(closed_losses)} ${closed_pnl:+,.0f})\n"
                   f"Balance: ${state['balance']:,.0f} | Open: {state['open_positions']+trades_executed}")
            notify.send_telegram(msg, parse_mode="")
        except Exception:
            pass  # Never crash the loop for notifications


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
