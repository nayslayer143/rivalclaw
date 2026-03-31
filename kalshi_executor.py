#!/usr/bin/env python3
"""
RivalClaw Kalshi executor — order management, rate limiting, and account sync.

Handles all communication with the Kalshi REST API for live order execution.
Reuses RSA authentication from kalshi_feed.py.

Env vars:
    RIVALCLAW_DB_PATH          — path to rivalclaw.db (default: ./rivalclaw.db)
    RIVALCLAW_KALSHI_WRITE_RATE — max writes per second (default: 10)
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import time
import uuid
from pathlib import Path

import requests as _requests

import kalshi_feed

try:
    from notify import send_live_alert
except ImportError:
    def send_live_alert(event: str, details: str = "") -> bool:
        print(f"[kalshi_executor] alert (notify unavailable): {event} — {details}")
        return False


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
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
        now = time.time()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._count = 0
        if self._count >= self._max:
            return False
        self._count += 1
        return True

    def usage(self) -> dict:
        return {
            "used": self._count,
            "limit": self._max,
            "remaining": max(0, self._max - self._count),
        }


_write_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
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
    """Build a Kalshi order payload dict.

    Converts dollar price to cents (clamped 1-99), generates a UUID4
    client_order_id.
    """
    raw_cents = int(round(yes_price_dollars * 100))
    yes_price_cents = max(1, min(99, raw_cents))

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
    client_order_id: str | None = None,
    kalshi_order_id: str | None = None,
    order_type: str = "limit",
    cycle_id: str | None = None,
    strategy: str | None = None,
    market_question: str | None = None,
    order_mode: str = "taker",
    brain_price: float | None = None,
) -> int:
    """Insert a row into live_orders. Returns the row id."""
    if client_order_id is None:
        client_order_id = str(uuid.uuid4())

    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO live_orders
            (intent_id, client_order_id, kalshi_order_id, ticker, action, side,
             count, yes_price, order_type, status, submitted_at, mode,
             cycle_id, strategy, market_question, order_mode, brain_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id, client_order_id, kalshi_order_id,
                ticker, action, side, count, yes_price_cents,
                order_type, now, mode,
                cycle_id, strategy, market_question,
                order_mode, brain_price,
            ),
        )
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
    rejection_reason: str | None = None,
) -> None:
    """Update a live_orders row with new status and optional fill data."""
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        sets = ["status = ?"]
        vals: list = [status]

        if kalshi_order_id is not None:
            sets.append("kalshi_order_id = ?")
            vals.append(kalshi_order_id)
        if fill_price is not None:
            sets.append("fill_price = ?")
            vals.append(fill_price)
        if fill_count is not None:
            sets.append("fill_count = ?")
            vals.append(fill_count)
        if error_message is not None:
            sets.append("error_message = ?")
            vals.append(error_message)
        if rejection_reason is not None:
            sets.append("rejection_reason = ?")
            vals.append(rejection_reason)
        if status in ("filled", "partial_fill"):
            sets.append("filled_at = ?")
            vals.append(now)

        vals.append(order_id)
        conn.execute(
            f"UPDATE live_orders SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        conn.commit()
    finally:
        conn.close()


def log_account_snapshot(
    balance_cents: int,
    portfolio_value_cents: int,
    open_positions: int,
) -> int:
    """Insert a row into account_snapshots. Returns the row id."""
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO account_snapshots
            (balance_cents, portfolio_value_cents, open_positions, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (balance_cents, portfolio_value_cents, open_positions, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def log_reconciliation(
    live_order_id: int,
    paper_entry_price: float,
    live_fill_price_cents: int,
    paper_amount_usd: float,
    live_amount_usd: float,
) -> int:
    """Insert a reconciliation row. Alerts if slippage > 500 bps."""
    live_fill_price = live_fill_price_cents / 100.0
    if paper_entry_price > 0:
        slippage_bps = abs(live_fill_price - paper_entry_price) / paper_entry_price * 10_000
    else:
        slippage_bps = 0.0

    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO live_reconciliation
            (live_order_id, paper_entry_price, live_fill_price,
             slippage_delta_bps, paper_amount_usd, live_amount_usd, reconciled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                live_order_id, paper_entry_price, live_fill_price,
                slippage_bps, paper_amount_usd, live_amount_usd, now,
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()

    if slippage_bps > 500:
        send_live_alert(
            "slippage_warning",
            f"Order {live_order_id}: {slippage_bps:.0f} bps slippage "
            f"(paper={paper_entry_price:.4f}, live={live_fill_price:.4f})",
        )

    return row_id


# ---------------------------------------------------------------------------
# Kalshi API helpers (reuse RSA auth from kalshi_feed)
# ---------------------------------------------------------------------------

def _get_kalshi_auth_headers(method: str, path: str) -> dict | None:
    """Get authenticated headers via kalshi_feed's RSA signer."""
    return kalshi_feed._auth_headers(method, path)


def _get_api_base() -> str:
    """Get the Kalshi API base URL."""
    return kalshi_feed._get_api_base()


# ---------------------------------------------------------------------------
# Kalshi API functions
# ---------------------------------------------------------------------------

def submit_order(payload: dict) -> dict:
    """POST an order to Kalshi. Exponential backoff on 429."""
    if not _write_limiter.acquire():
        send_live_alert("rate_limited", f"Local rate limit hit for {payload.get('ticker')}")
        return {"error": "rate_limited", "detail": "Local write rate limit exceeded"}

    path = "/portfolio/orders"
    max_retries = 3
    backoff = 1.0

    for attempt in range(max_retries):
        headers = _get_kalshi_auth_headers("POST", path)
        if headers is None:
            return {"error": "auth_failed", "detail": "Could not generate auth headers"}

        url = f"{_get_api_base()}{path}"
        try:
            resp = _requests.post(url, json=payload, headers=headers, timeout=30)

            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    print(f"[kalshi_executor] 429 — backing off {backoff}s (attempt {attempt + 1})")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                send_live_alert("rate_limited", f"Kalshi 429 after {max_retries} retries")
                return {"error": "rate_limited", "detail": "Kalshi 429 after retries"}

            if resp.status_code == 401:
                return {"error": "auth_failed", "detail": "401 Unauthorized"}

            resp.raise_for_status()
            return resp.json()

        except _requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            return {"error": "request_failed", "detail": str(e)}

    return {"error": "max_retries", "detail": "Exhausted retries"}


def poll_order_status(
    kalshi_order_id: str,
    max_polls: int = 3,
    interval: float = 2.0,
) -> dict:
    """Poll Kalshi for order fill status."""
    path = f"/portfolio/orders/{kalshi_order_id}"
    last_order: dict | None = None

    for i in range(max_polls):
        headers = _get_kalshi_auth_headers("GET", path)
        if headers is None:
            return {"error": "auth_failed"}

        url = f"{_get_api_base()}{path}"
        try:
            resp = _requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            last_order = data.get("order", data)
            status = last_order.get("status", "")
            if status in ("executed", "canceled", "cancelled"):
                return last_order
            if i < max_polls - 1:
                time.sleep(interval)
        except _requests.exceptions.RequestException as e:
            if i < max_polls - 1:
                time.sleep(interval)
                continue
            return {"error": "request_failed", "detail": str(e)}

    return last_order if last_order is not None else {"error": "timeout"}


def cancel_order(kalshi_order_id: str) -> dict:
    """DELETE a single resting order."""
    path = f"/portfolio/orders/{kalshi_order_id}"
    headers = _get_kalshi_auth_headers("DELETE", path)
    if headers is None:
        return {"error": "auth_failed"}

    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.delete(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "cancelled"}
    except _requests.exceptions.RequestException as e:
        return {"error": "request_failed", "detail": str(e)}


def poll_or_cancel(
    kalshi_order_id: str,
    patience_sec: float = 120,
    poll_interval: float = 10.0,
) -> dict:
    """Poll a resting order for fills. Cancel if not filled within patience window.

    Returns the final order status dict. If cancelled due to timeout,
    status will be 'cancelled_timeout'.
    """
    deadline = time.time() + patience_sec
    last_result: dict = {}

    while time.time() < deadline:
        result = poll_order_status(kalshi_order_id, max_polls=1, interval=0)
        last_result = result
        status = result.get("status", "")
        if status in ("executed", "canceled", "cancelled"):
            return result
        if "error" in result:
            return result
        time.sleep(poll_interval)

    # Timed out — cancel the resting order
    cancel_result = cancel_order(kalshi_order_id)
    if "error" in cancel_result:
        logger.warning("Failed to cancel timed-out order %s: %s", kalshi_order_id, cancel_result)
    return {"status": "cancelled_timeout", "order_id": kalshi_order_id, "last_poll": last_result}


def cancel_resting_for_ticker(ticker: str) -> int:
    """Cancel all resting orders for a given ticker. Returns count cancelled."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT kalshi_order_id FROM live_orders "
            "WHERE ticker=? AND mode='live' AND status='resting' AND kalshi_order_id IS NOT NULL",
            (ticker,),
        ).fetchall()
    finally:
        conn.close()

    cancelled = 0
    for row in rows:
        oid = row["kalshi_order_id"]
        result = cancel_order(oid)
        if "error" not in result:
            update_order_status_by_kalshi_id(oid, "cancelled")
            cancelled += 1
    return cancelled


def update_order_status_by_kalshi_id(kalshi_order_id: str, status: str):
    """Update live_orders status by Kalshi order ID."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE live_orders SET status=? WHERE kalshi_order_id=?",
            (status, kalshi_order_id),
        )
        conn.commit()
    finally:
        conn.close()


def batch_cancel_orders() -> dict:
    """DELETE all resting orders."""
    path = "/portfolio/orders"
    headers = _get_kalshi_auth_headers("DELETE", path)
    if headers is None:
        return {"error": "auth_failed"}

    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.delete(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "all_cancelled"}
    except _requests.exceptions.RequestException as e:
        return {"error": "request_failed", "detail": str(e)}


def get_balance() -> dict:
    """GET account balance."""
    path = "/portfolio/balance"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return {"error": "auth_failed"}

    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except _requests.exceptions.RequestException as e:
        return {"error": "request_failed", "detail": str(e)}


def get_positions() -> dict:
    """GET portfolio positions."""
    path = "/portfolio/positions"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return {"error": "auth_failed"}

    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except _requests.exceptions.RequestException as e:
        return {"error": "request_failed", "detail": str(e)}


def get_fills(limit: int = 50) -> dict:
    """GET recent order fills."""
    path = "/portfolio/fills"
    headers = _get_kalshi_auth_headers("GET", path)
    if headers is None:
        return {"error": "auth_failed"}

    url = f"{_get_api_base()}{path}"
    try:
        resp = _requests.get(url, headers=headers, params={"limit": limit}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except _requests.exceptions.RequestException as e:
        return {"error": "request_failed", "detail": str(e)}


def reconcile_resting_orders() -> int:
    """Check all 'resting' live_orders against Kalshi and update status if executed/cancelled.
    Returns number of orders updated."""
    import kalshi_feed as _kf
    conn = _get_conn()
    updated = 0
    try:
        resting = conn.execute(
            "SELECT id, kalshi_order_id FROM live_orders WHERE status='resting' AND kalshi_order_id IS NOT NULL"
        ).fetchall()
        for row in resting:
            path = f"/portfolio/orders/{row['kalshi_order_id']}"
            headers = _kf._auth_headers("GET", path)
            if not headers:
                continue
            try:
                import requests as _r
                resp = _r.get(f"{_kf._get_api_base()}{path}", headers=headers, timeout=10)
                if resp.status_code == 200:
                    kalshi_status = resp.json().get("order", {}).get("status", "")
                    if kalshi_status in ("executed",):
                        conn.execute("UPDATE live_orders SET status='filled' WHERE id=?", (row["id"],))
                        updated += 1
                    elif kalshi_status in ("cancelled", "canceled", "expired"):
                        conn.execute("UPDATE live_orders SET status='cancelled' WHERE id=?", (row["id"],))
                        updated += 1
                elif resp.status_code == 404:
                    conn.execute("UPDATE live_orders SET status='cancelled' WHERE id=?", (row["id"],))
                    updated += 1
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return updated


def migrate_maker_columns():
    """Add maker tracking columns to live_orders if not present."""
    conn = _get_conn()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(live_orders)").fetchall()}
        if "order_mode" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN order_mode TEXT DEFAULT 'taker'")
        if "brain_price" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN brain_price REAL")
        if "maker_savings" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN maker_savings REAL")
        if "fill_time_sec" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN fill_time_sec REAL")
        conn.commit()
    finally:
        conn.close()


def get_maker_fill_rate(series_prefix: str, lookback: int = 20) -> float:
    """Get rolling fill rate for maker orders on a series. Returns 0.0-1.0."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT status FROM live_orders "
            "WHERE order_mode='maker' AND ticker LIKE ? "
            "ORDER BY rowid DESC LIMIT ?",
            (f"{series_prefix}%", lookback),
        ).fetchall()
        if not rows:
            return 1.0  # No history — assume maker is viable
        filled = sum(1 for r in rows if r["status"] == "filled")
        return filled / len(rows)
    finally:
        conn.close()


def get_resting_count() -> int:
    """Count currently resting live orders."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM live_orders WHERE mode='live' AND status='resting'"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def sync_account() -> dict:
    """Fetch balance + positions, log snapshot, return combined dict."""
    balance_data = get_balance()
    positions_data = get_positions()

    if "error" in balance_data or "error" in positions_data:
        return {
            "error": "sync_failed",
            "balance": balance_data,
            "positions": positions_data,
        }

    balance_cents = balance_data.get("balance", 0)
    portfolio_val = balance_data.get("portfolio_value", 0)
    positions_list = positions_data.get("market_positions", [])
    open_count = len([p for p in positions_list if p.get("position", 0) != 0])

    # Reconcile any stale resting orders
    reconcile_resting_orders()

    log_account_snapshot(balance_cents, portfolio_val, open_count)

    return {
        "balance_cents": balance_cents,
        "portfolio_value_cents": portfolio_val,
        "open_positions": open_count,
        "positions": positions_list,
    }


def get_rate_limit_usage() -> dict:
    """Return current rate limiter usage."""
    return _write_limiter.usage()
