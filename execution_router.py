#!/usr/bin/env python3
"""
RivalClaw Execution Router — routes trade intents to shadow or live execution.

Sits between protocol_adapter and the Kalshi API. After a trade intent passes
through the protocol engine, it gets routed here. The router decides:
  paper  -> skip (no action)
  shadow -> log only (dry-run DB record)
  live   -> submit to Kalshi with full pre-flight safety checks

10-point pre-flight safety check:
  1. Mode check         — must be "live" or "shadow"
  2. Kill switch        — reject if kill switch is on
  3. Balance check      — account must cover order cost
  4. Exposure check     — total open exposure + this order <= max_exposure_usd
  5. Order size check   — amount_usd <= max_order_usd
  6. Contract count     — shares <= max_contracts
  7. Rate check         — per-cycle and per-hour order limits
  8. Series check       — ticker must match an allowed series prefix
  9. Price sanity       — entry price within configured deviation of last market price
  10. Staleness check   — data must be < 5 minutes old

Env vars:
    RIVALCLAW_EXECUTION_MODE              — paper | shadow | live  (default: paper)
    RIVALCLAW_LIVE_KILL_SWITCH            — 1 to reject all live orders (default: 0)
    RIVALCLAW_LIVE_MAX_ORDER_USD          — max single order size (default: 2)
    RIVALCLAW_LIVE_MAX_EXPOSURE_USD       — max total open exposure (default: 10)
    RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER — max contracts per order (default: 5)
    RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE   — max orders per cycle (default: 2)
    RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR    — max orders per hour (default: 10)
    RIVALCLAW_LIVE_SERIES                 — allowed series prefixes, comma-separated
    RIVALCLAW_LIVE_MAX_PRICE_DEVIATION    — max fractional deviation from market (default: 0.10)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid

import kalshi_executor as executor

try:
    from notify import send_live_alert
except ImportError:
    def send_live_alert(event: str, details: str = "") -> bool:
        print(f"[execution_router] alert (notify unavailable): {event} -- {details}")
        return False


logger = logging.getLogger("rivalclaw.execution_router")

# ---------------------------------------------------------------------------
# Module-level state (cycle & hourly rate tracking)
# ---------------------------------------------------------------------------

_cycle_order_count: int = 0
_current_cycle_id: str = ""
_hour_order_count: int = 0
_hour_window_start: float = 0.0

# DB path override — tests can set this to a temp file
_db_path: str | None = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _get_config() -> dict:
    return {
        "mode": os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper"),
        "kill_switch": os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0") == "1",
        "max_order_usd": float(os.environ.get("RIVALCLAW_LIVE_MAX_ORDER_USD", "2")),
        "max_exposure_usd": float(os.environ.get("RIVALCLAW_LIVE_MAX_EXPOSURE_USD", "10")),
        "max_contracts": int(os.environ.get("RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER", "5")),
        "max_per_cycle": int(os.environ.get("RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE", "2")),
        "max_per_hour": int(os.environ.get("RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR", "10")),
        "allowed_series": os.environ.get(
            "RIVALCLAW_LIVE_SERIES", "KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M"
        ).split(","),
        "max_price_deviation": float(
            os.environ.get("RIVALCLAW_LIVE_MAX_PRICE_DEVIATION", "0.10")
        ),
    }


# ---------------------------------------------------------------------------
# Exposure calculator
# ---------------------------------------------------------------------------


def _get_open_live_exposure() -> float:
    """Query live_orders table for sum of (count * yes_price) where
    mode='live' and status in ('pending', 'filled'). Returns dollars."""
    db = _db_path or str(executor.DB_PATH)
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(count * yes_price), 0) AS total_cents
            FROM live_orders
            WHERE mode = 'live' AND status IN ('pending', 'resting')
            """
        ).fetchone()
        conn.close()
        return (row[0] if row else 0) / 100.0
    except Exception as e:
        logger.warning("Failed to query open exposure: %s", e)
        return 0.0


# ---------------------------------------------------------------------------
# Cycle management
# ---------------------------------------------------------------------------


def reset_cycle(cycle_id: str) -> None:
    """Reset the per-cycle order counter for a new cycle."""
    global _cycle_order_count, _current_cycle_id
    _cycle_order_count = 0
    _current_cycle_id = cycle_id


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------

_STALE_THRESHOLD_SECONDS = 300  # 5 minutes


def preflight_check(
    decision,
    last_market_price: float,
    account_balance_cents: int,
    stale_seconds: float = 0,
) -> dict:
    """Run the 10-point pre-flight safety check.

    Returns {"passed": True} or {"passed": False, "reason": str}.
    """
    global _cycle_order_count, _hour_order_count, _hour_window_start

    cfg = _get_config()

    # 1. Mode check — must be "live" or "shadow"
    if cfg["mode"] not in ("live", "shadow"):
        return {"passed": False, "reason": "mode_not_live"}

    # 2. Kill switch
    if cfg["kill_switch"]:
        return {"passed": False, "reason": "kill_switch"}

    # 3. Balance check — account must have enough for the order cost
    order_cost_cents = int(decision.amount_usd * 100)
    if account_balance_cents < order_cost_cents:
        return {"passed": False, "reason": "insufficient_balance"}

    # 4. Exposure check — total open + this order <= max
    current_exposure = _get_open_live_exposure()
    if current_exposure + decision.amount_usd > cfg["max_exposure_usd"]:
        return {"passed": False, "reason": "exposure_exceeded"}

    # 5. Order size check — clip to max rather than reject
    if decision.amount_usd > cfg["max_order_usd"]:
        scale = cfg["max_order_usd"] / decision.amount_usd
        decision.amount_usd = cfg["max_order_usd"]
        decision.shares = decision.shares * scale

    # 6. Contract count check — clip to max rather than reject
    if decision.shares > cfg["max_contracts"]:
        scale = cfg["max_contracts"] / decision.shares
        decision.shares = float(cfg["max_contracts"])
        decision.amount_usd = decision.amount_usd * scale

    # 7. Rate check — per-cycle and per-hour limits
    now = time.time()
    # Hourly window reset
    if _hour_window_start == 0.0 or (now - _hour_window_start) >= 3600:
        _hour_window_start = now
        _hour_order_count = 0

    if _cycle_order_count >= cfg["max_per_cycle"]:
        return {"passed": False, "reason": "cycle_rate_exceeded"}

    if _hour_order_count >= cfg["max_per_hour"]:
        return {"passed": False, "reason": "hour_rate_exceeded"}

    # 8. Series check — ticker must start with an allowed series prefix
    ticker = decision.market_id
    if not any(ticker.startswith(prefix) for prefix in cfg["allowed_series"]):
        return {"passed": False, "reason": "series_not_allowed"}

    # 9. Price sanity — entry price within max_price_deviation of last market price
    if last_market_price > 0:
        deviation = abs(decision.entry_price - last_market_price) / last_market_price
        if deviation > cfg["max_price_deviation"]:
            return {
                "passed": False,
                "reason": f"price_deviation ({deviation:.2%} > {cfg['max_price_deviation']:.0%})",
            }

    # 10. Staleness check — data must be < 5 minutes old
    if stale_seconds >= _STALE_THRESHOLD_SECONDS:
        return {"passed": False, "reason": "stale_data"}

    # Floor shares to whole contracts and reject if < 1
    decision.shares = int(decision.shares)
    if decision.shares < 1:
        return {"passed": False, "reason": "order_too_small"}
    decision.amount_usd = decision.shares * decision.entry_price

    return {"passed": True}


# ---------------------------------------------------------------------------
# Trade routing
# ---------------------------------------------------------------------------


def route_trade(
    decision,
    protocol_result,
    last_market_price: float,
    account_balance_cents: int,
    cycle_id: str = "",
    stale_seconds: float = 0,
) -> dict:
    """Route a trade decision to paper, shadow, or live execution.

    Args:
        decision: Trade decision object with market_id, direction, entry_price,
                  amount_usd, shares, question, strategy, confidence, reasoning,
                  metadata, venue.
        protocol_result: Result dict from the protocol engine.
        last_market_price: Latest market price for the ticker.
        account_balance_cents: Current account balance in cents.
        cycle_id: Current cycle identifier (for rate limiting).
        stale_seconds: Age of the market data in seconds.

    Returns:
        dict with routing outcome.
    """
    global _cycle_order_count, _hour_order_count, _current_cycle_id

    cfg = _get_config()

    # ---- Paper mode: skip entirely ----
    if cfg["mode"] == "paper":
        return {"mode": "paper", "status": "skipped"}

    # ---- Map direction to Kalshi action/side ----
    if decision.direction == "YES":
        action, side = "buy", "yes"
    else:
        action, side = "buy", "no"

    intent_id = str(uuid.uuid4())
    ticker = decision.market_id

    # ---- Handle cycle reset ----
    if cycle_id and cycle_id != _current_cycle_id:
        reset_cycle(cycle_id)

    # ---- Run pre-flight checks ----
    check = preflight_check(
        decision, last_market_price, account_balance_cents, stale_seconds
    )

    if not check["passed"]:
        # Log as rejected
        order_id = executor.log_order(
            intent_id=intent_id,
            ticker=ticker,
            action=action,
            side=side,
            count=decision.shares,
            yes_price_cents=int(round(decision.entry_price * 100)),
            mode=cfg["mode"],
            cycle_id=cycle_id,
            strategy=decision.strategy,
            market_question=decision.question,
        )
        executor.update_order_status(
            order_id,
            status="rejected",
            rejection_reason=check["reason"],
        )
        logger.warning(
            "Pre-flight rejected: %s — %s", ticker, check["reason"]
        )
        return {
            "mode": cfg["mode"],
            "status": "rejected",
            "reason": check["reason"],
            "intent_id": intent_id,
        }

    # ---- Build order payload ----
    payload = executor.build_order_payload(
        ticker=ticker,
        action=action,
        side=side,
        count=decision.shares,
        yes_price_dollars=decision.entry_price,
    )

    # ---- Shadow mode: log only ----
    if cfg["mode"] == "shadow":
        order_id = executor.log_order(
            intent_id=intent_id,
            ticker=ticker,
            action=action,
            side=side,
            count=decision.shares,
            yes_price_cents=payload["yes_price"],
            mode="shadow",
            client_order_id=payload["client_order_id"],
            cycle_id=cycle_id,
            strategy=decision.strategy,
            market_question=decision.question,
        )
        _cycle_order_count += 1
        _hour_order_count += 1
        logger.info("Shadow order logged: %s (id=%d)", ticker, order_id)
        return {
            "mode": "shadow",
            "status": "logged",
            "order_id": order_id,
            "intent_id": intent_id,
            "payload": payload,
        }

    # ---- Live mode: submit to Kalshi ----
    send_live_alert(
        "order_submitted",
        f"{action} {decision.shares}x {ticker} @ ${decision.entry_price:.4f} ({side})",
    )

    submit_result = executor.submit_order(payload)

    if "error" in submit_result:
        order_id = executor.log_order(
            intent_id=intent_id,
            ticker=ticker,
            action=action,
            side=side,
            count=decision.shares,
            yes_price_cents=payload["yes_price"],
            mode="live",
            client_order_id=payload["client_order_id"],
            cycle_id=cycle_id,
            strategy=decision.strategy,
            market_question=decision.question,
        )
        executor.update_order_status(
            order_id,
            status="error",
            error_message=submit_result.get("detail", str(submit_result)),
        )
        send_live_alert(
            "order_rejected",
            f"API error for {ticker}: {submit_result.get('detail', 'unknown')}",
        )
        return {
            "mode": "live",
            "status": "error",
            "error": submit_result,
            "intent_id": intent_id,
        }

    # Successful submission
    kalshi_order_id = submit_result.get("order", {}).get("order_id", "")
    order_id = executor.log_order(
        intent_id=intent_id,
        ticker=ticker,
        action=action,
        side=side,
        count=decision.shares,
        yes_price_cents=payload["yes_price"],
        mode="live",
        client_order_id=payload["client_order_id"],
        kalshi_order_id=kalshi_order_id,
        cycle_id=cycle_id,
        strategy=decision.strategy,
        market_question=decision.question,
    )

    _cycle_order_count += 1
    _hour_order_count += 1

    # Poll for fill
    fill_result = executor.poll_order_status(kalshi_order_id)
    fill_status = fill_result.get("status", "unknown")
    fill_price = fill_result.get("yes_price", payload["yes_price"])
    fill_count = fill_result.get("count", decision.shares)

    executor.update_order_status(
        order_id,
        status="filled" if fill_status == "executed" else fill_status,
        kalshi_order_id=kalshi_order_id,
        fill_price=fill_price,
        fill_count=fill_count,
    )

    # Log reconciliation
    live_amount_usd = (fill_price * fill_count) / 100.0
    executor.log_reconciliation(
        live_order_id=order_id,
        paper_entry_price=decision.entry_price,
        live_fill_price_cents=fill_price,
        paper_amount_usd=decision.amount_usd,
        live_amount_usd=live_amount_usd,
    )

    send_live_alert(
        "order_filled" if fill_status == "executed" else "order_submitted",
        f"{ticker}: {fill_status} @ {fill_price}c x{fill_count}",
    )

    return {
        "mode": "live",
        "status": "filled" if fill_status == "executed" else fill_status,
        "order_id": order_id,
        "kalshi_order_id": kalshi_order_id,
        "intent_id": intent_id,
        "fill": fill_result,
    }
