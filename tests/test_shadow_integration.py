"""Shadow mode integration tests — end-to-end verification."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import kalshi_executor
import execution_router


# ---------------------------------------------------------------------------
# FakeDecision
# ---------------------------------------------------------------------------

class FakeDecision:
    def __init__(self):
        self.market_id = "KXDOGE15M-26MAR29-DOGE020"
        self.direction = "YES"
        self.entry_price = 0.45
        self.amount_usd = 1.35
        self.shares = 3
        self.question = "Will DOGE be above $0.20 at 3:15 PM?"
        self.strategy = "fair_value"
        self.confidence = 0.82
        self.reasoning = "Fair value 0.52 vs market 0.45"
        self.metadata = {"venue": "kalshi", "edge": 0.07}
        self.venue = "kalshi"
        self.decision_generated_at_ms = 1000


# ---------------------------------------------------------------------------
# DB setup helper
# ---------------------------------------------------------------------------

def _make_db(path: str) -> None:
    """Create required tables in a temp DB."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_orders (
            id INTEGER PRIMARY KEY,
            intent_id TEXT NOT NULL,
            client_order_id TEXT UNIQUE NOT NULL,
            kalshi_order_id TEXT,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT NOT NULL,
            count INTEGER NOT NULL,
            yes_price INTEGER NOT NULL,
            order_type TEXT DEFAULT 'limit',
            status TEXT DEFAULT 'pending',
            fill_price INTEGER,
            fill_count INTEGER,
            submitted_at TEXT,
            filled_at TEXT,
            mode TEXT NOT NULL,
            error_message TEXT,
            rejection_reason TEXT,
            cycle_id TEXT,
            strategy TEXT,
            market_question TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY,
            balance_cents INTEGER,
            portfolio_value_cents INTEGER,
            open_positions INTEGER,
            fetched_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_reconciliation (
            id INTEGER PRIMARY KEY,
            live_order_id INTEGER REFERENCES live_orders(id),
            paper_entry_price REAL,
            live_fill_price REAL,
            slippage_delta_bps REAL,
            paper_amount_usd REAL,
            live_amount_usd REAL,
            reconciled_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixture: reset module state between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset execution_router module-level counters between tests."""
    execution_router._cycle_order_count = 0
    execution_router._current_cycle_id = ""
    execution_router._hour_order_count = 0
    execution_router._hour_window_start = 0.0
    yield
    execution_router._cycle_order_count = 0
    execution_router._current_cycle_id = ""
    execution_router._hour_order_count = 0
    execution_router._hour_window_start = 0.0


# ---------------------------------------------------------------------------
# Test 1: shadow mode logs order without API call
# ---------------------------------------------------------------------------

def test_shadow_mode_logs_order_without_api_call():
    """Shadow mode should log the order to DB and return status='logged'."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    _make_db(db_path)

    env = {
        "RIVALCLAW_EXECUTION_MODE": "shadow",
        "RIVALCLAW_LIVE_KILL_SWITCH": "0",
        "RIVALCLAW_LIVE_MAX_ORDER_USD": "2",
        "RIVALCLAW_LIVE_MAX_EXPOSURE_USD": "10",
        "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "5",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE": "2",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR": "10",
        "RIVALCLAW_LIVE_SERIES": "KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M",
        "RIVALCLAW_LIVE_MAX_PRICE_DEVIATION": "0.10",
    }

    try:
        with patch.dict(os.environ, env, clear=False):
            # Override DB paths to use temp DB
            kalshi_executor.DB_PATH = Path(db_path)
            execution_router._db_path = db_path

            execution_router.reset_cycle("test_cycle_1")

            decision = FakeDecision()
            result = execution_router.route_trade(
                decision=decision,
                protocol_result={"id": "proto_123"},
                last_market_price=0.45,
                account_balance_cents=1000,
            )

        # Verify return value
        assert result["mode"] == "shadow"
        assert result["status"] == "logged"

        # Verify DB row
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM live_orders").fetchone()
        conn.close()

        assert row is not None
        assert row["mode"] == "shadow"
        assert "KXDOGE15M" in row["ticker"]
        assert row["action"] == "buy"
        assert row["side"] == "yes"
        assert row["count"] == 3
        assert row["yes_price"] == 45

    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Test 2: shadow mode rejects when kill switch is on
# ---------------------------------------------------------------------------

def test_shadow_mode_rejects_when_kill_switch_on():
    """Kill switch on should reject the order and log it as rejected."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    _make_db(db_path)

    env = {
        "RIVALCLAW_EXECUTION_MODE": "shadow",
        "RIVALCLAW_LIVE_KILL_SWITCH": "1",
        "RIVALCLAW_LIVE_MAX_ORDER_USD": "2",
        "RIVALCLAW_LIVE_MAX_EXPOSURE_USD": "10",
        "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "5",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE": "2",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR": "10",
        "RIVALCLAW_LIVE_SERIES": "KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M",
        "RIVALCLAW_LIVE_MAX_PRICE_DEVIATION": "0.10",
    }

    try:
        with patch.dict(os.environ, env, clear=False):
            # Override DB paths to use temp DB
            kalshi_executor.DB_PATH = Path(db_path)
            execution_router._db_path = db_path

            execution_router.reset_cycle("test_cycle_1")

            decision = FakeDecision()
            result = execution_router.route_trade(
                decision=decision,
                protocol_result={"id": "proto_123"},
                last_market_price=0.45,
                account_balance_cents=1000,
            )

        # Verify return value
        assert result["status"] == "rejected"
        assert result["reason"] == "kill_switch"

        # Verify DB row
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM live_orders").fetchone()
        conn.close()

        assert row is not None
        assert row["status"] == "rejected"
        assert row["rejection_reason"] == "kill_switch"

    finally:
        os.unlink(db_path)
