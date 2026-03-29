"""Kalshi API proxy routes — forwards requests to Kalshi via executor/feed modules."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

# Add rivalclaw root to sys.path so we can import kalshi_executor, kalshi_feed
sys.path.insert(0, str(Path(__file__).parent.parent))

import kalshi_executor
import kalshi_feed

router = APIRouter(prefix="/api/kalshi", tags=["kalshi"])


# ── Request models ──────────────────────────────────────────────────────────


class OrderRequest(BaseModel):
    ticker: str
    action: str
    side: str
    count: int
    yes_price: float
    type: str = "limit"
    client_order_id: Optional[str] = None


# ── Balance & Portfolio ─────────────────────────────────────────────────────


@router.get("/balance")
async def balance():
    try:
        return kalshi_executor.get_balance()
    except Exception as e:
        return {"error": str(e)}


@router.get("/positions")
async def positions():
    try:
        return kalshi_executor.get_positions()
    except Exception as e:
        return {"error": str(e)}


@router.get("/orders")
async def orders(status: Optional[str] = Query(None)):
    try:
        params = {}
        if status:
            params["status"] = status
        result = kalshi_feed._call_kalshi("GET", "/portfolio/orders", params=params)
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/fills")
async def fills(limit: int = Query(50, ge=1, le=500)):
    try:
        return kalshi_executor.get_fills(limit)
    except Exception as e:
        return {"error": str(e)}


@router.get("/settlements")
async def settlements(limit: int = Query(50, ge=1, le=500)):
    try:
        params = {"limit": limit}
        result = kalshi_feed._call_kalshi("GET", "/portfolio/settlements", params=params)
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


# ── Market Data ─────────────────────────────────────────────────────────────


@router.get("/market/{ticker}")
async def market(ticker: str):
    try:
        result = kalshi_feed._call_kalshi("GET", f"/markets/{ticker}")
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/market/{ticker}/orderbook")
async def orderbook(ticker: str):
    try:
        result = kalshi_feed._call_kalshi("GET", f"/markets/{ticker}/orderbook")
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/market/{ticker}/candlesticks")
async def candlesticks(ticker: str, period: str = Query("1m")):
    try:
        params = {"period": period}
        result = kalshi_feed._call_kalshi(
            "GET", f"/markets/{ticker}/candlesticks", params=params
        )
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


# ── Exchange ────────────────────────────────────────────────────────────────


@router.get("/exchange/status")
async def exchange_status():
    try:
        result = kalshi_feed._call_kalshi("GET", "/exchange/status")
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


# ── Order Management ────────────────────────────────────────────────────────


@router.post("/orders")
async def submit_order(order: OrderRequest):
    try:
        payload = kalshi_executor.build_order_payload(
            ticker=order.ticker,
            action=order.action,
            side=order.side,
            count=order.count,
            yes_price_dollars=order.yes_price,
        )
        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id
        return kalshi_executor.submit_order(payload)
    except Exception as e:
        return {"error": str(e)}


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    try:
        return kalshi_executor.cancel_order(order_id)
    except Exception as e:
        return {"error": str(e)}


@router.post("/orders/batch-cancel")
async def batch_cancel():
    try:
        return kalshi_executor.batch_cancel_orders()
    except Exception as e:
        return {"error": str(e)}


@router.get("/orders/{order_id}/queue")
async def order_queue(order_id: str):
    try:
        result = kalshi_feed._call_kalshi("GET", f"/portfolio/orders/{order_id}/queue")
        return result if result is not None else {"error": "kalshi_api_failed"}
    except Exception as e:
        return {"error": str(e)}


# ── Rate Limits ─────────────────────────────────────────────────────────────


@router.get("/rate-limits")
async def rate_limits():
    try:
        return kalshi_executor.get_rate_limit_usage()
    except Exception as e:
        return {"error": str(e)}
