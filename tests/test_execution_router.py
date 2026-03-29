"""Tests for execution_router — pre-flight safety checks."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# FakeDecision — mimics a TradeDecision for testing
# ---------------------------------------------------------------------------

class FakeDecision:
    def __init__(self, **kwargs):
        self.market_id = kwargs.get("market_id", "KXDOGE15M-TEST")
        self.direction = kwargs.get("direction", "YES")
        self.entry_price = kwargs.get("entry_price", 0.45)
        self.amount_usd = kwargs.get("amount_usd", 1.50)
        self.shares = kwargs.get("shares", 3)
        self.question = kwargs.get("question", "Test?")
        self.strategy = kwargs.get("strategy", "fair_value")
        self.confidence = kwargs.get("confidence", 0.8)
        self.reasoning = kwargs.get("reasoning", "test")
        self.metadata = kwargs.get("metadata", {"venue": "kalshi", "edge": 0.05})
        self.venue = kwargs.get("venue", "kalshi")
        self.decision_generated_at_ms = kwargs.get("decision_generated_at_ms", 0)


# ---------------------------------------------------------------------------
# Shared DB setup
# ---------------------------------------------------------------------------

def _make_db(path: str) -> None:
    """Create the required tables in a temp DB."""
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
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Shared env dict for live-mode tests
# ---------------------------------------------------------------------------

_LIVE_ENV = {
    "RIVALCLAW_EXECUTION_MODE": "live",
    "RIVALCLAW_LIVE_KILL_SWITCH": "0",
    "RIVALCLAW_LIVE_MAX_ORDER_USD": "2",
    "RIVALCLAW_LIVE_MAX_EXPOSURE_USD": "10",
    "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "5",
    "RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE": "2",
    "RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR": "10",
    "RIVALCLAW_LIVE_SERIES": "KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M",
    "RIVALCLAW_LIVE_MAX_PRICE_DEVIATION": "0.10",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset execution_router module-level counters between tests."""
    import importlib
    import sys
    # Ensure fresh import each test by cleaning counters
    if "execution_router" in sys.modules:
        mod = sys.modules["execution_router"]
        mod._cycle_order_count = 0
        mod._current_cycle_id = ""
        mod._hour_order_count = 0
        mod._hour_window_start = 0.0
    yield


@pytest.fixture
def tmp_db():
    """Create a temp DB with required tables, yield the path, clean up."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = f.name
    f.close()
    _make_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kill_switch_rejects(tmp_db):
    """Kill switch on should reject with reason 'kill_switch'."""
    env = {**_LIVE_ENV, "RIVALCLAW_LIVE_KILL_SWITCH": "1"}

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, env, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert result["reason"] == "kill_switch"


def test_mode_paper_rejects_live(tmp_db):
    """Paper mode should reject with reason 'mode_not_live'."""
    env = {**_LIVE_ENV, "RIVALCLAW_EXECUTION_MODE": "paper"}

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, env, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert result["reason"] == "mode_not_live"


def test_order_too_large_rejects(tmp_db):
    """Order exceeding max_order_usd should be rejected."""
    env = {**_LIVE_ENV, "RIVALCLAW_LIVE_MAX_ORDER_USD": "1"}

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, env, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(amount_usd=1.50),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert "order_too_large" in result["reason"]


def test_insufficient_balance_rejects(tmp_db):
    """Balance too low for order should be rejected."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, _LIVE_ENV, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(amount_usd=1.50),
            last_market_price=0.45,
            account_balance_cents=50,  # only 50 cents
        )
    assert result["passed"] is False
    assert "insufficient_balance" in result["reason"]


def test_wrong_series_rejects(tmp_db):
    """Ticker with disallowed series prefix should be rejected."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, _LIVE_ENV, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(market_id="KXINXSPX-26MAR29"),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert result["reason"] == "series_not_allowed"


def test_price_deviation_rejects(tmp_db):
    """Entry price with >10% deviation from market should be rejected."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, _LIVE_ENV, clear=False):
        # entry_price=0.60 vs market=0.45 => ~33% deviation
        result = er.preflight_check(
            decision=FakeDecision(entry_price=0.60),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert "price_deviation" in result["reason"]


def test_valid_order_passes(tmp_db):
    """A valid order within all limits should pass all checks."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, _LIVE_ENV, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(
                amount_usd=1.50,
                shares=3,
                entry_price=0.45,
                market_id="KXDOGE15M-TEST",
            ),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is True


def test_too_many_contracts_rejects(tmp_db):
    """Shares exceeding max_contracts should be rejected."""
    env = {**_LIVE_ENV, "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "2"}

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import execution_router as er
    er._db_path = tmp_db

    with patch.dict(os.environ, env, clear=False):
        result = er.preflight_check(
            decision=FakeDecision(shares=5),
            last_market_price=0.45,
            account_balance_cents=50000,
        )
    assert result["passed"] is False
    assert "too_many_contracts" in result["reason"]
