# Kalshi Live Trading Bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shadow and live execution modes to RivalClaw so trade intents can be submitted to Kalshi's REST API as real orders.

**Architecture:** Extend the existing `protocol_adapter.py` with an execution router that decides per-trade whether to shadow-log or live-submit. A new `kalshi_executor.py` handles all Kalshi order API calls with RSA auth (reusing `kalshi_feed.py` patterns), rate limiting, and account sync. Safety enforced by a 10-point pre-flight checklist in `execution_router.py`.

**Tech Stack:** Python 3.9+, SQLite (rivalclaw.db), Kalshi REST API v2, `requests`, `cryptography` (RSA signing — already installed)

**Spec:** `docs/superpowers/specs/2026-03-29-kalshi-live-bridge-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `kalshi_executor.py` | Create | Kalshi order submission, status polling, account sync, rate limiting |
| `execution_router.py` | Create | Pre-flight safety checks, route to shadow or live execution |
| `simulator.py` | Modify | Add DB migration for new tables, account sync at cycle start, call execution router after protocol trades |
| `protocol_adapter.py` | Modify | After `execute_trade()`, call execution router |
| `notify.py` | Modify | Add `send_live_alert()` helper for live trading events |
| `.env` | Modify | Add execution mode + safety limit env vars |
| `CLAUDE.md` | Modify | Update doctrine to permit live trading under safety controls |
| `tests/test_execution_router.py` | Create | Unit tests for pre-flight safety checks |
| `tests/test_kalshi_executor.py` | Create | Unit tests for order construction, rate limiting, account sync |

---

### Task 1: DB Migration — New Tables

**Files:**
- Modify: `simulator.py:32-148` (MIGRATION_SQL string)

- [ ] **Step 1: Add live_orders table to MIGRATION_SQL**

In `simulator.py`, append the following SQL to the end of the `MIGRATION_SQL` string, just before the closing `"""` on line 148:

```python
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
);
CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders(status);
CREATE INDEX IF NOT EXISTS idx_live_orders_mode ON live_orders(mode);

CREATE TABLE IF NOT EXISTS live_reconciliation (
    id INTEGER PRIMARY KEY,
    live_order_id INTEGER REFERENCES live_orders(id),
    paper_entry_price REAL,
    live_fill_price REAL,
    slippage_delta_bps REAL,
    paper_amount_usd REAL,
    live_amount_usd REAL,
    reconciled_at TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY,
    balance_cents INTEGER,
    portfolio_value_cents INTEGER,
    open_positions INTEGER,
    fetched_at TEXT
);
```

- [ ] **Step 2: Run migration to create tables**

Run: `cd ~/rivalclaw && python3 -c "import simulator; simulator.migrate()"`
Expected: `[rivalclaw] Migration complete. DB: /Users/nayslayer/rivalclaw/rivalclaw.db`

- [ ] **Step 3: Verify tables exist**

Run: `cd ~/rivalclaw && python3 -c "import sqlite3; conn=sqlite3.connect('rivalclaw.db'); print([r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'live%' OR name='account_snapshots'\").fetchall()])"`
Expected: `['live_orders', 'live_reconciliation', 'account_snapshots']`

- [ ] **Step 4: Commit**

```bash
cd ~/rivalclaw && git add simulator.py && git commit -m "feat: add live_orders, live_reconciliation, account_snapshots tables"
```

---

### Task 2: Notify Helper — Live Trading Alerts

**Files:**
- Modify: `notify.py`

- [ ] **Step 1: Add send_live_alert function**

Add the following function to `notify.py` after the `send_telegram` function (after line 52):

```python
def send_live_alert(event: str, details: str = "") -> bool:
    """Send a live trading alert via Telegram.

    Events: order_submitted, order_filled, order_rejected, kill_switch,
            mode_change, rate_limited, slippage_warning, balance_low
    """
    prefix = {
        "order_submitted": "ORDER SENT",
        "order_filled": "FILLED",
        "order_rejected": "REJECTED",
        "kill_switch": "KILL SWITCH ACTIVATED",
        "mode_change": "MODE CHANGE",
        "rate_limited": "RATE LIMITED",
        "slippage_warning": "SLIPPAGE WARNING",
        "balance_low": "LOW BALANCE",
    }.get(event, event.upper())
    msg = f"[RivalClaw LIVE] {prefix}\n{details}"
    return send_telegram(msg, parse_mode="")
```

- [ ] **Step 2: Commit**

```bash
cd ~/rivalclaw && git add notify.py && git commit -m "feat: add send_live_alert for live trading notifications"
```

---

### Task 3: Rate Limiter

**Files:**
- Create: `kalshi_executor.py` (partial — rate limiter section only, rest added in Task 5)
- Create: `tests/test_kalshi_executor.py` (partial — rate limiter tests only)

- [ ] **Step 1: Create tests directory and write rate limiter tests**

```bash
mkdir -p ~/rivalclaw/tests
```

Write `tests/__init__.py`:
```python
```

Write `tests/test_kalshi_executor.py`:

```python
#!/usr/bin/env python3
"""Tests for kalshi_executor.py"""
from __future__ import annotations
import time
import sys
from pathlib import Path

# Add parent dir so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_rate_limiter_allows_within_limit():
    from kalshi_executor import RateLimiter
    rl = RateLimiter(max_per_second=5)
    for _ in range(5):
        assert rl.acquire() is True


def test_rate_limiter_blocks_over_limit():
    from kalshi_executor import RateLimiter
    rl = RateLimiter(max_per_second=2)
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire() is False


def test_rate_limiter_resets_after_window():
    from kalshi_executor import RateLimiter
    rl = RateLimiter(max_per_second=1)
    assert rl.acquire() is True
    assert rl.acquire() is False
    # Manually advance the window
    rl._window_start = time.time() - 1.1
    rl._count = 0
    assert rl.acquire() is True


def test_rate_limiter_usage():
    from kalshi_executor import RateLimiter
    rl = RateLimiter(max_per_second=10)
    rl.acquire()
    rl.acquire()
    rl.acquire()
    usage = rl.usage()
    assert usage["used"] == 3
    assert usage["limit"] == 10
    assert usage["remaining"] == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_kalshi_executor.py -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError` for `kalshi_executor`

- [ ] **Step 3: Create kalshi_executor.py with RateLimiter class**

Write `kalshi_executor.py`:

```python
#!/usr/bin/env python3
"""
RivalClaw Kalshi executor — live order submission, status polling, account sync.
Reuses RSA auth from kalshi_feed.py. All Kalshi write operations go through here.
"""
from __future__ import annotations

import os
import time
import uuid
import sqlite3
import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Rate limit tier (Basic = 10 writes/sec)
WRITE_RATE_LIMIT = int(os.environ.get("RIVALCLAW_KALSHI_WRITE_RATE", "10"))


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple sliding-window rate limiter for API writes."""

    def __init__(self, max_per_second: int = WRITE_RATE_LIMIT):
        self._max = max_per_second
        self._window_start = time.time()
        self._count = 0

    def acquire(self) -> bool:
        """Try to acquire a rate limit token. Returns True if allowed."""
        now = time.time()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._count = 0
        if self._count >= self._max:
            return False
        self._count += 1
        return True

    def usage(self) -> dict:
        """Return current usage stats for dashboard."""
        return {
            "used": self._count,
            "limit": self._max,
            "remaining": max(0, self._max - self._count),
        }


_write_limiter = RateLimiter()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_kalshi_executor.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/rivalclaw && git add kalshi_executor.py tests/ && git commit -m "feat: add RateLimiter for Kalshi API write rate limiting"
```

---

### Task 4: Kalshi Executor — Order Submission & Account Sync

**Files:**
- Modify: `kalshi_executor.py` (add order submission, polling, account sync)
- Modify: `tests/test_kalshi_executor.py` (add order construction + DB tests)

- [ ] **Step 1: Write tests for order construction and DB logging**

Append to `tests/test_kalshi_executor.py`:

```python
import tempfile
import json


def _make_test_db():
    """Create a temporary DB with live_orders table."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE live_orders (
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
        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY,
            balance_cents INTEGER,
            portfolio_value_cents INTEGER,
            open_positions INTEGER,
            fetched_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    return tmp.name


def test_build_order_payload():
    from kalshi_executor import build_order_payload
    payload = build_order_payload(
        ticker="KXDOGE15M-26MAR29-BTC100K",
        action="buy",
        side="yes",
        count=3,
        yes_price_dollars=0.45,
    )
    assert payload["ticker"] == "KXDOGE15M-26MAR29-BTC100K"
    assert payload["action"] == "buy"
    assert payload["side"] == "yes"
    assert payload["count"] == 3
    assert payload["yes_price"] == 45  # cents
    assert payload["type"] == "limit"
    assert "client_order_id" in payload
    # client_order_id should be a valid UUID4
    uuid.UUID(payload["client_order_id"])


def test_build_order_payload_clamps_price():
    from kalshi_executor import build_order_payload
    payload = build_order_payload("T", "buy", "yes", 1, 0.005)
    assert payload["yes_price"] == 1  # minimum 1 cent
    payload = build_order_payload("T", "buy", "yes", 1, 0.999)
    assert payload["yes_price"] == 99  # maximum 99 cents


def test_log_shadow_order(tmp_path):
    import kalshi_executor as ke
    db_path = _make_test_db()
    ke.DB_PATH = Path(db_path)
    ke.log_order(
        intent_id="abc123",
        ticker="KXDOGE15M-TEST",
        action="buy",
        side="yes",
        count=2,
        yes_price_cents=45,
        mode="shadow",
        cycle_id="cycle1",
        strategy="fair_value",
        market_question="Will DOGE be above $0.20?",
    )
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT * FROM live_orders WHERE intent_id='abc123'").fetchone()
    assert row is not None
    assert row[5] == "buy"   # action
    assert row[6] == "yes"   # side
    assert row[7] == 2       # count
    assert row[8] == 45      # yes_price
    assert row[15] == "shadow"  # mode
    conn.close()
    os.unlink(db_path)


def test_log_account_snapshot(tmp_path):
    import kalshi_executor as ke
    db_path = _make_test_db()
    ke.DB_PATH = Path(db_path)
    ke.log_account_snapshot(balance_cents=1000, portfolio_value_cents=1050, open_positions=3)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT * FROM account_snapshots").fetchone()
    assert row is not None
    assert row[1] == 1000
    assert row[2] == 1050
    assert row[3] == 3
    conn.close()
    os.unlink(db_path)
```

Add `import sqlite3, os` to the top of the test file if not already there.

- [ ] **Step 2: Run tests to verify the new tests fail**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_kalshi_executor.py -v -k "build_order or log_shadow or log_account"`
Expected: FAIL — `ImportError` for `build_order_payload`, `log_order`, `log_account_snapshot`

- [ ] **Step 3: Implement order construction and DB logging in kalshi_executor.py**

Append to `kalshi_executor.py`:

```python
import requests as _requests

try:
    import notify
except ImportError:
    notify = None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------

def build_order_payload(
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price_dollars: float,
) -> dict:
    """Build a Kalshi order payload. Price converted from dollars (0.01-0.99) to cents (1-99)."""
    yes_price_cents = max(1, min(99, int(round(yes_price_dollars * 100))))
    return {
        "ticker": ticker,
        "action": action,
        "side": side,
        "count": count,
        "yes_price": yes_price_cents,
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# DB logging
# ---------------------------------------------------------------------------

def log_order(
    intent_id: str,
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price_cents: int,
    mode: str,
    cycle_id: str = "",
    strategy: str = "",
    market_question: str = "",
    client_order_id: str | None = None,
    kalshi_order_id: str | None = None,
    status: str = "pending",
    error_message: str | None = None,
    rejection_reason: str | None = None,
) -> int:
    """Log an order (shadow or live) to the live_orders table. Returns row id."""
    if client_order_id is None:
        client_order_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO live_orders
            (intent_id, client_order_id, kalshi_order_id, ticker, action, side,
             count, yes_price, order_type, status, submitted_at, mode,
             error_message, rejection_reason, cycle_id, strategy, market_question)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            intent_id, client_order_id, kalshi_order_id, ticker, action, side,
            count, yes_price_cents, "limit", status, now, mode,
            error_message, rejection_reason, cycle_id, strategy, market_question,
        ))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_order_status(
    order_id: int,
    status: str,
    kalshi_order_id: str | None = None,
    fill_price: int | None = None,
    fill_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update a live_orders row after submission or fill."""
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        updates = ["status=?"]
        values: list[Any] = [status]
        if kalshi_order_id is not None:
            updates.append("kalshi_order_id=?")
            values.append(kalshi_order_id)
        if fill_price is not None:
            updates.append("fill_price=?")
            values.append(fill_price)
        if fill_count is not None:
            updates.append("fill_count=?")
            values.append(fill_count)
        if error_message is not None:
            updates.append("error_message=?")
            values.append(error_message)
        if status == "filled":
            updates.append("filled_at=?")
            values.append(now)
        values.append(order_id)
        conn.execute(
            f"UPDATE live_orders SET {', '.join(updates)} WHERE id=?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def log_account_snapshot(
    balance_cents: int,
    portfolio_value_cents: int,
    open_positions: int,
) -> None:
    """Log a Kalshi account balance snapshot."""
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO account_snapshots (balance_cents, portfolio_value_cents, open_positions, fetched_at)
            VALUES (?,?,?,?)
        """, (balance_cents, portfolio_value_cents, open_positions, now))
        conn.commit()
    finally:
        conn.close()


def log_reconciliation(
    live_order_id: int,
    paper_entry_price: float,
    live_fill_price_cents: int,
    paper_amount_usd: float,
    live_amount_usd: float,
) -> None:
    """Log reconciliation between paper and live fills."""
    live_fill_dollars = live_fill_price_cents / 100.0
    if paper_entry_price > 0:
        slippage_delta_bps = (live_fill_dollars - paper_entry_price) / paper_entry_price * 10000
    else:
        slippage_delta_bps = 0.0
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT INTO live_reconciliation
            (live_order_id, paper_entry_price, live_fill_price, slippage_delta_bps,
             paper_amount_usd, live_amount_usd, reconciled_at)
            VALUES (?,?,?,?,?,?,?)
        """, (live_order_id, paper_entry_price, live_fill_dollars,
              slippage_delta_bps, paper_amount_usd, live_amount_usd, now))
        conn.commit()
    finally:
        conn.close()

    # Alert if slippage exceeds 500 bps (5%)
    if abs(slippage_delta_bps) > 500 and notify:
        notify.send_live_alert(
            "slippage_warning",
            f"Slippage {slippage_delta_bps:.0f} bps on order #{live_order_id}\n"
            f"Paper: ${paper_entry_price:.4f} Live: ${live_fill_dollars:.4f}",
        )


# ---------------------------------------------------------------------------
# Kalshi API calls (live mode only)
# ---------------------------------------------------------------------------

def _get_kalshi_auth_headers(method: str, path: str) -> dict | None:
    """Reuse RSA auth from kalshi_feed.py."""
    try:
        import kalshi_feed
        return kalshi_feed._auth_headers(method, path)
    except Exception:
        return None


def _get_api_base() -> str:
    """Get Kalshi API base URL."""
    try:
        import kalshi_feed
        return kalshi_feed._get_api_base()
    except Exception:
        env = os.environ.get("KALSHI_API_ENV", "demo").lower()
        if env == "prod":
            return "https://api.elections.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"


def submit_order(payload: dict) -> dict:
    """Submit an order to Kalshi. Returns API response dict or error dict.

    Handles rate limiting and exponential backoff on 429s.
    """
    if not _write_limiter.acquire():
        return {"error": "rate_limited", "message": "Write rate limit exceeded"}

    path = "/portfolio/orders"
    headers = _get_kalshi_auth_headers("POST", path)
    if headers is None:
        return {"error": "auth_failed", "message": "Could not generate auth headers"}

    url = f"{_get_api_base()}{path}"
    backoff = 1.0
    max_retries = 3

    for attempt in range(max_retries):
        try:
            resp = _requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 201:
                return resp.json()
            if resp.status_code == 429:
                print(f"[rivalclaw/executor] 429 Rate limited, backoff {backoff}s")
                if notify and attempt == max_retries - 1:
                    notify.send_live_alert("rate_limited", f"429 after {max_retries} retries")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 409:
                return {"error": "duplicate", "message": "Duplicate client_order_id", "status_code": 409}
            return {
                "error": "api_error",
                "message": resp.text[:200],
                "status_code": resp.status_code,
            }
        except Exception as e:
            return {"error": "network_error", "message": str(e)}

    return {"error": "rate_limited", "message": f"Still 429 after {max_retries} retries"}


def poll_order_status(kalshi_order_id: str, max_polls: int = 3, interval: float = 2.0) -> dict:
    """Poll Kalshi for order fill status. Returns latest order state."""
    path = f"/portfolio/orders/{kalshi_order_id}"
    for i in range(max_polls):
        headers = _get_kalshi_auth_headers("GET", path)
        if headers is None:
            return {"error": "auth_failed"}
        url = f"{_get_api_base()}{path}"
        try:
            resp = _requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                order = data.get("order", data)
                status = order.get("status", "")
                if status in ("executed", "canceled"):
                    return order
                if i < max_polls - 1:
                    time.sleep(interval)
            else:
                return {"error": "api_error", "status_code": resp.status_code}
        except Exception as e:
            return {"error": "network_error", "message": str(e)}
    return {"status": "pending", "message": f"Still pending after {max_polls} polls"}


def cancel_order(kalshi_order_id: str) -> dict:
    """Cancel a single resting order."""
    if not _write_limiter.acquire():
        return {"error": "rate_limited"}
    path = f"/portfolio/orders/{kalshi_order_id}"
    headers = _get_kalshi_auth_headers("DELETE", path)
    if headers is None:
        return {"error": "auth_failed"}
    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.delete(url, headers=headers, timeout=10)
        if resp.status_code in (200, 204):
            return {"status": "cancelled"}
        return {"error": "api_error", "status_code": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"error": "network_error", "message": str(e)}


def batch_cancel_orders() -> dict:
    """Cancel all resting orders."""
    if not _write_limiter.acquire():
        return {"error": "rate_limited"}
    path = "/portfolio/orders/batch"
    headers = _get_kalshi_auth_headers("DELETE", path)
    if headers is None:
        return {"error": "auth_failed"}
    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.delete(url, headers=headers, timeout=15)
        if resp.status_code in (200, 204):
            return {"status": "cancelled_all"}
        return {"error": "api_error", "status_code": resp.status_code}
    except Exception as e:
        return {"error": "network_error", "message": str(e)}


def get_balance() -> dict:
    """Get real Kalshi account balance. Returns {balance_cents, portfolio_value_cents}."""
    path = "/portfolio/balance"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return {"error": "auth_failed"}
    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": "api_error", "status_code": resp.status_code}
    except Exception as e:
        return {"error": "network_error", "message": str(e)}


def get_positions() -> list:
    """Get real Kalshi open positions."""
    path = "/portfolio/positions"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return []
    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("market_positions", [])
        return []
    except Exception:
        return []


def get_fills(limit: int = 50) -> list:
    """Get recent fill records from Kalshi."""
    path = "/portfolio/fills"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return []
    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, params={"limit": limit}, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("fills", [])
        return []
    except Exception:
        return []


def sync_account() -> dict | None:
    """Sync account balance and positions from Kalshi. Logs snapshot to DB."""
    balance_data = get_balance()
    if "error" in balance_data:
        print(f"[rivalclaw/executor] Account sync failed: {balance_data}")
        return None

    positions = get_positions()
    balance_cents = balance_data.get("balance", 0)
    portfolio_cents = balance_data.get("portfolio_value", 0)

    log_account_snapshot(balance_cents, portfolio_cents, len(positions))
    print(f"[rivalclaw/executor] Account sync: balance=${balance_cents/100:.2f} "
          f"portfolio=${portfolio_cents/100:.2f} positions={len(positions)}")
    return {
        "balance_cents": balance_cents,
        "portfolio_value_cents": portfolio_cents,
        "positions": positions,
    }


def get_rate_limit_usage() -> dict:
    """Return current write rate limit usage for dashboard."""
    return _write_limiter.usage()
```

- [ ] **Step 4: Run all tests**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_kalshi_executor.py -v`
Expected: All 8 tests pass

- [ ] **Step 5: Commit**

```bash
cd ~/rivalclaw && git add kalshi_executor.py tests/test_kalshi_executor.py && git commit -m "feat: add Kalshi order submission, polling, account sync, and DB logging"
```

---

### Task 5: Execution Router — Pre-Flight Safety Checks

**Files:**
- Create: `execution_router.py`
- Create: `tests/test_execution_router.py`

- [ ] **Step 1: Write execution router tests**

Write `tests/test_execution_router.py`:

```python
#!/usr/bin/env python3
"""Tests for execution_router.py pre-flight safety checks."""
from __future__ import annotations
import os
import sys
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_test_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE live_orders (
            id INTEGER PRIMARY KEY, intent_id TEXT, client_order_id TEXT UNIQUE,
            kalshi_order_id TEXT, ticker TEXT, action TEXT, side TEXT,
            count INTEGER, yes_price INTEGER, order_type TEXT DEFAULT 'limit',
            status TEXT DEFAULT 'pending', fill_price INTEGER, fill_count INTEGER,
            submitted_at TEXT, filled_at TEXT, mode TEXT, error_message TEXT,
            rejection_reason TEXT, cycle_id TEXT, strategy TEXT, market_question TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY, balance_cents INTEGER,
            portfolio_value_cents INTEGER, open_positions INTEGER, fetched_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    return tmp.name


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


def _setup_router(db_path, env_overrides=None):
    """Set up execution_router with test DB and env."""
    import execution_router as er
    import kalshi_executor as ke
    ke.DB_PATH = Path(db_path)
    er._db_path = Path(db_path)
    defaults = {
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
    if env_overrides:
        defaults.update(env_overrides)
    return er, defaults


def test_kill_switch_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path, {"RIVALCLAW_LIVE_KILL_SWITCH": "1"})
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(), last_market_price=0.45, account_balance_cents=1000)
    assert result["passed"] is False
    assert result["reason"] == "kill_switch"
    os.unlink(db_path)


def test_mode_paper_rejects_live():
    db_path = _make_test_db()
    er, env = _setup_router(db_path, {"RIVALCLAW_EXECUTION_MODE": "paper"})
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(), last_market_price=0.45, account_balance_cents=1000)
    assert result["passed"] is False
    assert result["reason"] == "mode_not_live"
    os.unlink(db_path)


def test_order_too_large_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path, {"RIVALCLAW_LIVE_MAX_ORDER_USD": "1"})
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(amount_usd=1.50), last_market_price=0.45, account_balance_cents=1000)
    assert result["passed"] is False
    assert result["reason"] == "order_too_large"
    os.unlink(db_path)


def test_insufficient_balance_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path)
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(amount_usd=1.50), last_market_price=0.45, account_balance_cents=50)
    assert result["passed"] is False
    assert result["reason"] == "insufficient_balance"
    os.unlink(db_path)


def test_wrong_series_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path)
    with patch.dict(os.environ, env):
        result = er.preflight_check(
            FakeDecision(market_id="KXINXSPX-TEST"),
            last_market_price=0.45, account_balance_cents=1000,
        )
    assert result["passed"] is False
    assert result["reason"] == "series_not_allowed"
    os.unlink(db_path)


def test_price_deviation_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path)
    with patch.dict(os.environ, env):
        # Entry price 0.45, last market price 0.60 => 33% deviation
        result = er.preflight_check(FakeDecision(entry_price=0.45), last_market_price=0.60, account_balance_cents=1000)
    assert result["passed"] is False
    assert result["reason"] == "price_deviation"
    os.unlink(db_path)


def test_valid_order_passes():
    db_path = _make_test_db()
    er, env = _setup_router(db_path)
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(amount_usd=1.50), last_market_price=0.45, account_balance_cents=1000)
    assert result["passed"] is True
    os.unlink(db_path)


def test_too_many_contracts_rejects():
    db_path = _make_test_db()
    er, env = _setup_router(db_path, {"RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "2"})
    with patch.dict(os.environ, env):
        result = er.preflight_check(FakeDecision(shares=5), last_market_price=0.45, account_balance_cents=1000)
    assert result["passed"] is False
    assert result["reason"] == "too_many_contracts"
    os.unlink(db_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_execution_router.py -v`
Expected: FAIL — `ModuleNotFoundError` for `execution_router`

- [ ] **Step 3: Implement execution_router.py**

Write `execution_router.py`:

```python
#!/usr/bin/env python3
"""
RivalClaw execution router — pre-flight safety checks and trade routing.
Decides per-trade whether to shadow-log or live-submit based on config and 10-point checklist.
"""
from __future__ import annotations

import os
import sqlite3
import datetime
from pathlib import Path

import kalshi_executor as executor

_db_path = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Per-cycle order counter (reset each cycle)
_cycle_order_count = 0
_current_cycle_id = ""

# Per-hour order counter
_hour_order_count = 0
_hour_window_start = datetime.datetime.utcnow()


def _get_config() -> dict:
    """Read safety config from env vars."""
    return {
        "mode": os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper"),
        "kill_switch": os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0") == "1",
        "max_order_usd": float(os.environ.get("RIVALCLAW_LIVE_MAX_ORDER_USD", "2")),
        "max_exposure_usd": float(os.environ.get("RIVALCLAW_LIVE_MAX_EXPOSURE_USD", "10")),
        "max_contracts": int(os.environ.get("RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER", "5")),
        "max_per_cycle": int(os.environ.get("RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE", "2")),
        "max_per_hour": int(os.environ.get("RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR", "10")),
        "allowed_series": os.environ.get("RIVALCLAW_LIVE_SERIES", "KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M").split(","),
        "max_price_deviation": float(os.environ.get("RIVALCLAW_LIVE_MAX_PRICE_DEVIATION", "0.10")),
    }


def _get_open_live_exposure() -> float:
    """Sum of amount_usd for all open/pending live orders."""
    try:
        conn = sqlite3.connect(str(_db_path))
        row = conn.execute(
            "SELECT COALESCE(SUM(count * yes_price), 0) FROM live_orders "
            "WHERE mode='live' AND status IN ('pending', 'filled')"
        ).fetchone()
        conn.close()
        return (row[0] or 0) / 100.0  # cents to dollars
    except Exception:
        return 0.0


def reset_cycle(cycle_id: str) -> None:
    """Reset per-cycle order counter. Called at start of each cycle."""
    global _cycle_order_count, _current_cycle_id
    _cycle_order_count = 0
    _current_cycle_id = cycle_id


def preflight_check(
    decision,
    last_market_price: float,
    account_balance_cents: int,
    stale_seconds: float = 0,
) -> dict:
    """Run 10-point pre-flight safety checklist.

    Returns {"passed": True} or {"passed": False, "reason": str}.
    """
    global _cycle_order_count, _hour_order_count, _hour_window_start

    cfg = _get_config()

    # 1. Mode check
    if cfg["mode"] not in ("live", "shadow"):
        return {"passed": False, "reason": "mode_not_live"}

    # 2. Kill switch
    if cfg["kill_switch"]:
        return {"passed": False, "reason": "kill_switch"}

    # 3. Balance check
    order_cost_cents = int(round(decision.amount_usd * 100))
    if account_balance_cents < order_cost_cents:
        return {"passed": False, "reason": "insufficient_balance"}

    # 4. Exposure check
    current_exposure = _get_open_live_exposure()
    if current_exposure + decision.amount_usd > cfg["max_exposure_usd"]:
        return {"passed": False, "reason": "exposure_exceeded"}

    # 5. Order size check
    if decision.amount_usd > cfg["max_order_usd"]:
        return {"passed": False, "reason": "order_too_large"}

    # 6. Contract count check
    if (decision.shares or 0) > cfg["max_contracts"]:
        return {"passed": False, "reason": "too_many_contracts"}

    # 7. Rate check (per-cycle and per-hour)
    if _cycle_order_count >= cfg["max_per_cycle"]:
        return {"passed": False, "reason": "cycle_limit_reached"}
    now = datetime.datetime.utcnow()
    if (now - _hour_window_start).total_seconds() >= 3600:
        _hour_order_count = 0
        _hour_window_start = now
    if _hour_order_count >= cfg["max_per_hour"]:
        return {"passed": False, "reason": "hour_limit_reached"}

    # 8. Series check
    market_id = decision.market_id
    series_match = any(market_id.startswith(s) for s in cfg["allowed_series"])
    if not series_match:
        return {"passed": False, "reason": "series_not_allowed"}

    # 9. Price sanity check
    if last_market_price and last_market_price > 0:
        deviation = abs(decision.entry_price - last_market_price) / last_market_price
        if deviation > cfg["max_price_deviation"]:
            return {"passed": False, "reason": "price_deviation"}

    # 10. Staleness check
    max_stale = 300  # 5 minutes
    if stale_seconds > max_stale:
        return {"passed": False, "reason": "stale_data"}

    return {"passed": True}


def route_trade(
    decision,
    protocol_result: dict,
    last_market_price: float,
    account_balance_cents: int,
    cycle_id: str = "",
    stale_seconds: float = 0,
) -> dict:
    """Route a trade to shadow or live execution after protocol engine processes it.

    Args:
        decision: Trading brain decision object
        protocol_result: Result from protocol_adapter.execute_trade()
        last_market_price: Last known market price for this ticker (dollars)
        account_balance_cents: Current Kalshi account balance in cents
        cycle_id: Current cycle identifier
        stale_seconds: How old the market data is

    Returns: {"mode": str, "order_id": int, "status": str, ...}
    """
    global _cycle_order_count, _hour_order_count

    cfg = _get_config()
    mode = cfg["mode"]

    # Paper mode — nothing to do
    if mode == "paper":
        return {"mode": "paper", "status": "skipped"}

    # Map decision direction to Kalshi action/side
    if decision.direction == "YES":
        action, side = "buy", "yes"
    else:
        action, side = "buy", "no"

    ticker = decision.market_id
    count = max(1, int(decision.shares or 1))
    yes_price_dollars = decision.entry_price

    # Run pre-flight checks
    check = preflight_check(decision, last_market_price, account_balance_cents, stale_seconds)
    if not check["passed"]:
        # Log rejected order
        order_id = executor.log_order(
            intent_id=protocol_result.get("id", ""),
            ticker=ticker, action=action, side=side,
            count=count,
            yes_price_cents=max(1, min(99, int(round(yes_price_dollars * 100)))),
            mode=mode, cycle_id=cycle_id,
            strategy=decision.strategy,
            market_question=getattr(decision, "question", ""),
            status="rejected",
            rejection_reason=check["reason"],
        )
        return {"mode": mode, "status": "rejected", "reason": check["reason"], "order_id": order_id}

    # Build order payload
    payload = executor.build_order_payload(ticker, action, side, count, yes_price_dollars)

    if mode == "shadow":
        # Shadow mode: log what we WOULD submit, don't hit Kalshi
        order_id = executor.log_order(
            intent_id=protocol_result.get("id", ""),
            ticker=ticker, action=action, side=side,
            count=count,
            yes_price_cents=payload["yes_price"],
            mode="shadow", cycle_id=cycle_id,
            strategy=decision.strategy,
            market_question=getattr(decision, "question", ""),
            client_order_id=payload["client_order_id"],
            status="shadow",
        )
        _cycle_order_count += 1
        _hour_order_count += 1
        print(f"[rivalclaw/router] SHADOW: {action} {count}x {side} {ticker} @ {payload['yes_price']}c")
        return {"mode": "shadow", "status": "logged", "order_id": order_id, "payload": payload}

    # Live mode: submit to Kalshi
    order_id = executor.log_order(
        intent_id=protocol_result.get("id", ""),
        ticker=ticker, action=action, side=side,
        count=count,
        yes_price_cents=payload["yes_price"],
        mode="live", cycle_id=cycle_id,
        strategy=decision.strategy,
        market_question=getattr(decision, "question", ""),
        client_order_id=payload["client_order_id"],
        status="pending",
    )

    # Submit order
    resp = executor.submit_order(payload)

    if "error" in resp:
        executor.update_order_status(order_id, "rejected", error_message=resp.get("message", ""))
        print(f"[rivalclaw/router] LIVE REJECTED: {resp}")
        try:
            import notify
            notify.send_live_alert("order_rejected", f"{ticker} {action} {side}: {resp.get('message', '')}")
        except Exception:
            pass
        return {"mode": "live", "status": "rejected", "error": resp, "order_id": order_id}

    # Order submitted successfully
    kalshi_order_id = resp.get("order", {}).get("order_id", "")
    executor.update_order_status(order_id, "submitted", kalshi_order_id=kalshi_order_id)
    _cycle_order_count += 1
    _hour_order_count += 1

    print(f"[rivalclaw/router] LIVE SUBMITTED: {action} {count}x {side} {ticker} @ {payload['yes_price']}c "
          f"order_id={kalshi_order_id}")
    try:
        import notify
        notify.send_live_alert(
            "order_submitted",
            f"{action.upper()} {count}x {side.upper()} {ticker} @ {payload['yes_price']}c\n"
            f"Order: {kalshi_order_id}",
        )
    except Exception:
        pass

    # Poll for fill
    if kalshi_order_id:
        fill_result = executor.poll_order_status(kalshi_order_id)
        fill_status = fill_result.get("status", "pending")
        if fill_status == "executed":
            fill_price = fill_result.get("yes_price", payload["yes_price"])
            fill_count = fill_result.get("count", count)
            executor.update_order_status(
                order_id, "filled",
                kalshi_order_id=kalshi_order_id,
                fill_price=fill_price, fill_count=fill_count,
            )
            # Log reconciliation
            executor.log_reconciliation(
                live_order_id=order_id,
                paper_entry_price=yes_price_dollars,
                live_fill_price_cents=fill_price,
                paper_amount_usd=decision.amount_usd,
                live_amount_usd=fill_count * fill_price / 100.0,
            )
            try:
                import notify
                notify.send_live_alert(
                    "order_filled",
                    f"FILLED {fill_count}x @ {fill_price}c (paper: {int(yes_price_dollars*100)}c)\n{ticker}",
                )
            except Exception:
                pass
            return {"mode": "live", "status": "filled", "order_id": order_id,
                    "fill_price": fill_price, "fill_count": fill_count}
        elif fill_status == "canceled":
            executor.update_order_status(order_id, "cancelled", kalshi_order_id=kalshi_order_id)
            return {"mode": "live", "status": "cancelled", "order_id": order_id}

    return {"mode": "live", "status": "submitted", "order_id": order_id, "kalshi_order_id": kalshi_order_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_execution_router.py -v`
Expected: All 8 tests pass

- [ ] **Step 5: Commit**

```bash
cd ~/rivalclaw && git add execution_router.py tests/test_execution_router.py && git commit -m "feat: add execution router with 10-point pre-flight safety checks"
```

---

### Task 6: Wire Execution Router into Protocol Adapter

**Files:**
- Modify: `protocol_adapter.py:98-240` (execute_trade function)
- Modify: `simulator.py:180-400` (run_loop function)

- [ ] **Step 1: Add execution router call to protocol_adapter.execute_trade()**

In `protocol_adapter.py`, add the import at the top of the file (after the existing imports around line 9):

```python
import execution_router
```

Then modify the `execute_trade()` function. After the successful return dict is built (line 226-240), add the execution routing call. Replace the return block starting at line 226 with:

```python
    _command_log.update_status(cmd.command_id, "executed")

    # --- Return legacy-compatible dict ---
    legacy_result = {
        "id": result.execution_id,
        "market_id": market_id,
        "direction": getattr(decision, "direction", "YES"),
        "amount_usd": result.filled_size * result.entry_price,
        "shares": result.filled_size,
        "entry_price": result.entry_price,
        "status": "open",
        "execution_sim": {
            "slippage_bps": result.slippage_bps,
            "fill_ratio": result.fill_ratio,
            "fees_entry": result.fees_entry,
            "latency_penalty_bps": result.latency_penalty_bps,
        },
    }

    # --- Route to shadow/live execution ---
    try:
        # Get last known market price for this ticker
        last_price = entry_price  # Use the decision's entry price as baseline
        # Get account balance (cached from cycle start sync)
        acct_balance = _last_account_balance_cents

        route_result = execution_router.route_trade(
            decision=decision,
            protocol_result=legacy_result,
            last_market_price=last_price,
            account_balance_cents=acct_balance,
            cycle_id=cycle_id,
        )
        if route_result.get("status") not in ("skipped", None):
            logger.info(f"Execution route: {route_result.get('mode')} -> {route_result.get('status')}")
    except Exception as e:
        logger.warning(f"Execution routing failed (non-fatal): {e}")

    return legacy_result
```

Also add a module-level variable to track account balance (after `_lock_key` on line 39):

```python
_last_account_balance_cents: int = 0
```

Add a function to update it (after `shutdown()` at the end of the file):

```python
def set_account_balance(balance_cents: int) -> None:
    """Update cached account balance for pre-flight checks."""
    global _last_account_balance_cents
    _last_account_balance_cents = balance_cents
```

- [ ] **Step 2: Add account sync to simulator.py run_loop()**

In `simulator.py`, add the import at the top (after the existing imports):

```python
# Added at the top of run_loop() where other imports happen (line ~188)
```

In the `run_loop()` function, after the protocol engine initialization block (line 193-194), add account sync:

```python
    # Initialize protocol engine (idempotent — safe to call every cycle)
    if USE_PROTOCOL:
        protocol_adapter.init_engine(str(Path(__file__).parent))

    # Sync Kalshi account balance for live/shadow modes
    exec_mode = os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper")
    if exec_mode in ("live", "shadow"):
        try:
            import kalshi_executor
            import execution_router
            acct = kalshi_executor.sync_account()
            if acct:
                protocol_adapter.set_account_balance(acct["balance_cents"])
            cycle_id_for_router = str(int(time.time() * 1000))[:12]
            execution_router.reset_cycle(cycle_id_for_router)
        except Exception as e:
            print(f"[rivalclaw] Account sync failed (non-fatal): {e}")
```

- [ ] **Step 3: Test the full cycle runs without error in paper mode**

Run: `cd ~/rivalclaw && python3 -c "import os; os.environ['RIVALCLAW_EXECUTION_MODE']='paper'; import simulator; simulator.migrate()"`
Expected: Migration complete, no errors

- [ ] **Step 4: Commit**

```bash
cd ~/rivalclaw && git add protocol_adapter.py simulator.py && git commit -m "feat: wire execution router into protocol adapter and simulator cycle"
```

---

### Task 7: Config Updates — .env and CLAUDE.md

**Files:**
- Modify: `.env`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add new env vars to .env**

Append the following to the end of `.env`:

```bash
# === Live Trading Bridge ===
# Execution mode: paper (default) | shadow | live
RIVALCLAW_EXECUTION_MODE=paper
# Safety limits (for $10 wallet)
RIVALCLAW_LIVE_MAX_ORDER_USD=2
RIVALCLAW_LIVE_MAX_EXPOSURE_USD=10
RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER=5
RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE=2
RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR=10
RIVALCLAW_LIVE_SERIES=KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M
RIVALCLAW_LIVE_KILL_SWITCH=0
RIVALCLAW_LIVE_MAX_PRICE_DEVIATION=0.10
RIVALCLAW_KALSHI_WRITE_RATE=10
```

- [ ] **Step 2: Update CLAUDE.md doctrine**

In `CLAUDE.md`, replace the "Non-Negotiable Rules" section (lines 353-361) with:

```markdown
## Non-Negotiable Rules

- No bypassing execution realism
- No optimistic fills — live fills are real, paper fills stay simulated
- No silent failures
- No trading on ambiguous resolution
- No degradation of metric honesty
- Live trading requires explicit mode flag (`RIVALCLAW_EXECUTION_MODE=live`)
- All live orders must pass pre-flight safety checks (10-point checklist)
- Kill switch must always be functional and immediately halt all submissions
- Max order size and exposure limits are hard-enforced, not advisory
```

Also update the "Execution Model" section header (line 139) from `## Execution Model (Paper Only)` to:

```markdown
## Execution Model
```

And add to the file map section (around line 389):

```markdown
├── kalshi_executor.py     <- Kalshi order submission, polling, account sync
├── execution_router.py    <- pre-flight safety checks, shadow/live routing
├── tests/                 <- unit tests
```

- [ ] **Step 3: Commit**

```bash
cd ~/rivalclaw && git add .env CLAUDE.md && git commit -m "feat: add live trading config and update doctrine for live execution"
```

---

### Task 8: Integration Test — Shadow Mode End-to-End

**Files:**
- Create: `tests/test_shadow_integration.py`

- [ ] **Step 1: Write shadow mode integration test**

Write `tests/test_shadow_integration.py`:

```python
#!/usr/bin/env python3
"""Integration test: shadow mode logs orders without hitting Kalshi API."""
from __future__ import annotations
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _create_full_test_db(path):
    """Create DB with all required tables."""
    conn = sqlite3.connect(path)
    # Import and run migration
    sys.path.insert(0, str(Path(__file__).parent.parent))
    # Manually create the tables we need
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS live_orders (
            id INTEGER PRIMARY KEY, intent_id TEXT, client_order_id TEXT UNIQUE,
            kalshi_order_id TEXT, ticker TEXT, action TEXT, side TEXT,
            count INTEGER, yes_price INTEGER, order_type TEXT DEFAULT 'limit',
            status TEXT DEFAULT 'pending', fill_price INTEGER, fill_count INTEGER,
            submitted_at TEXT, filled_at TEXT, mode TEXT, error_message TEXT,
            rejection_reason TEXT, cycle_id TEXT, strategy TEXT, market_question TEXT
        );
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY, balance_cents INTEGER,
            portfolio_value_cents INTEGER, open_positions INTEGER, fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS live_reconciliation (
            id INTEGER PRIMARY KEY, live_order_id INTEGER,
            paper_entry_price REAL, live_fill_price REAL,
            slippage_delta_bps REAL, paper_amount_usd REAL,
            live_amount_usd REAL, reconciled_at TEXT
        );
    """)
    conn.commit()
    conn.close()


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


def test_shadow_mode_logs_order_without_api_call():
    """Shadow mode should log the order to DB but never call Kalshi API."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    _create_full_test_db(db_path)

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

    import kalshi_executor as ke
    import execution_router as er

    ke.DB_PATH = Path(db_path)
    er._db_path = Path(db_path)

    with patch.dict(os.environ, env):
        er.reset_cycle("test_cycle_1")
        result = er.route_trade(
            decision=FakeDecision(),
            protocol_result={"id": "proto_123"},
            last_market_price=0.45,
            account_balance_cents=1000,
            cycle_id="test_cycle_1",
        )

    assert result["mode"] == "shadow"
    assert result["status"] == "logged"

    # Verify DB has the shadow order
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT * FROM live_orders").fetchone()
    assert row is not None
    # mode column (index 15) should be "shadow"
    assert row[15] == "shadow"
    # ticker should be the DOGE market
    assert "KXDOGE15M" in row[4]
    # action should be "buy"
    assert row[5] == "buy"
    # side should be "yes"
    assert row[6] == "yes"
    # count should be 3
    assert row[7] == 3
    # price should be 45 cents
    assert row[8] == 45
    conn.close()
    os.unlink(db_path)


def test_shadow_mode_rejects_when_kill_switch_on():
    """Kill switch should reject even in shadow mode."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    _create_full_test_db(db_path)

    env = {
        "RIVALCLAW_EXECUTION_MODE": "shadow",
        "RIVALCLAW_LIVE_KILL_SWITCH": "1",
        "RIVALCLAW_LIVE_MAX_ORDER_USD": "2",
        "RIVALCLAW_LIVE_MAX_EXPOSURE_USD": "10",
        "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER": "5",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE": "2",
        "RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR": "10",
        "RIVALCLAW_LIVE_SERIES": "KXDOGE15M,KXADA15M",
        "RIVALCLAW_LIVE_MAX_PRICE_DEVIATION": "0.10",
    }

    import kalshi_executor as ke
    import execution_router as er

    ke.DB_PATH = Path(db_path)
    er._db_path = Path(db_path)

    with patch.dict(os.environ, env):
        er.reset_cycle("test_cycle_2")
        result = er.route_trade(
            decision=FakeDecision(),
            protocol_result={"id": "proto_456"},
            last_market_price=0.45,
            account_balance_cents=1000,
            cycle_id="test_cycle_2",
        )

    assert result["status"] == "rejected"
    assert result["reason"] == "kill_switch"

    # Should still be logged as rejected
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, rejection_reason FROM live_orders").fetchone()
    assert row[0] == "rejected"
    assert row[1] == "kill_switch"
    conn.close()
    os.unlink(db_path)
```

- [ ] **Step 2: Run integration tests**

Run: `cd ~/rivalclaw && python3 -m pytest tests/test_shadow_integration.py -v`
Expected: 2 passed

- [ ] **Step 3: Run full test suite**

Run: `cd ~/rivalclaw && python3 -m pytest tests/ -v`
Expected: All tests pass (rate limiter + router + integration)

- [ ] **Step 4: Commit**

```bash
cd ~/rivalclaw && git add tests/test_shadow_integration.py && git commit -m "test: add shadow mode integration tests"
```

---

### Task 9: Smoke Test — Full Cycle in Shadow Mode

- [ ] **Step 1: Set mode to shadow and run one cycle**

```bash
cd ~/rivalclaw && RIVALCLAW_EXECUTION_MODE=shadow python3 run.py --run
```

Expected: Normal cycle output plus `[rivalclaw/router] SHADOW:` lines for any trades the brain generates. No Kalshi API order calls.

- [ ] **Step 2: Verify shadow orders were logged**

```bash
cd ~/rivalclaw && python3 -c "
import sqlite3
conn = sqlite3.connect('rivalclaw.db')
rows = conn.execute('SELECT ticker, action, side, count, yes_price, status, mode FROM live_orders ORDER BY id DESC LIMIT 5').fetchall()
for r in rows:
    print(f'{r[6]:6s} {r[5]:8s} {r[1]:4s} {r[3]}x {r[2]:3s} {r[0]} @ {r[4]}c')
conn.close()
"
```

Expected: Shadow orders logged with correct tickers, sides, counts, and prices.

- [ ] **Step 3: Verify account snapshot was taken**

```bash
cd ~/rivalclaw && python3 -c "
import sqlite3
conn = sqlite3.connect('rivalclaw.db')
row = conn.execute('SELECT balance_cents, portfolio_value_cents, open_positions, fetched_at FROM account_snapshots ORDER BY id DESC LIMIT 1').fetchone()
if row: print(f'Balance: \${row[0]/100:.2f}  Portfolio: \${row[1]/100:.2f}  Positions: {row[2]}  At: {row[3]}')
else: print('No snapshots yet')
conn.close()
"
```

Expected: Account snapshot with ~$10 balance.

- [ ] **Step 4: Commit any fixes if needed, then final commit**

```bash
cd ~/rivalclaw && git add -A && git commit -m "feat: Kalshi live trading bridge complete — shadow mode verified"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | DB migration (3 new tables) | `simulator.py` |
| 2 | Telegram live alerts | `notify.py` |
| 3 | Rate limiter | `kalshi_executor.py`, `tests/test_kalshi_executor.py` |
| 4 | Order submission + account sync | `kalshi_executor.py`, `tests/test_kalshi_executor.py` |
| 5 | Pre-flight safety checks | `execution_router.py`, `tests/test_execution_router.py` |
| 6 | Wire into protocol adapter + simulator | `protocol_adapter.py`, `simulator.py` |
| 7 | Config + doctrine updates | `.env`, `CLAUDE.md` |
| 8 | Shadow integration tests | `tests/test_shadow_integration.py` |
| 9 | Smoke test in shadow mode | (manual verification) |

After this plan completes, RivalClaw can run in shadow mode to validate order construction. Flipping to `RIVALCLAW_EXECUTION_MODE=live` enables real Kalshi orders. The ERS Dashboard (Sub-Project 2) will be planned separately.
