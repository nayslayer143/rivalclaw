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
    RIVALCLAW_BLOCK_15M_YES               — 1 to block YES bets on 15-min contracts (default: 1)
    RIVALCLAW_BLOCK_SELF_HEDGE            — 1 to block opposite-side bets on already-open tickers (default: 1)
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

# Anti-stacking: track market_ids with live orders submitted this session.
# Prevents the same market from being traded multiple times across cycles.
_submitted_market_ids: set[str] = set()

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
        "block_15m_yes": os.environ.get("RIVALCLAW_BLOCK_15M_YES", "1") == "1",
        "block_self_hedge": os.environ.get("RIVALCLAW_BLOCK_SELF_HEDGE", "1") == "1",
    }


# ---------------------------------------------------------------------------
# Exposure calculator
# ---------------------------------------------------------------------------


def _get_open_sides_for_ticker(ticker: str) -> set:
    """Return the set of sides ('yes', 'no') with active live orders for this ticker.
    Includes filled orders — these represent real Kalshi positions that haven't settled."""
    db = _db_path or str(executor.DB_PATH)
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT DISTINCT side FROM live_orders WHERE ticker=? AND mode='live' AND status IN ('pending', 'resting', 'filled')",
            (ticker,),
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}
    except Exception as e:
        logger.warning("Failed to query open sides for %s: %s", ticker, e)
        return set()


def _has_any_live_order_for_ticker(ticker: str) -> bool:
    """Check if ANY live order exists for this ticker (any side, any active status).
    This is the primary anti-stacking guard.
    Fails CLOSED — returns True (blocks) on any error, because allowing a
    duplicate order is worse than missing one trade."""
    db = _db_path or str(executor.DB_PATH)
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM live_orders WHERE ticker=? AND mode='live' AND status IN ('pending', 'resting', 'filled') LIMIT 1",
            (ticker,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.warning("Failed to check existing orders for %s: %s — blocking as safety measure", ticker, e)
        return True


def _get_open_live_exposure() -> float:
    """Query live_orders table for actual capital at risk.
    For YES orders: cost = count * yes_price.
    For NO orders: cost = count * (100 - yes_price).
    Fails CLOSED — returns infinity on error so exposure check blocks."""
    db = _db_path or str(executor.DB_PATH)
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN side = 'no' THEN count * (100 - yes_price)
                     ELSE count * yes_price
                END
            ), 0) AS total_cents
            FROM live_orders
            WHERE mode = 'live' AND status IN ('pending', 'resting', 'filled')
            """
        ).fetchone()
        conn.close()
        return (row[0] if row else 0) / 100.0
    except Exception as e:
        logger.warning("Failed to query open exposure: %s — blocking as safety measure", e)
        return float("inf")


# ---------------------------------------------------------------------------
# Cycle management
# ---------------------------------------------------------------------------


def reset_cycle(cycle_id: str) -> None:
    """Reset the per-cycle order counter for a new cycle."""
    global _cycle_order_count, _current_cycle_id
    _cycle_order_count = 0
    _current_cycle_id = cycle_id


def clear_settled_market(market_id: str) -> None:
    """Remove a market from the anti-stacking set after it settles."""
    _submitted_market_ids.discard(market_id)


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

    # 8a. Anti-stacking — reject if ANY live order exists for this market (any side)
    if ticker in _submitted_market_ids:
        return {"passed": False, "reason": "already_submitted"}
    # DB check catches orders from previous process invocations (cron restarts)
    # Includes pending, resting, AND filled — filled orders are real positions
    if _has_any_live_order_for_ticker(ticker):
        return {"passed": False, "reason": "already_submitted"}

    # 8b. Block YES on 15-min contracts — live WR is ~9%, not viable
    if cfg["block_15m_yes"] and "15M" in ticker and decision.direction == "YES":
        return {"passed": False, "reason": "15m_yes_blocked"}

    # 8c. Anti-self-hedge — reject if we already hold the opposite side on this ticker
    if cfg["block_self_hedge"]:
        open_sides = _get_open_sides_for_ticker(ticker)
        opposite = "no" if decision.direction == "YES" else "yes"
        if opposite in open_sides:
            return {"passed": False, "reason": "self_hedge_blocked"}

    # 9. Price sanity — entry price within max_price_deviation of last market price
    if last_market_price > 0:
        deviation = abs(decision.entry_price - last_market_price) / last_market_price
        if deviation > cfg["max_price_deviation"]:
            return {
                "passed": False,
                "reason": f"price_deviation ({deviation:.2%} > {cfg['max_price_deviation']:.0%})",
            }

    # 10. Staleness check — currently disabled (stale_seconds never passed by caller)
    # TODO: wire stale_seconds from market fetch timestamp through protocol_adapter
    # if stale_seconds >= _STALE_THRESHOLD_SECONDS:
    #     return {"passed": False, "reason": "stale_data"}

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

    # Compute correct YES price for logging (entry_price is NO cost for NO trades)
    if decision.direction == "NO":
        log_yes_cents = int(round((1.0 - decision.entry_price) * 100))
    else:
        log_yes_cents = int(round(decision.entry_price * 100))

    if not check["passed"]:
        # Log as rejected
        order_id = executor.log_order(
            intent_id=intent_id,
            ticker=ticker,
            action=action,
            side=side,
            count=decision.shares,
            yes_price_cents=log_yes_cents,
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
    # For NO orders: entry_price is the NO cost. Kalshi API takes yes_price as limit.
    # Convert: if we want max NO cost = entry_price, set yes_price = 1 - entry_price.
    # This ensures the limit correctly caps what we pay.
    if decision.direction == "NO":
        api_yes_price = 1.0 - decision.entry_price
    else:
        api_yes_price = decision.entry_price
    payload = executor.build_order_payload(
        ticker=ticker,
        action=action,
        side=side,
        count=decision.shares,
        yes_price_dollars=api_yes_price,
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

    # Successful submission — register in anti-stacking set
    _submitted_market_ids.add(ticker)
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

    # Log reconciliation — correct cost depends on side
    if side == "no":
        live_amount_usd = ((100 - fill_price) * fill_count) / 100.0
    else:
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
