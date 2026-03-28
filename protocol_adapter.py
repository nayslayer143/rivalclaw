#!/usr/bin/env python3
"""
RivalClaw Protocol Adapter — Phase 1 integration.
Sits between simulator.py's trading loop and the openclaw_protocol engine.
Provides drop-in replacements for paper_wallet.execute_trade(), check_stops(),
and get_state() that route through the protocol.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid

from openclaw_protocol import ProtocolEngine, ProtocolConfig, InMemoryEventStore
from openclaw_protocol.store.sqlite import SqliteEventStore
from openclaw_protocol.config.defaults import DEFAULT_PROTOCOL_CONFIG
from openclaw_protocol.lock import FileExecutionLock
from openclaw_protocol.helpers import build_synthetic_book, build_synthetic_market, LiquidityProfile

# Zero-spread profile: asks are priced AT mid price so paper fills always succeed
_PAPER_PROFILE = LiquidityProfile(base_depth=1000, depth_decay=0.9, num_levels=8, spread_bps=0)
from openclaw_protocol.schemas.trade_intent import TradeIntent
from openclaw_protocol.schemas.base import ExecutionStatus, ExitReason
from openclaw_protocol.commands import CommandLog, ProtocolCommand
from openclaw_protocol.observability import ObservabilityStore, CycleReport
from openclaw_protocol.rollout import RolloutManager, RolloutConfig, RolloutMode

logger = logging.getLogger("rivalclaw.protocol")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_engine: ProtocolEngine | None = None
_command_log: CommandLog | None = None
_observability: ObservabilityStore | None = None
_rollout: RolloutManager | None = None
_lock: FileExecutionLock | None = None
_lock_key: str | None = None  # track acquired lock key for shutdown

BOT_ID = "rivalclaw"
INITIAL_BALANCE = 10000.0


# ---------------------------------------------------------------------------
# Task 1B — init_engine
# ---------------------------------------------------------------------------

def init_engine(db_dir: str | None = None) -> None:
    """Initialize protocol engine with persistent SQLite store.

    Idempotent — safe to call on every cycle entry. If already initialized
    this returns immediately without re-creating anything.
    """
    global _engine, _command_log, _observability, _rollout, _lock, _lock_key

    if _engine is not None:
        return  # Already initialized

    if db_dir is None:
        db_dir = os.path.expanduser("~/rivalclaw")

    os.makedirs(db_dir, exist_ok=True)

    events_db = os.path.join(db_dir, "protocol_events.db")
    commands_db = os.path.join(db_dir, "protocol_commands.db")
    obs_db = os.path.join(db_dir, "protocol_cycles.db")
    rollout_db = os.path.join(db_dir, "protocol_rollout.db")

    store = SqliteEventStore(events_db)

    config = ProtocolConfig(
        venues=DEFAULT_PROTOCOL_CONFIG.venues,
        profile=DEFAULT_PROTOCOL_CONFIG.profile,
        initial_balance=INITIAL_BALANCE,
    )

    _engine = ProtocolEngine(config=config, store=store)
    _command_log = CommandLog(commands_db)
    _observability = ObservabilityStore(obs_db)
    _rollout = RolloutManager(rollout_db)
    _lock = FileExecutionLock()

    # Create wallet — idempotent, returns existing if already created
    _engine.create_wallet(BOT_ID, INITIAL_BALANCE)

    # Verify integrity on startup
    if not _engine.verify_integrity(BOT_ID):
        logger.warning("Wallet integrity check failed on startup — rebuilding state")

    wallet = _engine.get_wallet(BOT_ID)
    logger.info(
        f"Protocol engine initialized. Wallet balance: {wallet.cash_balance}"
    )


# ---------------------------------------------------------------------------
# Task 1B — execute_trade
# ---------------------------------------------------------------------------

def execute_trade(decision, cycle_started_at_ms: float, cycle_id: str | None = None) -> dict | None:
    """Execute a trade through the protocol.

    Converts a RivalClaw decision object to a TradeIntent and routes it
    through the ProtocolEngine.  Returns a dict matching the legacy
    paper_wallet format on success, or None if rejected / failed.

    Args:
        decision: decision object with market_id, direction, entry_price,
                  amount_usd, shares, confidence, reasoning, strategy,
                  venue, metadata attributes.
        cycle_started_at_ms: timestamp (ms) when the current cycle started.
        cycle_id: optional cycle identifier; generated from uuid if None.
    """
    if _engine is None:
        raise RuntimeError("Protocol engine not initialized. Call init_engine() first.")

    if cycle_id is None:
        cycle_id = str(uuid.uuid4())[:8]

    now_ms = int(time.time() * 1000)

    # --- Extract decision fields defensively ---
    side = "BUY" if getattr(decision, "direction", "YES") == "YES" else "SELL"
    venue = getattr(decision, "venue", "polymarket")
    market_id = getattr(decision, "market_id", "")
    strategy = getattr(decision, "strategy", "unknown")
    confidence = float(getattr(decision, "confidence", 0.5))
    entry_price = float(getattr(decision, "entry_price", 0.5))
    amount_usd = float(getattr(decision, "amount_usd", 0))
    shares = int(getattr(decision, "shares", 0) or 0)
    reasoning = str(getattr(decision, "reasoning", ""))

    metadata = getattr(decision, "metadata", {}) or {}
    experiment_id = metadata.get("experiment_id", "rivalclaw_default")

    # Deterministic intent_id for idempotency
    intent_id = hashlib.sha256(
        f"{BOT_ID}:{cycle_id}:{market_id}:{side}:{strategy}".encode()
    ).hexdigest()[:16]

    # --- Build TradeIntent ---
    intent = TradeIntent(
        intent_id=intent_id,
        bot_id=BOT_ID,
        experiment_id=experiment_id,
        strategy_name=strategy,
        market_id=market_id,
        venue=venue,
        contract_id=market_id,  # RivalClaw uses market_id as contract_id
        side=side,
        target_price=entry_price,
        max_notional_usd=amount_usd,
        max_contracts=shares if shares > 0 else None,
        thesis_score=confidence,
        confidence=confidence,
        rationale_hash=(
            hashlib.sha256(reasoning.encode()).hexdigest()[:16] if reasoning else None
        ),
        signal_time_ms=int(cycle_started_at_ms),
        decision_time_ms=now_ms - 50,  # approximate decision time
        submit_time_ms=now_ms,
        protocol_version=_engine.config.protocol_version,
    )

    # --- Log command ---
    cmd = ProtocolCommand(
        command_id=str(uuid.uuid4()),
        bot_id=BOT_ID,
        cycle_id=cycle_id,
        command_type="entry_intent",
        source_agent=strategy,
        dedupe_key=intent_id,
        requested_at_ms=now_ms,
        status="accepted",
        market_id=market_id,
        contract_id=market_id,
        payload=intent.model_dump_json(),
    )
    _command_log.log_command(cmd)

    # --- Build synthetic market context ---
    book = build_synthetic_book(
        contract_id=market_id,
        venue=venue,
        price=entry_price,
        profile=_PAPER_PROFILE,
        fetched_at_ms=now_ms,
    )
    question = (
        getattr(decision, "question", market_id)
        if hasattr(decision, "question")
        else market_id
    )
    market = build_synthetic_market(
        market_id=market_id,
        venue=venue,
        title=question,
        contract_id=market_id,
        fetched_at_ms=now_ms,
    )

    # --- Execute through protocol ---
    try:
        result = _engine.execute_entry(intent, book, market)
    except Exception as e:
        _command_log.update_status(cmd.command_id, "failed", error_message=str(e))
        logger.error(f"Protocol execution failed for {market_id}: {e}")
        return None

    # --- Handle rejection ---
    if result.execution_status == ExecutionStatus.REJECTED:
        _command_log.update_status(
            cmd.command_id,
            "rejected",
            error_code=result.rejection_reason,
            error_message=result.rejection_reason,
        )
        logger.info(f"Trade rejected [{market_id}]: {result.rejection_reason}")
        return None

    _command_log.update_status(cmd.command_id, "executed")

    # --- Return legacy-compatible dict ---
    return {
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


# ---------------------------------------------------------------------------
# Task 1B — check_stops
# ---------------------------------------------------------------------------

def check_stops(current_prices: dict) -> list[dict]:
    """Check stop-loss and take-profit for all open positions.

    Args:
        current_prices: dict of {market_id: {"yes_price": float, "no_price": float}}

    Returns:
        List of closed position dicts (legacy-compatible format).
    """
    if _engine is None:
        return []

    STOP_LOSS_PCT = -0.20
    TAKE_PROFIT_PCT = 0.50

    closed = []
    positions = _engine.get_positions(BOT_ID)

    for pos in positions:
        price_data = current_prices.get(pos.contract_id)
        if not price_data:
            continue

        # Get current price based on position side
        if pos.side == "BUY":
            current_price = price_data.get("yes_price", pos.entry_price_avg)
        else:
            current_price = price_data.get("no_price", pos.entry_price_avg)

        # Calculate unrealized PnL percentage
        if pos.entry_price_avg > 0:
            if pos.side == "BUY":
                pnl_pct = (current_price - pos.entry_price_avg) / pos.entry_price_avg
            else:
                pnl_pct = (pos.entry_price_avg - current_price) / pos.entry_price_avg
        else:
            pnl_pct = 0.0

        # Determine if we should exit
        exit_reason = None
        if pnl_pct <= STOP_LOSS_PCT:
            exit_reason = ExitReason.STOP_LOSS
        elif pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = ExitReason.TAKE_PROFIT

        if exit_reason is None:
            continue

        # Execute exit through protocol
        now_ms = int(time.time() * 1000)
        try:
            exit_book = build_synthetic_book(
                contract_id=pos.contract_id,
                venue=pos.venue,
                price=current_price,
                fetched_at_ms=now_ms,
            )
            close = _engine.execute_exit(
                BOT_ID,
                pos.contract_id,
                current_price,
                exit_reason,
                exit_book,
            )
            closed.append({
                "id": close.close_id,
                "market_id": pos.contract_id,
                "direction": "YES" if pos.side == "BUY" else "NO",
                "exit_price": close.exit_price,
                "pnl": close.pnl_net,
                "status": "closed_win" if close.pnl_net > 0 else "closed_loss",
                "exit_reason": exit_reason.value,
            })
            logger.info(
                f"Position closed: {pos.contract_id} {exit_reason.value} pnl={close.pnl_net:.2f}"
            )
        except Exception as e:
            logger.error(f"Exit failed for {pos.contract_id}: {e}")

    return closed


# ---------------------------------------------------------------------------
# Task 1B — get_state
# ---------------------------------------------------------------------------

def get_state() -> dict:
    """Return wallet state in legacy-compatible format."""
    if _engine is None:
        return {"balance": 0, "open_positions": 0}

    wallet = _engine.get_wallet(BOT_ID)
    positions = _engine.get_positions(BOT_ID)

    return {
        "balance": wallet.cash_balance,
        "starting_balance": INITIAL_BALANCE,
        "open_positions": len(positions),
        "total_equity": wallet.total_equity,
    }


# ---------------------------------------------------------------------------
# Task 1B — shutdown
# ---------------------------------------------------------------------------

def get_open_market_ids() -> set:
    """Return set of market_ids with open protocol positions (for open_ids gating)."""
    if _engine is None:
        return set()
    positions = _engine.get_positions(BOT_ID)
    return {p.contract_id for p in positions}


def credit_resolution(market_id: str, pnl_net: float, exit_price: float) -> None:
    """Credit the protocol wallet when a trade resolves in paper_trades.

    For wins: credits back stake + profit.
    For losses: position is already debited; just close it in protocol state.
    This keeps protocol balance in sync with paper_trades outcomes.
    """
    if _engine is None:
        return
    try:
        positions = {p.contract_id: p for p in _engine.get_positions(BOT_ID)}
        pos = positions.get(market_id)
        if pos is None:
            # Position not in protocol (bridge gap) — just credit the net pnl directly
            if pnl_net > 0:
                _engine._wallet_mgr.credit(BOT_ID, pnl_net, "resolution_credit", market_id)
            return
        from openclaw_protocol.schemas.base import ExitReason
        from openclaw_protocol.helpers import build_synthetic_book
        now_ms = int(time.time() * 1000)
        exit_book = build_synthetic_book(
            contract_id=market_id,
            venue=pos.venue,
            price=exit_price,
            profile=_PAPER_PROFILE,
            fetched_at_ms=now_ms,
        )
        reason = ExitReason.TAKE_PROFIT if pnl_net >= 0 else ExitReason.STOP_LOSS
        _engine.execute_exit(BOT_ID, market_id, exit_price, reason, exit_book)
        logger.info(f"Protocol credited resolution: {market_id} pnl={pnl_net:.2f}")
    except Exception as e:
        logger.warning(f"Protocol credit_resolution failed for {market_id}: {e}")


def shutdown() -> None:
    """Clean shutdown — release lock, clear module state."""
    global _engine, _command_log, _observability, _rollout, _lock, _lock_key

    if _lock is not None and _lock_key is not None:
        try:
            _lock.release(_lock_key)
        except Exception as e:
            logger.warning(f"Lock release error: {e}")
        _lock_key = None

    # SQLite connections cleaned up by GC; reset module state
    _engine = None
    _command_log = None
    _observability = None
    _rollout = None
    _lock = None

    logger.info("Protocol adapter shutdown complete")
