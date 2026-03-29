"""Tests for kalshi_executor — rate limiter, order building, DB logging."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import uuid

import pytest

# Point DB to a temp file before importing the module
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db_path = _tmp_db.name
_tmp_db.close()
os.environ["RIVALCLAW_DB_PATH"] = _tmp_db_path

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kalshi_executor as ke


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _setup_db():
    """Create the required tables in the temp DB before each test, tear down after."""
    conn = sqlite3.connect(_tmp_db_path)
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
    yield
    # Clean rows after each test
    conn = sqlite3.connect(_tmp_db_path)
    conn.execute("DELETE FROM live_orders")
    conn.execute("DELETE FROM account_snapshots")
    conn.execute("DELETE FROM live_reconciliation")
    conn.commit()
    conn.close()


@pytest.fixture
def _clean_db():
    """Provide a clean DB connection for assertions."""
    conn = sqlite3.connect(_tmp_db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------

def test_rate_limiter_allows_within_limit():
    """5 acquires within a limit of 5 should all succeed."""
    rl = ke.RateLimiter(max_per_second=5)
    results = [rl.acquire() for _ in range(5)]
    assert all(results), "All 5 acquires should return True"


def test_rate_limiter_blocks_over_limit():
    """3rd acquire at limit=2 should be blocked."""
    rl = ke.RateLimiter(max_per_second=2)
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire() is False


def test_rate_limiter_resets_after_window():
    """After the 1-second window elapses, acquire should succeed again."""
    rl = ke.RateLimiter(max_per_second=1)
    assert rl.acquire() is True
    assert rl.acquire() is False
    # Manually advance the window start to simulate time passing
    rl._window_start = time.time() - 1.1
    assert rl.acquire() is True


def test_rate_limiter_usage():
    """Usage dict should report correct used/limit/remaining."""
    rl = ke.RateLimiter(max_per_second=5)
    rl.acquire()
    rl.acquire()
    usage = rl.usage()
    assert usage["used"] == 2
    assert usage["limit"] == 5
    assert usage["remaining"] == 3


# ---------------------------------------------------------------------------
# build_order_payload tests
# ---------------------------------------------------------------------------

def test_build_order_payload():
    """Correct field mapping, dollar-to-cents conversion, UUID client_order_id."""
    payload = ke.build_order_payload(
        ticker="KXBTC-26MAR29-95000",
        action="buy",
        side="yes",
        count=10,
        yes_price_dollars=0.55,
    )
    assert payload["ticker"] == "KXBTC-26MAR29-95000"
    assert payload["action"] == "buy"
    assert payload["side"] == "yes"
    assert payload["count"] == 10
    assert payload["yes_price"] == 55  # 0.55 * 100
    assert payload["type"] == "limit"
    # client_order_id should be a valid UUID4
    uuid.UUID(payload["client_order_id"], version=4)


def test_build_order_payload_clamps_price():
    """Prices should be clamped to 1-99 cents."""
    low = ke.build_order_payload("T", "buy", "yes", 1, 0.001)
    assert low["yes_price"] == 1

    high = ke.build_order_payload("T", "buy", "yes", 1, 1.50)
    assert high["yes_price"] == 99

    zero = ke.build_order_payload("T", "buy", "yes", 1, 0.0)
    assert zero["yes_price"] == 1


# ---------------------------------------------------------------------------
# DB logging tests
# ---------------------------------------------------------------------------

def test_log_shadow_order(_clean_db):
    """log_order inserts a row with correct fields into live_orders."""
    order_id = ke.log_order(
        intent_id="intent-abc",
        ticker="KXBTC-26MAR29-95000",
        action="buy",
        side="yes",
        count=5,
        yes_price_cents=55,
        mode="shadow",
        client_order_id="test-uuid-123",
    )
    assert isinstance(order_id, int)

    row = _clean_db.execute(
        "SELECT * FROM live_orders WHERE id = ?", (order_id,)
    ).fetchone()
    assert row is not None
    assert row["intent_id"] == "intent-abc"
    assert row["ticker"] == "KXBTC-26MAR29-95000"
    assert row["action"] == "buy"
    assert row["side"] == "yes"
    assert row["count"] == 5
    assert row["yes_price"] == 55
    assert row["mode"] == "shadow"
    assert row["client_order_id"] == "test-uuid-123"
    assert row["status"] == "pending"


def test_log_account_snapshot(_clean_db):
    """log_account_snapshot inserts a row with correct fields."""
    snap_id = ke.log_account_snapshot(
        balance_cents=150000,
        portfolio_value_cents=200000,
        open_positions=3,
    )
    assert isinstance(snap_id, int)

    row = _clean_db.execute(
        "SELECT * FROM account_snapshots WHERE id = ?", (snap_id,)
    ).fetchone()
    assert row is not None
    assert row["balance_cents"] == 150000
    assert row["portfolio_value_cents"] == 200000
    assert row["open_positions"] == 3
    assert row["fetched_at"] is not None


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def teardown_module():
    """Remove the temporary DB file."""
    try:
        os.unlink(_tmp_db_path)
    except OSError:
        pass
