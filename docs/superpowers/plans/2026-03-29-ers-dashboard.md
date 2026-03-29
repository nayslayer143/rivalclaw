# ERS Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private trading dashboard at eternalrevenueservice.com that displays RivalClaw trading data, proxies Kalshi API calls, and provides live/shadow/paper mode controls.

**Architecture:** A FastAPI bridge server runs on the Mac alongside rivalclaw, reading rivalclaw.db directly and proxying Kalshi API calls via RSA auth. Cloudflare Tunnel exposes the bridge to the internet. A Next.js App Router dashboard hosted on Vercel consumes the bridge API, gated behind NextAuth.js credentials auth. All data flows: Vercel -> Cloudflare Tunnel -> FastAPI bridge -> rivalclaw.db / Kalshi API.

**Tech Stack:** Python 3.9 + FastAPI + uvicorn (bridge), Next.js 14 + TypeScript + App Router (dashboard), Tailwind CSS, Recharts, Lightweight Charts (TradingView), NextAuth.js, TanStack Query, Lucide React, next-themes, Cloudflare Tunnel, Vercel hosting.

---

## Task 1: FastAPI Bridge Server

**Files:**
- `~/rivalclaw/bridge/server.py`
- `~/rivalclaw/bridge/auth.py`
- `~/rivalclaw/bridge/db_routes.py`
- `~/rivalclaw/bridge/kalshi_routes.py`
- `~/rivalclaw/bridge/control_routes.py`
- `~/rivalclaw/bridge/run.sh`

### Steps

- [ ] **1.1** Create bridge directory and `__init__.py`

```bash
mkdir -p ~/rivalclaw/bridge
touch ~/rivalclaw/bridge/__init__.py
```

- [ ] **1.2** Create `bridge/auth.py` — Bearer token middleware

```python
# ~/rivalclaw/bridge/auth.py
"""Bearer token authentication middleware for the ERS bridge."""
from __future__ import annotations

import os
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

API_KEY = os.environ.get("ERS_BRIDGE_API_KEY", "")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token."""

    async def dispatch(self, request: Request, call_next):
        # Allow health check without auth
        if request.url.path == "/health":
            return await call_next(request)

        if not API_KEY:
            raise HTTPException(status_code=500, detail="ERS_BRIDGE_API_KEY not configured")

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")

        token = auth_header[7:]
        if token != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid token")

        return await call_next(request)
```

- [ ] **1.3** Create `bridge/db_routes.py` — all database read endpoints

```python
# ~/rivalclaw/bridge/db_routes.py
"""Database read endpoints — queries rivalclaw.db directly."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/db", tags=["database"])

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).resolve().parent.parent / "rivalclaw.db"))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


@router.get("/wallet")
def get_wallet():
    """Paper wallet state: balance, open positions, recent P&L."""
    conn = _get_conn()
    try:
        # Get balance from context table
        row = conn.execute(
            "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
        ).fetchone()
        starting_balance = float(row["value"]) if row else 10000.0

        # Calculate current balance from trades
        realized = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM paper_trades WHERE status='closed'"
        ).fetchone()
        total_pnl = realized["total_pnl"] if realized else 0.0

        # Open positions
        open_trades = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount_usd), 0) as exposure FROM paper_trades WHERE status='open'"
        ).fetchone()

        # Today's P&L
        today_row = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 1"
        ).fetchone()

        return {
            "starting_balance": starting_balance,
            "current_balance": starting_balance + total_pnl,
            "total_realized_pnl": total_pnl,
            "open_positions": open_trades["cnt"],
            "open_exposure": open_trades["exposure"],
            "today": dict(today_row) if today_row else None,
        }
    finally:
        conn.close()


@router.get("/trades")
def get_trades(
    status: Optional[str] = Query(None, regex="^(open|closed)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paper trade history."""
    conn = _get_conn()
    try:
        where = ""
        params: list = []
        if status:
            where = "WHERE status = ?"
            params.append(status)

        params.extend([limit, offset])
        rows = conn.execute(
            f"SELECT * FROM paper_trades {where} ORDER BY opened_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return {"trades": _rows_to_dicts(rows), "count": len(rows)}
    finally:
        conn.close()


@router.get("/live-orders")
def get_live_orders(
    mode: Optional[str] = Query(None, regex="^(shadow|live)$"),
    limit: int = Query(50, ge=1, le=500),
):
    """Live/shadow order log."""
    conn = _get_conn()
    try:
        where = ""
        params: list = []
        if mode:
            where = "WHERE mode = ?"
            params.append(mode)

        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM live_orders {where} ORDER BY submitted_at DESC LIMIT ?",
            params,
        ).fetchall()
        return {"orders": _rows_to_dicts(rows), "count": len(rows)}
    finally:
        conn.close()


@router.get("/reconciliation")
def get_reconciliation():
    """Paper vs live fill comparison."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT r.*, lo.ticker, lo.action, lo.side, lo.count, lo.mode
            FROM live_reconciliation r
            JOIN live_orders lo ON r.live_order_id = lo.id
            ORDER BY r.reconciled_at DESC
            LIMIT 100
            """
        ).fetchall()
        return {"reconciliation": _rows_to_dicts(rows)}
    finally:
        conn.close()


@router.get("/strategies")
def get_strategies():
    """Per-strategy performance aggregates from paper_trades."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                strategy,
                COUNT(*) as total_trades,
                SUM(CASE WHEN status='closed' AND pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='closed' AND pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_count,
                COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END), 0) as total_pnl,
                COALESCE(AVG(CASE WHEN status='closed' THEN pnl END), 0) as avg_pnl,
                ROUND(
                    CAST(SUM(CASE WHEN status='closed' AND pnl > 0 THEN 1 ELSE 0 END) AS REAL) /
                    NULLIF(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END), 0),
                    4
                ) as win_rate,
                COALESCE(AVG(CASE WHEN status='closed' THEN expected_edge END), 0) as avg_expected_edge,
                MIN(opened_at) as first_trade,
                MAX(opened_at) as last_trade
            FROM paper_trades
            GROUP BY strategy
            ORDER BY total_pnl DESC
            """
        ).fetchall()
        return {"strategies": _rows_to_dicts(rows)}
    finally:
        conn.close()


@router.get("/cycles")
def get_cycles(limit: int = Query(50, ge=1, le=500)):
    """Cycle timing metrics."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cycle_metrics ORDER BY cycle_started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"cycles": _rows_to_dicts(rows)}
    finally:
        conn.close()


@router.get("/daily-pnl")
def get_daily_pnl():
    """Daily P&L snapshots."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM daily_pnl ORDER BY date ASC"
        ).fetchall()
        return {"daily_pnl": _rows_to_dicts(rows)}
    finally:
        conn.close()


@router.get("/errors")
def get_errors(limit: int = Query(100, ge=1, le=500)):
    """Error events from protocol_events.db."""
    events_db = Path(os.environ.get(
        "RIVALCLAW_EVENTS_DB_PATH",
        Path(__file__).resolve().parent.parent / "protocol_events.db",
    ))
    conn = sqlite3.connect(str(events_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, event_id, event_type, timestamp_ms, cycle_id, payload
            FROM protocol_events
            WHERE event_type = 'error'
            ORDER BY timestamp_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"errors": _rows_to_dicts(rows)}
    except sqlite3.OperationalError:
        return {"errors": [], "note": "protocol_events table not available"}
    finally:
        conn.close()


@router.get("/account-snapshots")
def get_account_snapshots():
    """Kalshi account snapshot history."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY fetched_at DESC LIMIT 100"
        ).fetchall()
        return {"snapshots": _rows_to_dicts(rows)}
    finally:
        conn.close()


@router.get("/market-data")
def get_market_data(
    venue: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Cached market data."""
    conn = _get_conn()
    try:
        where = ""
        params: list = []
        if venue:
            where = "WHERE venue = ?"
            params.append(venue)

        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM market_data {where} ORDER BY fetched_at DESC LIMIT ?",
            params,
        ).fetchall()
        return {"markets": _rows_to_dicts(rows)}
    finally:
        conn.close()
```

- [ ] **1.4** Create `bridge/kalshi_routes.py` — Kalshi API proxy endpoints

```python
# ~/rivalclaw/bridge/kalshi_routes.py
"""Kalshi API proxy endpoints — authenticated server-side via RSA."""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

# Add rivalclaw root to path so we can import kalshi_executor and kalshi_feed
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kalshi_executor as executor
import kalshi_feed

router = APIRouter(prefix="/api/kalshi", tags=["kalshi"])


# ---------------------------------------------------------------------------
# Pydantic models for request bodies
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    ticker: str
    action: str  # buy or sell
    side: str  # yes or no
    count: int
    yes_price: float  # dollars (0.01-0.99), converted to cents in executor

class AmendRequest(BaseModel):
    count: int | None = None
    price: float | None = None  # dollars


# ---------------------------------------------------------------------------
# Helper to make Kalshi GET requests via kalshi_feed auth
# ---------------------------------------------------------------------------

def _kalshi_get(path: str, params: dict | None = None) -> dict:
    """Make an authenticated GET to the Kalshi API."""
    import requests

    headers = kalshi_feed._auth_headers("GET", path)
    if headers is None:
        raise HTTPException(status_code=503, detail="Kalshi auth not configured")

    url = f"{kalshi_feed._get_api_base()}{path}"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Kalshi API error: {str(e)}")


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------

@router.get("/balance")
def get_balance():
    """Real Kalshi account balance."""
    result = executor.get_balance()
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/positions")
def get_positions():
    """Real Kalshi portfolio positions."""
    result = executor.get_positions()
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/orders")
def get_orders(status: Optional[str] = Query(None)):
    """Resting orders on Kalshi."""
    params = {}
    if status:
        params["status"] = status
    return _kalshi_get("/portfolio/orders", params)


@router.get("/fills")
def get_fills(limit: int = Query(50, ge=1, le=500)):
    """Recent fill history from Kalshi."""
    result = executor.get_fills(limit=limit)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/settlements")
def get_settlements(limit: int = Query(50, ge=1, le=500)):
    """Settlement history from Kalshi."""
    return _kalshi_get("/portfolio/settlements", {"limit": limit})


# ---------------------------------------------------------------------------
# Market data endpoints
# ---------------------------------------------------------------------------

@router.get("/market/{ticker}")
def get_market(ticker: str):
    """Single market data from Kalshi."""
    return _kalshi_get(f"/markets/{ticker}")


@router.get("/market/{ticker}/orderbook")
def get_orderbook(ticker: str):
    """Orderbook for a market."""
    return _kalshi_get(f"/markets/{ticker}/orderbook")


@router.get("/market/{ticker}/candlesticks")
def get_candlesticks(
    ticker: str,
    period: str = Query("1m"),
):
    """Candlestick data for a market."""
    return _kalshi_get(f"/markets/{ticker}/candlesticks", {"period": period})


@router.get("/exchange/status")
def get_exchange_status():
    """Kalshi exchange status."""
    return _kalshi_get("/exchange/status")


# ---------------------------------------------------------------------------
# Order management endpoints
# ---------------------------------------------------------------------------

@router.post("/orders")
def place_order(order: OrderRequest):
    """Place a new order on Kalshi."""
    payload = executor.build_order_payload(
        ticker=order.ticker,
        action=order.action,
        side=order.side,
        count=order.count,
        yes_price_dollars=order.yes_price,
    )

    # Log the order
    order_row_id = executor.log_order(
        intent_id=f"manual-{payload['client_order_id'][:8]}",
        ticker=order.ticker,
        action=order.action,
        side=order.side,
        count=order.count,
        yes_price_cents=payload["yes_price"],
        mode="live",
        client_order_id=payload["client_order_id"],
    )

    # Submit to Kalshi
    result = executor.submit_order(payload)
    if "error" in result:
        executor.update_order_status(order_row_id, "rejected", error_message=result.get("detail"))
        raise HTTPException(status_code=502, detail=result)

    kalshi_order_id = result.get("order", {}).get("order_id", "")
    executor.update_order_status(order_row_id, "submitted", kalshi_order_id=kalshi_order_id)

    return {"order_row_id": order_row_id, "kalshi": result}


@router.delete("/orders/{order_id}")
def cancel_single_order(order_id: str):
    """Cancel a single resting order."""
    result = executor.cancel_order(order_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.put("/orders/{order_id}")
def amend_order(order_id: str, body: AmendRequest):
    """Amend a resting order (price and/or count)."""
    import requests as _requests

    path = f"/portfolio/orders/{order_id}/amend"
    headers = kalshi_feed._auth_headers("POST", path)
    if headers is None:
        raise HTTPException(status_code=503, detail="Kalshi auth not configured")

    payload = {}
    if body.count is not None:
        payload["count"] = body.count
    if body.price is not None:
        payload["price"] = max(1, min(99, int(round(body.price * 100))))

    url = f"{kalshi_feed._get_api_base()}{path}"
    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "amended"}
    except _requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Amend failed: {str(e)}")


@router.post("/orders/batch-cancel")
def batch_cancel():
    """Cancel all resting orders."""
    result = executor.batch_cancel_orders()
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/orders/{order_id}/queue")
def get_queue_position(order_id: str):
    """Get queue position for a resting order."""
    return _kalshi_get(f"/portfolio/orders/{order_id}")
```

- [ ] **1.5** Create `bridge/control_routes.py` — control endpoints

```python
# ~/rivalclaw/bridge/control_routes.py
"""Control endpoints — read/write execution mode, kill switch, config."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/control", tags=["control"])

RIVALCLAW_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = RIVALCLAW_DIR / ".env"
DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", RIVALCLAW_DIR / "rivalclaw.db"))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _read_env() -> dict[str, str]:
    """Parse .env file into a dict."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def _write_env_var(key: str, value: str) -> None:
    """Update a single key in the .env file. Adds it if missing."""
    lines = []
    found = False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, _ = stripped.partition("=")
                if k.strip() == key:
                    lines.append(f"{key}={value}")
                    found = True
                    continue
            lines.append(line)

    if not found:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n")
    # Also update the running process env
    os.environ[key] = value


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

class ModeRequest(BaseModel):
    mode: str  # paper, shadow, live


@router.get("/mode")
def get_mode():
    """Current execution mode."""
    env = _read_env()
    mode = env.get("RIVALCLAW_EXECUTION_MODE", os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper"))
    return {"mode": mode}


@router.post("/mode")
def set_mode(body: ModeRequest):
    """Set execution mode."""
    if body.mode not in ("paper", "shadow", "live"):
        raise HTTPException(status_code=400, detail="Mode must be paper, shadow, or live")

    _write_env_var("RIVALCLAW_EXECUTION_MODE", body.mode)

    # Also write to context table for redundancy
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO context (chat_id, key, value) VALUES ('rivalclaw', 'execution_mode', ?)",
            (body.mode,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"mode": body.mode, "status": "updated"}


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

class KillSwitchRequest(BaseModel):
    enabled: bool


@router.get("/kill-switch")
def get_kill_switch():
    """Kill switch status."""
    env = _read_env()
    val = env.get("RIVALCLAW_LIVE_KILL_SWITCH", os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0"))
    return {"enabled": val == "1"}


@router.post("/kill-switch")
def set_kill_switch(body: KillSwitchRequest):
    """Activate or deactivate the kill switch."""
    value = "1" if body.enabled else "0"
    _write_env_var("RIVALCLAW_LIVE_KILL_SWITCH", value)

    # Also write to context table
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO context (chat_id, key, value) VALUES ('rivalclaw', 'kill_switch', ?)",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"enabled": body.enabled, "status": "updated"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_KEYS = [
    "RIVALCLAW_LIVE_MAX_ORDER_USD",
    "RIVALCLAW_LIVE_MAX_EXPOSURE_USD",
    "RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER",
    "RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE",
    "RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR",
    "RIVALCLAW_LIVE_SERIES",
    "RIVALCLAW_LIVE_MAX_PRICE_DEVIATION",
    "RIVALCLAW_KALSHI_WRITE_RATE",
]


class ConfigUpdate(BaseModel):
    key: str
    value: str


@router.get("/config")
def get_config():
    """Current safety config values."""
    env = _read_env()
    config = {}
    for key in CONFIG_KEYS:
        config[key] = env.get(key, os.environ.get(key, ""))
    return {"config": config}


@router.post("/config")
def update_config(body: ConfigUpdate):
    """Update a single safety config value."""
    if body.key not in CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {body.key}")

    _write_env_var(body.key, body.value)
    return {"key": body.key, "value": body.value, "status": "updated"}


# ---------------------------------------------------------------------------
# Sync triggers
# ---------------------------------------------------------------------------

@router.post("/sync-balance")
def sync_balance():
    """Force-refresh Kalshi balance."""
    import sys
    sys.path.insert(0, str(RIVALCLAW_DIR))
    import kalshi_executor as executor

    result = executor.sync_account()
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/sync-positions")
def sync_positions():
    """Force-refresh Kalshi positions."""
    import sys
    sys.path.insert(0, str(RIVALCLAW_DIR))
    import kalshi_executor as executor

    result = executor.get_positions()
    if "error" in result:
        raise HTTPException(status_code=502, detail=result)
    return result
```

- [ ] **1.6** Create `bridge/server.py` — main FastAPI app

```python
# ~/rivalclaw/bridge/server.py
"""ERS Bridge — FastAPI server exposing rivalclaw.db and Kalshi API."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure rivalclaw root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth import BearerAuthMiddleware
from db_routes import router as db_router
from kalshi_routes import router as kalshi_router
from control_routes import router as control_router

app = FastAPI(title="ERS Bridge", version="1.0.0")

# CORS — allow the Vercel dashboard origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://eternalrevenueservice.com",
        "https://www.eternalrevenueservice.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth middleware
app.add_middleware(BearerAuthMiddleware)

# Mount routers
app.include_router(db_router)
app.include_router(kalshi_router)
app.include_router(control_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ers-bridge"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8400)
```

- [ ] **1.7** Create `bridge/run.sh` — startup script

```bash
#!/bin/bash
# ~/rivalclaw/bridge/run.sh — start the ERS bridge server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RIVALCLAW_DIR="$(dirname "$SCRIPT_DIR")"

# Load rivalclaw .env
if [ -f "$RIVALCLAW_DIR/.env" ]; then
    set -a
    source "$RIVALCDIR/.env"
    set +a
fi

# Load bridge-specific env if it exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

cd "$SCRIPT_DIR"

echo "[ERS Bridge] Starting on port 8400..."
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8400 --reload
```

Then make it executable:

```bash
chmod +x ~/rivalclaw/bridge/run.sh
```

- [ ] **1.8** Add `ERS_BRIDGE_API_KEY` to rivalclaw `.env`

Append to `~/rivalclaw/.env`:

```bash
# ERS Dashboard Bridge
ERS_BRIDGE_API_KEY=ers-$(openssl rand -hex 24)
```

Generate the key:

```bash
echo "ERS_BRIDGE_API_KEY=ers-$(openssl rand -hex 24)" >> ~/rivalclaw/.env
```

- [ ] **1.9** Fix the typo in `run.sh` and verify the server starts

The `run.sh` has a typo: `RIVALCDIR` should be `RIVALCLAW_DIR`. Fix it, then test:

```bash
# Fix the source line in run.sh
sed -i '' 's/RIVALCDIR/RIVALCLAW_DIR/' ~/rivalclaw/bridge/run.sh

# Test that the server starts (kill after confirming)
cd ~/rivalclaw/bridge && bash -c 'source ../venv/bin/activate 2>/dev/null; ERS_BRIDGE_API_KEY=test python3 -c "
import sys; sys.path.insert(0, \".\")
from server import app
print(\"Server module loads OK\")
print(\"Routes:\", [r.path for r in app.routes])
"'
```

Expected output should include route paths for `/health`, `/api/db/wallet`, `/api/kalshi/balance`, `/api/control/mode`, etc.

- [ ] **1.10** Test the wallet endpoint

```bash
cd ~/rivalclaw/bridge && bash -c '
source ../venv/bin/activate 2>/dev/null
ERS_BRIDGE_API_KEY=test python3 -m uvicorn server:app --host 127.0.0.1 --port 8400 &
SERVER_PID=$!
sleep 2
curl -s -H "Authorization: Bearer test" http://127.0.0.1:8400/api/db/wallet | python3 -m json.tool
curl -s http://127.0.0.1:8400/health | python3 -m json.tool
kill $SERVER_PID 2>/dev/null
'
```

Expected: JSON with `starting_balance`, `current_balance`, `total_realized_pnl`, `open_positions` fields.

---

## Task 2: Cloudflare Tunnel Setup

**Files:**
- `~/rivalclaw/bridge/tunnel.sh`

### Steps

- [ ] **2.1** Create the Cloudflare Tunnel

```bash
# Login to Cloudflare (opens browser)
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create ers-bridge

# Note the tunnel ID printed (e.g., abc123-def456-...)
```

- [ ] **2.2** Create tunnel config

Create `~/.cloudflared/config.yml` (or add to existing):

```yaml
# ~/.cloudflared/config.yml
tunnel: <TUNNEL_ID>
credentials-file: /Users/nayslayer/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: api.eternalrevenueservice.com
    service: http://localhost:8400
  - service: http_status:404
```

Replace `<TUNNEL_ID>` with the actual tunnel ID from step 2.1.

- [ ] **2.3** Route DNS for the tunnel

```bash
cloudflared tunnel route dns ers-bridge api.eternalrevenueservice.com
```

This creates a CNAME record in Cloudflare DNS pointing `api.eternalrevenueservice.com` to the tunnel.

- [ ] **2.4** Create `bridge/tunnel.sh` — tunnel startup script

```bash
#!/bin/bash
# ~/rivalclaw/bridge/tunnel.sh — start the Cloudflare Tunnel for the bridge

set -euo pipefail

echo "[ERS Tunnel] Starting Cloudflare Tunnel..."
exec cloudflared tunnel run ers-bridge
```

```bash
chmod +x ~/rivalclaw/bridge/tunnel.sh
```

- [ ] **2.5** DNS setup at GoDaddy

Manual steps (cannot be scripted):

1. Go to GoDaddy DNS management for eternalrevenueservice.com
2. Change nameservers to Cloudflare's (provided when you add the domain to Cloudflare)
3. In Cloudflare dashboard, add a CNAME record: `@` -> `cname.vercel-dns.com` (for the Vercel-hosted dashboard)
4. The `api` subdomain CNAME is already handled by the tunnel route in step 2.3

- [ ] **2.6** Verify tunnel connectivity

```bash
# Start bridge in background
cd ~/rivalclaw/bridge && ERS_BRIDGE_API_KEY=test bash run.sh &

# Start tunnel in background
bash ~/rivalclaw/bridge/tunnel.sh &

# Test through tunnel
sleep 5
curl -s https://api.eternalrevenueservice.com/health
```

Expected: `{"status":"ok","service":"ers-bridge"}`

---

## Task 3: Next.js Project Scaffold

**Files:**
- `~/ers-dashboard/` (entire project)

### Steps

- [ ] **3.1** Create the Next.js project

```bash
cd ~/
npx create-next-app@14 ers-dashboard \
  --typescript \
  --tailwind \
  --eslint \
  --app \
  --src-dir \
  --import-alias "@/*" \
  --no-turbopack
```

- [ ] **3.2** Install dependencies

```bash
cd ~/ers-dashboard
npm install next-auth@4 @tanstack/react-query recharts lightweight-charts lucide-react next-themes
npm install -D @types/node
```

- [ ] **3.3** Create directory structure

```bash
mkdir -p ~/ers-dashboard/src/app/trades
mkdir -p ~/ers-dashboard/src/app/markets
mkdir -p ~/ers-dashboard/src/app/strategies
mkdir -p ~/ers-dashboard/src/app/controls
mkdir -p ~/ers-dashboard/src/app/system
mkdir -p ~/ers-dashboard/src/app/api/auth/\[...nextauth\]
mkdir -p ~/ers-dashboard/src/app/api/bridge/\[...path\]
mkdir -p ~/ers-dashboard/src/components
mkdir -p ~/ers-dashboard/src/lib
```

- [ ] **3.4** Create `src/lib/api.ts` — API client

```typescript
// ~/ers-dashboard/src/lib/api.ts
const BRIDGE_URL = process.env.NEXT_PUBLIC_BRIDGE_URL || "http://localhost:8400";
const BRIDGE_KEY = process.env.ERS_BRIDGE_API_KEY || "";

export async function bridgeFetch<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BRIDGE_URL}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${BRIDGE_KEY}`,
      ...options.headers,
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`Bridge error ${res.status}: ${text}`);
  }

  return res.json();
}

// Client-side fetcher that goes through our Next.js API proxy
export async function apiFetch<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(`/api/bridge${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`API error ${res.status}: ${text}`);
  }

  return res.json();
}
```

- [ ] **3.5** Create `src/lib/auth.ts` — NextAuth config

```typescript
// ~/ers-dashboard/src/lib/auth.ts
import { type NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";

export const authOptions: NextAuthOptions = {
  providers: [
    CredentialsProvider({
      name: "ERS Dashboard",
      credentials: {
        username: { label: "Username", type: "text" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        const validUser = process.env.ERS_AUTH_USERNAME || "admin";
        const validPass = process.env.ERS_AUTH_PASSWORD || "";

        if (
          credentials?.username === validUser &&
          credentials?.password === validPass &&
          validPass !== ""
        ) {
          return { id: "1", name: validUser };
        }
        return null;
      },
    }),
  ],
  session: { strategy: "jwt" },
  pages: {
    signIn: "/login",
  },
  secret: process.env.NEXTAUTH_SECRET,
};
```

- [ ] **3.6** Create `src/app/api/auth/[...nextauth]/route.ts`

```typescript
// ~/ers-dashboard/src/app/api/auth/[...nextauth]/route.ts
import NextAuth from "next-auth";
import { authOptions } from "@/lib/auth";

const handler = NextAuth(authOptions);
export { handler as GET, handler as POST };
```

- [ ] **3.7** Create `src/app/api/bridge/[...path]/route.ts` — proxy to bridge

```typescript
// ~/ers-dashboard/src/app/api/bridge/[...path]/route.ts
import { getServerSession } from "next-auth";
import { NextRequest, NextResponse } from "next/server";
import { authOptions } from "@/lib/auth";

const BRIDGE_URL = process.env.BRIDGE_INTERNAL_URL || "https://api.eternalrevenueservice.com";
const BRIDGE_KEY = process.env.ERS_BRIDGE_API_KEY || "";

async function proxyToBridge(req: NextRequest, params: { path: string[] }) {
  const session = await getServerSession(authOptions);
  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const path = "/" + params.path.join("/");
  const url = new URL(req.url);
  const search = url.search;
  const bridgeUrl = `${BRIDGE_URL}/api${path}${search}`;

  const headers: HeadersInit = {
    Authorization: `Bearer ${BRIDGE_KEY}`,
    "Content-Type": "application/json",
  };

  const fetchOptions: RequestInit = {
    method: req.method,
    headers,
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    const body = await req.text();
    if (body) {
      fetchOptions.body = body;
    }
  }

  try {
    const res = await fetch(bridgeUrl, fetchOptions);
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (e) {
    return NextResponse.json(
      { error: "Bridge unreachable", detail: String(e) },
      { status: 502 }
    );
  }
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxyToBridge(req, params);
}

export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxyToBridge(req, params);
}

export async function PUT(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxyToBridge(req, params);
}

export async function DELETE(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxyToBridge(req, params);
}
```

- [ ] **3.8** Create `src/lib/providers.tsx` — React Query + Session providers

```tsx
// ~/ers-dashboard/src/lib/providers.tsx
"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SessionProvider } from "next-auth/react";
import { ThemeProvider } from "next-themes";
import { useState, type ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            refetchInterval: 15_000,
          },
        },
      })
  );

  return (
    <SessionProvider>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false}>
          {children}
        </ThemeProvider>
      </QueryClientProvider>
    </SessionProvider>
  );
}
```

- [ ] **3.9** Create `src/app/layout.tsx` — root layout

```tsx
// ~/ers-dashboard/src/app/layout.tsx
import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/lib/providers";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "ERS Dashboard",
  description: "Eternal Revenue Service — RivalClaw Trading Dashboard",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className={`${inter.className} bg-zinc-950 text-zinc-100 min-h-screen`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
```

- [ ] **3.10** Update `src/app/globals.css` — dark theme base

```css
/* ~/ers-dashboard/src/app/globals.css */
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    --background: 0 0% 4%;
    --foreground: 0 0% 93%;
    --card: 0 0% 7%;
    --card-foreground: 0 0% 93%;
    --border: 0 0% 15%;
    --muted: 0 0% 15%;
    --muted-foreground: 0 0% 64%;
    --accent: 142 76% 36%;
    --destructive: 0 84% 60%;
    --warning: 38 92% 50%;
  }

  body {
    @apply bg-zinc-950 text-zinc-100;
  }
}
```

- [ ] **3.11** Create `src/components/nav.tsx` — sidebar navigation

```tsx
// ~/ers-dashboard/src/components/nav.tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Home,
  ArrowLeftRight,
  BarChart3,
  Target,
  Settings2,
  Activity,
  LogOut,
} from "lucide-react";
import { signOut } from "next-auth/react";

const links = [
  { href: "/", label: "Home", icon: Home },
  { href: "/trades", label: "Trades", icon: ArrowLeftRight },
  { href: "/markets", label: "Markets", icon: BarChart3 },
  { href: "/strategies", label: "Strategies", icon: Target },
  { href: "/controls", label: "Controls", icon: Settings2 },
  { href: "/system", label: "System", icon: Activity },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <aside className="w-56 border-r border-zinc-800 bg-zinc-950 flex flex-col h-screen sticky top-0">
      <div className="p-4 border-b border-zinc-800">
        <h1 className="text-lg font-bold tracking-tight">ERS</h1>
        <p className="text-xs text-zinc-500">Eternal Revenue Service</p>
      </div>

      <nav className="flex-1 p-2 space-y-1">
        {links.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
                active
                  ? "bg-zinc-800 text-white"
                  : "text-zinc-400 hover:text-white hover:bg-zinc-900"
              }`}
            >
              <Icon className="w-4 h-4" />
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="p-2 border-t border-zinc-800">
        <button
          onClick={() => signOut()}
          className="flex items-center gap-3 px-3 py-2 rounded-md text-sm text-zinc-400 hover:text-white hover:bg-zinc-900 w-full"
        >
          <LogOut className="w-4 h-4" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
```

- [ ] **3.12** Create `src/components/mode-indicator.tsx` — header mode badge

```tsx
// ~/ers-dashboard/src/components/mode-indicator.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

const MODE_STYLES = {
  paper: "bg-zinc-700 text-zinc-300",
  shadow: "bg-amber-900/60 text-amber-300 border border-amber-700",
  live: "bg-green-900/60 text-green-300 border border-green-700 animate-pulse",
} as const;

export function ModeIndicator() {
  const { data } = useQuery({
    queryKey: ["mode"],
    queryFn: () => apiFetch<{ mode: string }>("/control/mode"),
    refetchInterval: 5000,
  });

  const mode = (data?.mode || "paper") as keyof typeof MODE_STYLES;
  const style = MODE_STYLES[mode] || MODE_STYLES.paper;

  return (
    <span className={`px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider ${style}`}>
      {mode}
    </span>
  );
}
```

- [ ] **3.13** Create `src/components/stat-card.tsx` — reusable stat card

```tsx
// ~/ers-dashboard/src/components/stat-card.tsx
import type { ReactNode } from "react";

interface StatCardProps {
  label: string;
  value: string | number;
  sub?: string;
  icon?: ReactNode;
  trend?: "up" | "down" | "neutral";
}

export function StatCard({ label, value, sub, icon, trend }: StatCardProps) {
  const trendColor =
    trend === "up" ? "text-green-400" : trend === "down" ? "text-red-400" : "text-zinc-400";

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-zinc-500 uppercase tracking-wide">{label}</span>
        {icon && <span className="text-zinc-500">{icon}</span>}
      </div>
      <div className={`text-2xl font-bold ${trendColor}`}>{value}</div>
      {sub && <div className="text-xs text-zinc-500 mt-1">{sub}</div>}
    </div>
  );
}
```

- [ ] **3.14** Create `src/middleware.ts` — route protection

```typescript
// ~/ers-dashboard/src/middleware.ts
export { default } from "next-auth/middleware";

export const config = {
  matcher: [
    "/((?!login|api/auth|_next/static|_next/image|favicon.ico).*)",
  ],
};
```

- [ ] **3.15** Create `src/app/login/page.tsx` — login page

```tsx
// ~/ers-dashboard/src/app/login/page.tsx
"use client";

import { signIn } from "next-auth/react";
import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");

    const result = await signIn("credentials", {
      username,
      password,
      redirect: false,
    });

    setLoading(false);

    if (result?.error) {
      setError("Invalid credentials");
    } else {
      router.push("/");
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold">ERS</h1>
          <p className="text-zinc-500 text-sm mt-1">Eternal Revenue Service</p>
        </div>

        <form onSubmit={handleSubmit} className="bg-zinc-900 border border-zinc-800 rounded-lg p-6 space-y-4">
          <div>
            <label className="block text-xs text-zinc-500 mb-1 uppercase tracking-wide">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-zinc-500"
              required
            />
          </div>
          <div>
            <label className="block text-xs text-zinc-500 mb-1 uppercase tracking-wide">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-zinc-500"
              required
            />
          </div>

          {error && <p className="text-red-400 text-sm">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-zinc-700 hover:bg-zinc-600 text-white font-medium py-2 px-4 rounded text-sm transition-colors disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **3.16** Create `.env.local` template

```bash
# ~/ers-dashboard/.env.local
NEXTAUTH_SECRET=generate-a-random-secret-here
NEXTAUTH_URL=http://localhost:3000

# Auth credentials
ERS_AUTH_USERNAME=admin
ERS_AUTH_PASSWORD=your-secure-password-here

# Bridge connection
BRIDGE_INTERNAL_URL=https://api.eternalrevenueservice.com
ERS_BRIDGE_API_KEY=ers-paste-your-key-here
NEXT_PUBLIC_BRIDGE_URL=https://api.eternalrevenueservice.com
```

Generate the NextAuth secret:

```bash
echo "NEXTAUTH_SECRET=$(openssl rand -base64 32)" >> ~/ers-dashboard/.env.local
```

---

## Task 4: Auth + API Client Layer

This was completed inline in Task 3 (steps 3.5, 3.6, 3.7, 3.14). No additional work needed.

---

## Task 5: Home Page

**Files:**
- `~/ers-dashboard/src/app/page.tsx`
- `~/ers-dashboard/src/components/pnl-chart.tsx`
- `~/ers-dashboard/src/app/(dashboard)/layout.tsx`

### Steps

- [ ] **5.1** Create dashboard layout with sidebar

```tsx
// ~/ers-dashboard/src/app/(dashboard)/layout.tsx
import { Nav } from "@/components/nav";
import { ModeIndicator } from "@/components/mode-indicator";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Nav />
      <div className="flex-1 flex flex-col">
        <header className="h-14 border-b border-zinc-800 flex items-center justify-between px-6 sticky top-0 bg-zinc-950/80 backdrop-blur z-10">
          <div />
          <ModeIndicator />
        </header>
        <main className="flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
```

**Note:** Move `src/app/page.tsx` into the `(dashboard)` route group so it gets the sidebar:

```bash
mv ~/ers-dashboard/src/app/page.tsx ~/ers-dashboard/src/app/\(dashboard\)/page.tsx
```

Also move all page routes under `(dashboard)`:

```bash
mkdir -p ~/ers-dashboard/src/app/\(dashboard\)/trades
mkdir -p ~/ers-dashboard/src/app/\(dashboard\)/markets
mkdir -p ~/ers-dashboard/src/app/\(dashboard\)/strategies
mkdir -p ~/ers-dashboard/src/app/\(dashboard\)/controls
mkdir -p ~/ers-dashboard/src/app/\(dashboard\)/system
```

- [ ] **5.2** Create `src/components/pnl-chart.tsx`

```tsx
// ~/ers-dashboard/src/components/pnl-chart.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";

interface DailyPnl {
  date: string;
  balance: number;
  realized_pnl: number;
  win_rate: number;
}

export function PnlChart() {
  const { data, isLoading } = useQuery({
    queryKey: ["daily-pnl"],
    queryFn: () => apiFetch<{ daily_pnl: DailyPnl[] }>("/db/daily-pnl"),
  });

  if (isLoading) {
    return <div className="h-64 bg-zinc-900 rounded-lg animate-pulse" />;
  }

  const chartData = (data?.daily_pnl || []).map((d) => ({
    date: d.date,
    balance: Number(d.balance.toFixed(2)),
    pnl: Number((d.realized_pnl || 0).toFixed(2)),
  }));

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Portfolio Value</h3>
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id="balanceGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#22c55e" stopOpacity={0.3} />
              <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#71717a", fontSize: 11 }}
            tickFormatter={(v) => v.slice(5)}
          />
          <YAxis tick={{ fill: "#71717a", fontSize: 11 }} width={60} />
          <Tooltip
            contentStyle={{
              background: "#18181b",
              border: "1px solid #3f3f46",
              borderRadius: 8,
              fontSize: 12,
            }}
          />
          <Area
            type="monotone"
            dataKey="balance"
            stroke="#22c55e"
            fill="url(#balanceGrad)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
```

- [ ] **5.3** Create `src/app/(dashboard)/page.tsx` — Home page

```tsx
// ~/ers-dashboard/src/app/(dashboard)/page.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import { StatCard } from "@/components/stat-card";
import { PnlChart } from "@/components/pnl-chart";
import { Wallet, TrendingUp, Target, Clock, Zap, DollarSign } from "lucide-react";

interface WalletData {
  starting_balance: number;
  current_balance: number;
  total_realized_pnl: number;
  open_positions: number;
  open_exposure: number;
  today: {
    date: string;
    balance: number;
    realized_pnl: number;
    win_rate: number;
    total_trades: number;
    roi_pct: number;
  } | null;
}

interface KalshiBalance {
  balance: number;
  portfolio_value: number;
}

interface CycleData {
  cycles: Array<{
    cycle_started_at: string;
    total_cycle_ms: number;
    trades_executed: number;
  }>;
}

export default function HomePage() {
  const { data: wallet } = useQuery({
    queryKey: ["wallet"],
    queryFn: () => apiFetch<WalletData>("/db/wallet"),
  });

  const { data: kalshi } = useQuery({
    queryKey: ["kalshi-balance"],
    queryFn: () => apiFetch<KalshiBalance>("/kalshi/balance"),
    retry: 1,
  });

  const { data: cycles } = useQuery({
    queryKey: ["cycles"],
    queryFn: () => apiFetch<CycleData>("/db/cycles?limit=1"),
  });

  const pnl = wallet?.total_realized_pnl || 0;
  const lastCycle = cycles?.cycles?.[0];

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Dashboard</h2>

      {/* Top stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Paper Balance"
          value={`$${(wallet?.current_balance || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
          icon={<Wallet className="w-4 h-4" />}
          trend={pnl >= 0 ? "up" : "down"}
        />
        <StatCard
          label="Kalshi Balance"
          value={kalshi ? `$${((kalshi.balance || 0) / 100).toFixed(2)}` : "--"}
          icon={<DollarSign className="w-4 h-4" />}
          sub={kalshi ? `Portfolio: $${((kalshi.portfolio_value || 0) / 100).toFixed(2)}` : "Not connected"}
        />
        <StatCard
          label="Total P&L"
          value={`${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`}
          icon={<TrendingUp className="w-4 h-4" />}
          trend={pnl >= 0 ? "up" : "down"}
        />
        <StatCard
          label="Open Positions"
          value={wallet?.open_positions || 0}
          icon={<Target className="w-4 h-4" />}
          sub={`$${(wallet?.open_exposure || 0).toFixed(2)} exposure`}
        />
      </div>

      {/* Second stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Win Rate"
          value={wallet?.today?.win_rate != null ? `${(wallet.today.win_rate * 100).toFixed(1)}%` : "--"}
          icon={<Zap className="w-4 h-4" />}
        />
        <StatCard
          label="Today's Trades"
          value={wallet?.today?.total_trades || 0}
          sub={wallet?.today?.date || ""}
        />
        <StatCard
          label="Today's P&L"
          value={wallet?.today?.realized_pnl != null ? `$${wallet.today.realized_pnl.toFixed(2)}` : "--"}
          trend={
            wallet?.today?.realized_pnl != null
              ? wallet.today.realized_pnl >= 0
                ? "up"
                : "down"
              : "neutral"
          }
        />
        <StatCard
          label="Last Cycle"
          value={lastCycle ? `${(lastCycle.total_cycle_ms / 1000).toFixed(1)}s` : "--"}
          icon={<Clock className="w-4 h-4" />}
          sub={lastCycle?.cycle_started_at?.slice(11, 19) || ""}
        />
      </div>

      {/* P&L Chart */}
      <PnlChart />
    </div>
  );
}
```

---

## Task 6: Trades Page

**Files:**
- `~/ers-dashboard/src/app/(dashboard)/trades/page.tsx`

### Steps

- [ ] **6.1** Create `src/app/(dashboard)/trades/page.tsx`

```tsx
// ~/ers-dashboard/src/app/(dashboard)/trades/page.tsx
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type Tab = "open" | "closed" | "shadow" | "live";

interface PaperTrade {
  id: number;
  market_id: string;
  question: string;
  direction: string;
  shares: number;
  entry_price: number;
  exit_price: number | null;
  amount_usd: number;
  pnl: number | null;
  status: string;
  strategy: string;
  venue: string;
  opened_at: string;
  closed_at: string | null;
  expected_edge: number | null;
}

interface LiveOrder {
  id: number;
  ticker: string;
  action: string;
  side: string;
  count: number;
  yes_price: number;
  status: string;
  mode: string;
  submitted_at: string;
  fill_price: number | null;
  strategy: string | null;
  market_question: string | null;
  rejection_reason: string | null;
}

interface Recon {
  live_order_id: number;
  ticker: string;
  paper_entry_price: number;
  live_fill_price: number;
  slippage_delta_bps: number;
  reconciled_at: string;
}

export default function TradesPage() {
  const [tab, setTab] = useState<Tab>("open");
  const [strategy, setStrategy] = useState("");

  const { data: openTrades } = useQuery({
    queryKey: ["trades", "open"],
    queryFn: () => apiFetch<{ trades: PaperTrade[] }>("/db/trades?status=open&limit=100"),
    enabled: tab === "open",
  });

  const { data: closedTrades } = useQuery({
    queryKey: ["trades", "closed"],
    queryFn: () => apiFetch<{ trades: PaperTrade[] }>("/db/trades?status=closed&limit=100"),
    enabled: tab === "closed",
  });

  const { data: shadowOrders } = useQuery({
    queryKey: ["live-orders", "shadow"],
    queryFn: () => apiFetch<{ orders: LiveOrder[] }>("/db/live-orders?mode=shadow&limit=100"),
    enabled: tab === "shadow",
  });

  const { data: liveOrders } = useQuery({
    queryKey: ["live-orders", "live"],
    queryFn: () => apiFetch<{ orders: LiveOrder[] }>("/db/live-orders?mode=live&limit=100"),
    enabled: tab === "live",
  });

  const { data: recon } = useQuery({
    queryKey: ["reconciliation"],
    queryFn: () => apiFetch<{ reconciliation: Recon[] }>("/db/reconciliation"),
    enabled: tab === "live",
  });

  const tabs: { key: Tab; label: string }[] = [
    { key: "open", label: "Open" },
    { key: "closed", label: "Closed" },
    { key: "shadow", label: "Shadow" },
    { key: "live", label: "Live Orders" },
  ];

  function filterByStrategy<T extends { strategy?: string | null }>(items: T[]): T[] {
    if (!strategy) return items;
    return items.filter((t) => t.strategy === strategy);
  }

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold">Trades</h2>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-zinc-800 pb-px">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${
              tab === t.key
                ? "bg-zinc-800 text-white border-b-2 border-green-500"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Strategy filter */}
      <div className="flex items-center gap-2">
        <label className="text-xs text-zinc-500 uppercase">Strategy:</label>
        <input
          type="text"
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          placeholder="All"
          className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm w-40"
        />
      </div>

      {/* Paper trades table (open/closed) */}
      {(tab === "open" || tab === "closed") && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
                <th className="pb-2 pr-4">Market</th>
                <th className="pb-2 pr-4">Dir</th>
                <th className="pb-2 pr-4">Entry</th>
                <th className="pb-2 pr-4">Exit</th>
                <th className="pb-2 pr-4">Amount</th>
                <th className="pb-2 pr-4">P&L</th>
                <th className="pb-2 pr-4">Strategy</th>
                <th className="pb-2 pr-4">Venue</th>
                <th className="pb-2">Time</th>
              </tr>
            </thead>
            <tbody>
              {filterByStrategy(
                (tab === "open" ? openTrades?.trades : closedTrades?.trades) || []
              ).map((t) => (
                <tr key={t.id} className="border-b border-zinc-800/50 hover:bg-zinc-900/50">
                  <td className="py-2 pr-4 max-w-[200px] truncate" title={t.question}>
                    {t.question?.slice(0, 40) || t.market_id}
                  </td>
                  <td className="py-2 pr-4">
                    <span className={t.direction === "yes" ? "text-green-400" : "text-red-400"}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="py-2 pr-4">${t.entry_price?.toFixed(2)}</td>
                  <td className="py-2 pr-4">{t.exit_price != null ? `$${t.exit_price.toFixed(2)}` : "--"}</td>
                  <td className="py-2 pr-4">${t.amount_usd?.toFixed(2)}</td>
                  <td className={`py-2 pr-4 ${t.pnl != null && t.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {t.pnl != null ? `$${t.pnl.toFixed(2)}` : "--"}
                  </td>
                  <td className="py-2 pr-4 text-zinc-400">{t.strategy}</td>
                  <td className="py-2 pr-4 text-zinc-500">{t.venue}</td>
                  <td className="py-2 text-zinc-500 text-xs">{t.opened_at?.slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Shadow/Live orders table */}
      {(tab === "shadow" || tab === "live") && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
                <th className="pb-2 pr-4">Ticker</th>
                <th className="pb-2 pr-4">Action</th>
                <th className="pb-2 pr-4">Side</th>
                <th className="pb-2 pr-4">Count</th>
                <th className="pb-2 pr-4">Price (c)</th>
                <th className="pb-2 pr-4">Fill (c)</th>
                <th className="pb-2 pr-4">Status</th>
                <th className="pb-2 pr-4">Strategy</th>
                <th className="pb-2">Time</th>
              </tr>
            </thead>
            <tbody>
              {filterByStrategy(
                (tab === "shadow" ? shadowOrders?.orders : liveOrders?.orders) || []
              ).map((o) => (
                <tr key={o.id} className="border-b border-zinc-800/50 hover:bg-zinc-900/50">
                  <td className="py-2 pr-4 font-mono text-xs">{o.ticker}</td>
                  <td className="py-2 pr-4">{o.action}</td>
                  <td className={`py-2 pr-4 ${o.side === "yes" ? "text-green-400" : "text-red-400"}`}>
                    {o.side}
                  </td>
                  <td className="py-2 pr-4">{o.count}</td>
                  <td className="py-2 pr-4">{o.yes_price}</td>
                  <td className="py-2 pr-4">{o.fill_price ?? "--"}</td>
                  <td className="py-2 pr-4">
                    <span
                      className={`px-2 py-0.5 rounded text-xs ${
                        o.status === "filled"
                          ? "bg-green-900/40 text-green-400"
                          : o.status === "rejected"
                            ? "bg-red-900/40 text-red-400"
                            : o.status === "pending"
                              ? "bg-yellow-900/40 text-yellow-400"
                              : "bg-zinc-800 text-zinc-400"
                      }`}
                    >
                      {o.status}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-zinc-400">{o.strategy || "--"}</td>
                  <td className="py-2 text-zinc-500 text-xs">{o.submitted_at?.slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Reconciliation panel for live tab */}
      {tab === "live" && recon?.reconciliation && recon.reconciliation.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-3">Reconciliation</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
                  <th className="pb-2 pr-4">Ticker</th>
                  <th className="pb-2 pr-4">Paper Price</th>
                  <th className="pb-2 pr-4">Live Fill</th>
                  <th className="pb-2 pr-4">Slippage (bps)</th>
                  <th className="pb-2">Time</th>
                </tr>
              </thead>
              <tbody>
                {recon.reconciliation.map((r) => (
                  <tr key={r.live_order_id} className="border-b border-zinc-800/50">
                    <td className="py-2 pr-4 font-mono text-xs">{r.ticker}</td>
                    <td className="py-2 pr-4">${r.paper_entry_price?.toFixed(4)}</td>
                    <td className="py-2 pr-4">${r.live_fill_price?.toFixed(4)}</td>
                    <td
                      className={`py-2 pr-4 ${
                        r.slippage_delta_bps > 500 ? "text-red-400" : "text-zinc-300"
                      }`}
                    >
                      {r.slippage_delta_bps?.toFixed(0)}
                    </td>
                    <td className="py-2 text-zinc-500 text-xs">{r.reconciled_at?.slice(0, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
```

---

## Task 7: Markets Page

**Files:**
- `~/ers-dashboard/src/app/(dashboard)/markets/page.tsx`
- `~/ers-dashboard/src/components/candlestick-chart.tsx`

### Steps

- [ ] **7.1** Create `src/components/candlestick-chart.tsx`

```tsx
// ~/ers-dashboard/src/components/candlestick-chart.tsx
"use client";

import { useEffect, useRef } from "react";
import { createChart, type IChartApi, type ISeriesApi, ColorType } from "lightweight-charts";

interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

interface CandlestickChartProps {
  data: Candle[];
  height?: number;
}

export function CandlestickChart({ data, height = 400 }: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#09090b" },
        textColor: "#71717a",
      },
      grid: {
        vertLines: { color: "#27272a" },
        horzLines: { color: "#27272a" },
      },
      width: containerRef.current.clientWidth,
      height,
      crosshair: {
        vertLine: { color: "#3f3f46" },
        horzLine: { color: "#3f3f46" },
      },
    });

    const series = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderDownColor: "#ef4444",
      borderUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      wickUpColor: "#22c55e",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [height]);

  useEffect(() => {
    if (seriesRef.current && data.length > 0) {
      const formatted = data.map((c) => ({
        time: c.time as unknown as import("lightweight-charts").Time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      seriesRef.current.setData(formatted);
      chartRef.current?.timeScale().fitContent();
    }
  }, [data]);

  return <div ref={containerRef} className="w-full" />;
}
```

- [ ] **7.2** Create `src/app/(dashboard)/markets/page.tsx`

```tsx
// ~/ers-dashboard/src/app/(dashboard)/markets/page.tsx
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import { CandlestickChart } from "@/components/candlestick-chart";

const FAST_SERIES = [
  "KXDOGE15M", "KXADA15M", "KXBNB15M", "KXBCH15M",
  "KXBTC", "KXETH", "KXBTCMAXD",
  "KXHIGHTDC", "KXHIGHTSFO", "KXTEMPNYCH",
  "KXGOLDD", "KXSILVERD", "KXTNOTED", "KXUSDJPY",
  "KXINXSPX", "KXINXNDX",
];

interface MarketDataRow {
  id: number;
  market_id: string;
  question: string;
  yes_price: number;
  no_price: number;
  volume: number;
  venue: string;
  fetched_at: string;
}

export default function MarketsPage() {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [candlePeriod, setCandlePeriod] = useState("1h");

  const { data: markets } = useQuery({
    queryKey: ["market-data"],
    queryFn: () => apiFetch<{ markets: MarketDataRow[] }>("/db/market-data?venue=kalshi&limit=200"),
  });

  const { data: exchangeStatus } = useQuery({
    queryKey: ["exchange-status"],
    queryFn: () => apiFetch<{ exchange_status?: string; trading_active?: boolean }>("/kalshi/exchange/status"),
    retry: 1,
  });

  const { data: orderbook } = useQuery({
    queryKey: ["orderbook", selectedTicker],
    queryFn: () => apiFetch<{ orderbook?: Record<string, unknown> }>(`/kalshi/market/${selectedTicker}/orderbook`),
    enabled: !!selectedTicker,
    refetchInterval: 10000,
  });

  const { data: candles } = useQuery({
    queryKey: ["candles", selectedTicker, candlePeriod],
    queryFn: () =>
      apiFetch<{ candlesticks?: Array<{ t: number; o: number; h: number; l: number; c: number }> }>(
        `/kalshi/market/${selectedTicker}/candlesticks?period=${candlePeriod}`
      ),
    enabled: !!selectedTicker,
  });

  // Deduplicate markets by market_id, keeping the latest
  const uniqueMarkets = new Map<string, MarketDataRow>();
  (markets?.markets || []).forEach((m) => {
    if (!uniqueMarkets.has(m.market_id) || m.fetched_at > (uniqueMarkets.get(m.market_id)?.fetched_at || "")) {
      uniqueMarkets.set(m.market_id, m);
    }
  });
  const marketList = Array.from(uniqueMarkets.values());

  const candleData = (candles?.candlesticks || []).map((c) => ({
    time: new Date(c.t * 1000).toISOString().slice(0, 10),
    open: c.o / 100,
    high: c.h / 100,
    low: c.l / 100,
    close: c.c / 100,
  }));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">Markets</h2>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-zinc-500">Exchange:</span>
          <span
            className={`w-2 h-2 rounded-full ${
              exchangeStatus?.trading_active ? "bg-green-500" : "bg-red-500"
            }`}
          />
          <span className={exchangeStatus?.trading_active ? "text-green-400" : "text-red-400"}>
            {exchangeStatus?.trading_active ? "Open" : "Closed"}
          </span>
        </div>
      </div>

      {/* Series filter chips */}
      <div className="flex flex-wrap gap-2">
        {FAST_SERIES.map((s) => {
          const hasData = marketList.some((m) => m.market_id.startsWith(s));
          return (
            <button
              key={s}
              onClick={() => setSelectedTicker(s)}
              className={`px-2 py-1 rounded text-xs font-mono transition-colors ${
                selectedTicker === s
                  ? "bg-green-900/40 text-green-400 border border-green-700"
                  : hasData
                    ? "bg-zinc-800 text-zinc-300 hover:bg-zinc-700"
                    : "bg-zinc-900 text-zinc-600"
              }`}
            >
              {s}
            </button>
          );
        })}
      </div>

      {/* Market data table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
              <th className="pb-2 pr-4">Market</th>
              <th className="pb-2 pr-4">Question</th>
              <th className="pb-2 pr-4">Yes</th>
              <th className="pb-2 pr-4">No</th>
              <th className="pb-2 pr-4">Volume</th>
              <th className="pb-2">Fetched</th>
            </tr>
          </thead>
          <tbody>
            {marketList.slice(0, 50).map((m) => (
              <tr
                key={m.id}
                className={`border-b border-zinc-800/50 cursor-pointer hover:bg-zinc-900/50 ${
                  selectedTicker && m.market_id.startsWith(selectedTicker) ? "bg-zinc-800/30" : ""
                }`}
                onClick={() => setSelectedTicker(m.market_id)}
              >
                <td className="py-2 pr-4 font-mono text-xs">{m.market_id}</td>
                <td className="py-2 pr-4 max-w-[300px] truncate" title={m.question}>
                  {m.question?.slice(0, 60)}
                </td>
                <td className="py-2 pr-4 text-green-400">{m.yes_price?.toFixed(2)}</td>
                <td className="py-2 pr-4 text-red-400">{m.no_price?.toFixed(2)}</td>
                <td className="py-2 pr-4">{m.volume?.toLocaleString()}</td>
                <td className="py-2 text-zinc-500 text-xs">{m.fetched_at?.slice(11, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Candlestick chart for selected market */}
      {selectedTicker && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-mono text-zinc-400">{selectedTicker} Candlesticks</h3>
            <div className="flex gap-1">
              {["1m", "1h", "1d"].map((p) => (
                <button
                  key={p}
                  onClick={() => setCandlePeriod(p)}
                  className={`px-2 py-1 rounded text-xs ${
                    candlePeriod === p
                      ? "bg-zinc-700 text-white"
                      : "text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
          {candleData.length > 0 ? (
            <CandlestickChart data={candleData} height={350} />
          ) : (
            <div className="h-[350px] flex items-center justify-center text-zinc-600 text-sm">
              No candlestick data available
            </div>
          )}
        </div>
      )}

      {/* Orderbook for selected market */}
      {selectedTicker && orderbook?.orderbook && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-3">Orderbook</h3>
          <pre className="text-xs text-zinc-300 font-mono overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(orderbook.orderbook, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
```

---

## Task 8: Strategies Page

**Files:**
- `~/ers-dashboard/src/app/(dashboard)/strategies/page.tsx`

### Steps

- [ ] **8.1** Create `src/app/(dashboard)/strategies/page.tsx`

```tsx
// ~/ers-dashboard/src/app/(dashboard)/strategies/page.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import { TrendingUp, TrendingDown, Target, Percent } from "lucide-react";

interface StrategyData {
  strategy: string;
  total_trades: number;
  wins: number;
  losses: number;
  open_count: number;
  total_pnl: number;
  avg_pnl: number;
  win_rate: number | null;
  avg_expected_edge: number;
  first_trade: string;
  last_trade: string;
}

export default function StrategiesPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["strategies"],
    queryFn: () => apiFetch<{ strategies: StrategyData[] }>("/db/strategies"),
  });

  const strategies = data?.strategies || [];
  const best = strategies.length > 0 ? strategies[0] : null; // already sorted by total_pnl DESC
  const worst = strategies.length > 1 ? strategies[strategies.length - 1] : null;

  if (isLoading) {
    return (
      <div className="space-y-4">
        <h2 className="text-xl font-bold">Strategies</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="bg-zinc-900 border border-zinc-800 rounded-lg p-6 h-48 animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Strategies</h2>

      {/* Best/Worst highlights */}
      {best && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-green-900/10 border border-green-900/30 rounded-lg p-4 flex items-center gap-4">
            <TrendingUp className="w-8 h-8 text-green-400" />
            <div>
              <div className="text-xs text-green-400 uppercase tracking-wide">Best Performer</div>
              <div className="text-lg font-bold">{best.strategy}</div>
              <div className="text-sm text-green-400">+${best.total_pnl.toFixed(2)}</div>
            </div>
          </div>
          {worst && worst.total_pnl < 0 && (
            <div className="bg-red-900/10 border border-red-900/30 rounded-lg p-4 flex items-center gap-4">
              <TrendingDown className="w-8 h-8 text-red-400" />
              <div>
                <div className="text-xs text-red-400 uppercase tracking-wide">Worst Performer</div>
                <div className="text-lg font-bold">{worst.strategy}</div>
                <div className="text-sm text-red-400">${worst.total_pnl.toFixed(2)}</div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Strategy cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {strategies.map((s) => (
          <div key={s.strategy} className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-sm">{s.strategy}</h3>
              <span
                className={`text-sm font-bold ${s.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}
              >
                {s.total_pnl >= 0 ? "+" : ""}${s.total_pnl.toFixed(2)}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <div className="text-zinc-500 text-xs">Win Rate</div>
                <div className="font-medium flex items-center gap-1">
                  <Percent className="w-3 h-3 text-zinc-500" />
                  {s.win_rate != null ? `${(s.win_rate * 100).toFixed(1)}%` : "--"}
                </div>
              </div>
              <div>
                <div className="text-zinc-500 text-xs">Avg P&L</div>
                <div className={`font-medium ${s.avg_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  ${s.avg_pnl.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-zinc-500 text-xs">Total Trades</div>
                <div className="font-medium flex items-center gap-1">
                  <Target className="w-3 h-3 text-zinc-500" />
                  {s.total_trades}
                </div>
              </div>
              <div>
                <div className="text-zinc-500 text-xs">W/L</div>
                <div className="font-medium">
                  <span className="text-green-400">{s.wins}</span>
                  {" / "}
                  <span className="text-red-400">{s.losses}</span>
                </div>
              </div>
              <div>
                <div className="text-zinc-500 text-xs">Open</div>
                <div className="font-medium">{s.open_count}</div>
              </div>
              <div>
                <div className="text-zinc-500 text-xs">Avg Edge</div>
                <div className="font-medium">{(s.avg_expected_edge * 100).toFixed(1)}%</div>
              </div>
            </div>

            <div className="mt-3 pt-3 border-t border-zinc-800 text-xs text-zinc-500">
              {s.first_trade?.slice(0, 10)} to {s.last_trade?.slice(0, 10)}
            </div>
          </div>
        ))}
      </div>

      {strategies.length === 0 && (
        <div className="text-center text-zinc-600 py-12">No strategy data yet</div>
      )}
    </div>
  );
}
```

---

## Task 9: Controls Page

**Files:**
- `~/ers-dashboard/src/app/(dashboard)/controls/page.tsx`

### Steps

- [ ] **9.1** Create `src/app/(dashboard)/controls/page.tsx`

```tsx
// ~/ers-dashboard/src/app/(dashboard)/controls/page.tsx
"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import {
  Power,
  ShieldOff,
  Eye,
  FileText,
  XCircle,
  RefreshCw,
  Send,
} from "lucide-react";

interface ModeData {
  mode: string;
}

interface KillSwitchData {
  enabled: boolean;
}

interface ConfigData {
  config: Record<string, string>;
}

export default function ControlsPage() {
  const queryClient = useQueryClient();
  const [showConfirm, setShowConfirm] = useState<string | null>(null);
  const [orderForm, setOrderForm] = useState({
    ticker: "",
    action: "buy",
    side: "yes",
    count: 1,
    yes_price: 0.5,
  });
  const [configEdit, setConfigEdit] = useState<{ key: string; value: string } | null>(null);

  // Queries
  const { data: modeData } = useQuery({
    queryKey: ["mode"],
    queryFn: () => apiFetch<ModeData>("/control/mode"),
    refetchInterval: 5000,
  });

  const { data: killData } = useQuery({
    queryKey: ["kill-switch"],
    queryFn: () => apiFetch<KillSwitchData>("/control/kill-switch"),
    refetchInterval: 5000,
  });

  const { data: configData } = useQuery({
    queryKey: ["config"],
    queryFn: () => apiFetch<ConfigData>("/control/config"),
  });

  // Mutations
  const setMode = useMutation({
    mutationFn: (mode: string) =>
      apiFetch("/control/mode", { method: "POST", body: JSON.stringify({ mode }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mode"] });
      setShowConfirm(null);
    },
  });

  const setKillSwitch = useMutation({
    mutationFn: (enabled: boolean) =>
      apiFetch("/control/kill-switch", { method: "POST", body: JSON.stringify({ enabled }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kill-switch"] });
      setShowConfirm(null);
    },
  });

  const batchCancel = useMutation({
    mutationFn: () =>
      apiFetch("/kalshi/orders/batch-cancel", { method: "POST" }),
    onSuccess: () => setShowConfirm(null),
  });

  const placeOrder = useMutation({
    mutationFn: () =>
      apiFetch("/kalshi/orders", { method: "POST", body: JSON.stringify(orderForm) }),
  });

  const syncBalance = useMutation({
    mutationFn: () => apiFetch("/control/sync-balance", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["wallet", "kalshi-balance"] }),
  });

  const syncPositions = useMutation({
    mutationFn: () => apiFetch("/control/sync-positions", { method: "POST" }),
  });

  const updateConfig = useMutation({
    mutationFn: (body: { key: string; value: string }) =>
      apiFetch("/control/config", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["config"] });
      setConfigEdit(null);
    },
  });

  const mode = modeData?.mode || "paper";
  const killActive = killData?.enabled || false;

  // Confirmation modal
  function ConfirmModal({
    action,
    message,
    onConfirm,
    variant = "default",
  }: {
    action: string;
    message: string;
    onConfirm: () => void;
    variant?: "default" | "danger" | "success";
  }) {
    if (showConfirm !== action) return null;
    const btnClass =
      variant === "danger"
        ? "bg-red-600 hover:bg-red-700"
        : variant === "success"
          ? "bg-green-600 hover:bg-green-700"
          : "bg-zinc-600 hover:bg-zinc-500";
    return (
      <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
        <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 max-w-sm w-full mx-4">
          <p className="text-sm mb-4">{message}</p>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setShowConfirm(null)}
              className="px-4 py-2 rounded text-sm bg-zinc-800 hover:bg-zinc-700"
            >
              Cancel
            </button>
            <button
              onClick={onConfirm}
              className={`px-4 py-2 rounded text-sm text-white ${btnClass}`}
            >
              Confirm
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <h2 className="text-xl font-bold">Controls</h2>

      {/* Mode buttons */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Execution Mode</h3>
        <div className="flex flex-wrap gap-4">
          {/* Start Trading (Live) */}
          <button
            onClick={() => setShowConfirm("live")}
            className={`relative px-8 py-4 rounded-lg font-bold text-lg transition-all ${
              mode === "live"
                ? "bg-green-600 text-white shadow-lg shadow-green-600/30 animate-pulse"
                : "bg-green-900/30 text-green-400 border-2 border-green-700 hover:bg-green-900/50"
            }`}
          >
            <Power className="w-5 h-5 inline mr-2" />
            Start Trading
          </button>
          <ConfirmModal
            action="live"
            message="Switch to LIVE mode? Real orders will be submitted to Kalshi."
            onConfirm={() => setMode.mutate("live")}
            variant="success"
          />

          {/* Shadow Mode */}
          <button
            onClick={() => setShowConfirm("shadow")}
            className={`px-6 py-4 rounded-lg font-bold text-lg transition-all ${
              mode === "shadow"
                ? "bg-amber-600 text-white shadow-lg shadow-amber-600/30"
                : "bg-amber-900/30 text-amber-400 border-2 border-amber-700 hover:bg-amber-900/50"
            }`}
          >
            <Eye className="w-5 h-5 inline mr-2" />
            Shadow
          </button>
          <ConfirmModal
            action="shadow"
            message="Switch to SHADOW mode? Orders will be logged but not submitted."
            onConfirm={() => setMode.mutate("shadow")}
          />

          {/* Paper Mode */}
          <button
            onClick={() => setMode.mutate("paper")}
            className={`px-6 py-4 rounded-lg font-bold text-lg transition-all ${
              mode === "paper"
                ? "bg-zinc-600 text-white"
                : "bg-zinc-800 text-zinc-400 border-2 border-zinc-700 hover:bg-zinc-700"
            }`}
          >
            <FileText className="w-5 h-5 inline mr-2" />
            Paper
          </button>
        </div>
      </section>

      {/* Kill Switch */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Kill Switch</h3>
        <button
          onClick={() => setShowConfirm("kill")}
          className={`relative px-10 py-5 rounded-xl font-black text-xl transition-all ${
            killActive
              ? "bg-red-600 text-white shadow-2xl shadow-red-600/50 animate-pulse ring-4 ring-red-400/30"
              : "bg-red-900/30 text-red-400 border-4 border-red-700 hover:bg-red-900/50 hover:shadow-lg hover:shadow-red-600/20"
          }`}
        >
          <ShieldOff className="w-6 h-6 inline mr-2" />
          {killActive ? "KILL SWITCH ACTIVE" : "KILL SWITCH"}
        </button>
        <ConfirmModal
          action="kill"
          message={
            killActive
              ? "Deactivate the kill switch? Trading will resume based on the current execution mode."
              : "Activate the KILL SWITCH? This will immediately halt ALL live order submissions and batch-cancel resting orders."
          }
          onConfirm={() => {
            setKillSwitch.mutate(!killActive);
            if (!killActive) {
              batchCancel.mutate();
            }
          }}
          variant="danger"
        />
      </section>

      {/* Order management */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Order Management</h3>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => setShowConfirm("batch-cancel")}
            className="px-4 py-2 rounded bg-red-900/30 text-red-400 border border-red-800 hover:bg-red-900/50 text-sm flex items-center gap-2"
          >
            <XCircle className="w-4 h-4" />
            Cancel All Orders
          </button>
          <ConfirmModal
            action="batch-cancel"
            message="Cancel ALL resting orders on Kalshi?"
            onConfirm={() => batchCancel.mutate()}
            variant="danger"
          />

          <button
            onClick={() => syncBalance.mutate()}
            disabled={syncBalance.isPending}
            className="px-4 py-2 rounded bg-zinc-800 text-zinc-300 hover:bg-zinc-700 text-sm flex items-center gap-2 disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${syncBalance.isPending ? "animate-spin" : ""}`} />
            Sync Balance
          </button>

          <button
            onClick={() => syncPositions.mutate()}
            disabled={syncPositions.isPending}
            className="px-4 py-2 rounded bg-zinc-800 text-zinc-300 hover:bg-zinc-700 text-sm flex items-center gap-2 disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${syncPositions.isPending ? "animate-spin" : ""}`} />
            Sync Positions
          </button>
        </div>
      </section>

      {/* Manual order form */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Place Manual Order</h3>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 max-w-lg">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-zinc-500 mb-1">Ticker</label>
              <input
                type="text"
                value={orderForm.ticker}
                onChange={(e) => setOrderForm({ ...orderForm, ticker: e.target.value })}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm font-mono"
                placeholder="KXDOGE15M-..."
              />
            </div>
            <div>
              <label className="block text-xs text-zinc-500 mb-1">Action</label>
              <select
                value={orderForm.action}
                onChange={(e) => setOrderForm({ ...orderForm, action: e.target.value })}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm"
              >
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-zinc-500 mb-1">Side</label>
              <select
                value={orderForm.side}
                onChange={(e) => setOrderForm({ ...orderForm, side: e.target.value })}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm"
              >
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-zinc-500 mb-1">Count</label>
              <input
                type="number"
                min={1}
                max={100}
                value={orderForm.count}
                onChange={(e) => setOrderForm({ ...orderForm, count: parseInt(e.target.value) || 1 })}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm"
              />
            </div>
            <div className="col-span-2">
              <label className="block text-xs text-zinc-500 mb-1">Price ($)</label>
              <input
                type="number"
                min={0.01}
                max={0.99}
                step={0.01}
                value={orderForm.yes_price}
                onChange={(e) => setOrderForm({ ...orderForm, yes_price: parseFloat(e.target.value) || 0.5 })}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm"
              />
            </div>
          </div>
          <button
            onClick={() => setShowConfirm("place-order")}
            disabled={!orderForm.ticker || placeOrder.isPending}
            className="mt-3 w-full px-4 py-2 rounded bg-green-800 text-green-200 hover:bg-green-700 text-sm flex items-center justify-center gap-2 disabled:opacity-50"
          >
            <Send className="w-4 h-4" />
            {placeOrder.isPending ? "Submitting..." : "Submit Order"}
          </button>
          <ConfirmModal
            action="place-order"
            message={`Submit ${orderForm.action} ${orderForm.count} ${orderForm.side} @ $${orderForm.yes_price} on ${orderForm.ticker}?`}
            onConfirm={() => placeOrder.mutate()}
            variant="success"
          />
          {placeOrder.isError && (
            <p className="text-red-400 text-xs mt-2">{String(placeOrder.error)}</p>
          )}
          {placeOrder.isSuccess && (
            <p className="text-green-400 text-xs mt-2">Order submitted successfully</p>
          )}
        </div>
      </section>

      {/* Safety config panel */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-4">Safety Configuration</h3>
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
                <th className="p-3">Parameter</th>
                <th className="p-3">Value</th>
                <th className="p-3 w-20">Edit</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(configData?.config || {}).map(([key, value]) => (
                <tr key={key} className="border-b border-zinc-800/50">
                  <td className="p-3 font-mono text-xs text-zinc-400">{key}</td>
                  <td className="p-3">
                    {configEdit?.key === key ? (
                      <input
                        type="text"
                        value={configEdit.value}
                        onChange={(e) => setConfigEdit({ key, value: e.target.value })}
                        className="bg-zinc-800 border border-zinc-600 rounded px-2 py-1 text-sm w-full"
                        onKeyDown={(e) => {
                          if (e.key === "Enter") updateConfig.mutate(configEdit);
                          if (e.key === "Escape") setConfigEdit(null);
                        }}
                        autoFocus
                      />
                    ) : (
                      <span className="text-zinc-200">{value}</span>
                    )}
                  </td>
                  <td className="p-3">
                    {configEdit?.key === key ? (
                      <div className="flex gap-1">
                        <button
                          onClick={() => updateConfig.mutate(configEdit)}
                          className="text-green-400 hover:text-green-300 text-xs"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => setConfigEdit(null)}
                          className="text-zinc-500 hover:text-zinc-300 text-xs"
                        >
                          X
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfigEdit({ key, value: value || "" })}
                        className="text-zinc-500 hover:text-zinc-300 text-xs"
                      >
                        Edit
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
```

---

## Task 10: System Page

**Files:**
- `~/ers-dashboard/src/app/(dashboard)/system/page.tsx`

### Steps

- [ ] **10.1** Create `src/app/(dashboard)/system/page.tsx`

```tsx
// ~/ers-dashboard/src/app/(dashboard)/system/page.tsx
"use client";

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import { StatCard } from "@/components/stat-card";
import { Clock, AlertTriangle, Activity, Gauge } from "lucide-react";

interface Cycle {
  id: number;
  cycle_started_at: string;
  markets_fetched: number;
  opportunities_detected: number;
  opportunities_qualified: number;
  trades_executed: number;
  stops_closed: number;
  fetch_ms: number;
  analyze_ms: number;
  wallet_ms: number;
  total_cycle_ms: number;
}

interface ErrorEvent {
  id: number;
  event_id: string;
  event_type: string;
  timestamp_ms: number;
  cycle_id: string;
  payload: string;
}

export default function SystemPage() {
  const { data: cycleData } = useQuery({
    queryKey: ["cycles-system"],
    queryFn: () => apiFetch<{ cycles: Cycle[] }>("/db/cycles?limit=50"),
  });

  const { data: errorData } = useQuery({
    queryKey: ["errors"],
    queryFn: () => apiFetch<{ errors: ErrorEvent[] }>("/db/errors?limit=100"),
  });

  const { data: rateLimit } = useQuery({
    queryKey: ["rate-limit"],
    queryFn: () => apiFetch<{ exchange_status?: string }>("/kalshi/exchange/status"),
    retry: 1,
  });

  const cycles = cycleData?.cycles || [];
  const errors = errorData?.errors || [];

  // Compute cycle timing stats
  const cycleTimes = cycles.map((c) => c.total_cycle_ms).filter((t) => t > 0);
  const avgCycle = cycleTimes.length > 0 ? cycleTimes.reduce((a, b) => a + b, 0) / cycleTimes.length : 0;
  const sortedTimes = [...cycleTimes].sort((a, b) => a - b);
  const p95Index = Math.floor(sortedTimes.length * 0.95);
  const p95Cycle = sortedTimes[p95Index] || 0;
  const maxCycle = sortedTimes[sortedTimes.length - 1] || 0;

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">System</h2>

      {/* Timing stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Avg Cycle"
          value={`${(avgCycle / 1000).toFixed(2)}s`}
          icon={<Clock className="w-4 h-4" />}
          sub={`${cycleTimes.length} cycles`}
        />
        <StatCard
          label="P95 Cycle"
          value={`${(p95Cycle / 1000).toFixed(2)}s`}
          icon={<Activity className="w-4 h-4" />}
        />
        <StatCard
          label="Max Cycle"
          value={`${(maxCycle / 1000).toFixed(2)}s`}
          icon={<Gauge className="w-4 h-4" />}
        />
        <StatCard
          label="Errors"
          value={errors.length}
          icon={<AlertTriangle className="w-4 h-4" />}
          trend={errors.length > 10 ? "down" : "neutral"}
        />
      </div>

      {/* Recent cycles table */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-3">Recent Cycles</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-zinc-500 text-xs uppercase border-b border-zinc-800">
                <th className="pb-2 pr-4">Time</th>
                <th className="pb-2 pr-4">Total (ms)</th>
                <th className="pb-2 pr-4">Fetch</th>
                <th className="pb-2 pr-4">Analyze</th>
                <th className="pb-2 pr-4">Wallet</th>
                <th className="pb-2 pr-4">Markets</th>
                <th className="pb-2 pr-4">Opps</th>
                <th className="pb-2 pr-4">Qualified</th>
                <th className="pb-2">Trades</th>
              </tr>
            </thead>
            <tbody>
              {cycles.map((c) => (
                <tr key={c.id} className="border-b border-zinc-800/50 hover:bg-zinc-900/50">
                  <td className="py-2 pr-4 text-xs text-zinc-400">
                    {c.cycle_started_at?.slice(0, 19)}
                  </td>
                  <td className="py-2 pr-4 font-mono">
                    <span
                      className={
                        c.total_cycle_ms > p95Cycle
                          ? "text-red-400"
                          : c.total_cycle_ms > avgCycle * 1.5
                            ? "text-amber-400"
                            : "text-green-400"
                      }
                    >
                      {c.total_cycle_ms?.toFixed(0)}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-zinc-400">{c.fetch_ms?.toFixed(0)}</td>
                  <td className="py-2 pr-4 text-zinc-400">{c.analyze_ms?.toFixed(0)}</td>
                  <td className="py-2 pr-4 text-zinc-400">{c.wallet_ms?.toFixed(0)}</td>
                  <td className="py-2 pr-4">{c.markets_fetched}</td>
                  <td className="py-2 pr-4">{c.opportunities_detected}</td>
                  <td className="py-2 pr-4">{c.opportunities_qualified}</td>
                  <td className="py-2">{c.trades_executed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Error log */}
      <section>
        <h3 className="text-sm text-zinc-500 uppercase tracking-wide mb-3">Error Log</h3>
        {errors.length === 0 ? (
          <div className="text-center text-zinc-600 py-8 bg-zinc-900 rounded-lg">No errors recorded</div>
        ) : (
          <div className="space-y-2 max-h-[400px] overflow-y-auto">
            {errors.map((e) => {
              let payload: Record<string, unknown> = {};
              try {
                payload = JSON.parse(e.payload);
              } catch {
                payload = { raw: e.payload };
              }
              return (
                <div
                  key={e.id}
                  className="bg-zinc-900 border border-zinc-800 rounded-lg p-3 text-sm"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-red-400 font-medium text-xs">{e.event_type}</span>
                    <span className="text-zinc-600 text-xs">
                      {e.timestamp_ms
                        ? new Date(e.timestamp_ms).toISOString().slice(0, 19)
                        : ""}
                    </span>
                  </div>
                  {e.cycle_id && (
                    <div className="text-zinc-500 text-xs mb-1">Cycle: {e.cycle_id}</div>
                  )}
                  <pre className="text-xs text-zinc-400 font-mono overflow-x-auto whitespace-pre-wrap">
                    {JSON.stringify(payload, null, 2).slice(0, 500)}
                  </pre>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
```

---

## Task 11: Deploy to Vercel

**Files:**
- `~/ers-dashboard/vercel.json`

### Steps

- [ ] **11.1** Create `vercel.json`

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "nextjs"
}
```

Write to `~/ers-dashboard/vercel.json`.

- [ ] **11.2** Initialize git repo

```bash
cd ~/ers-dashboard
git init
echo "node_modules\n.next\n.env.local\n.env\n.vercel" > .gitignore
git add -A
git commit -m "Initial ERS Dashboard scaffold"
```

- [ ] **11.3** Create GitHub repo and push

```bash
cd ~/ers-dashboard
gh repo create ers-dashboard --private --source=. --push
```

- [ ] **11.4** Deploy to Vercel

```bash
cd ~/ers-dashboard
npx vercel --prod
```

During setup, accept defaults. When prompted for framework, select Next.js.

- [ ] **11.5** Set environment variables on Vercel

```bash
cd ~/ers-dashboard

# Required env vars
vercel env add NEXTAUTH_SECRET production
vercel env add NEXTAUTH_URL production       # Set to https://eternalrevenueservice.com
vercel env add ERS_AUTH_USERNAME production
vercel env add ERS_AUTH_PASSWORD production
vercel env add ERS_BRIDGE_API_KEY production  # Same key as in rivalclaw/.env
vercel env add BRIDGE_INTERNAL_URL production # https://api.eternalrevenueservice.com
vercel env add NEXT_PUBLIC_BRIDGE_URL production # https://api.eternalrevenueservice.com
```

- [ ] **11.6** Add custom domain in Vercel

```bash
vercel domains add eternalrevenueservice.com
```

Then verify DNS is pointing correctly (Cloudflare CNAME `@` -> `cname.vercel-dns.com`).

- [ ] **11.7** Redeploy with env vars

```bash
cd ~/ers-dashboard
npx vercel --prod
```

- [ ] **11.8** End-to-end verification

1. Start the bridge: `cd ~/rivalclaw/bridge && bash run.sh`
2. Start the tunnel: `bash ~/rivalclaw/bridge/tunnel.sh`
3. Visit `https://eternalrevenueservice.com`
4. Log in with credentials
5. Verify Home page loads with wallet data
6. Check Trades, Markets, Strategies, Controls, System pages
7. Test mode switching on Controls page
8. Verify kill switch toggles correctly

Expected: All pages render data from the bridge. Mode changes propagate. Kill switch activates/deactivates.
