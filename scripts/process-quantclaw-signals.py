#!/usr/bin/env python3
"""
process-quantclaw-signals.py — Consume QuantumentalClaw signals and route via RivalClaw.

Reads PENDING rows from ~/rivalclaw/quantclaw_signals.db (written by
QuantumentalClaw's rivalclaw_bridge.py) and routes each signal through
RivalClaw's execution_router. Marks rows PROCESSED or FAILED.

Run manually: python3 scripts/process-quantclaw-signals.py
Via cron: already scheduled alongside rivalclaw cron (add if not present)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import execution_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("rivalclaw.quantclaw-consumer")

QUEUE_DB = os.path.expanduser(
    os.getenv("QUANTCLAW_SIGNAL_QUEUE_DB", "~/rivalclaw/quantclaw_signals.db")
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(QUEUE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_balance_cents() -> int:
    """Best-effort balance from rivalclaw.db context table."""
    try:
        rc_db = Path("~/rivalclaw/rivalclaw.db").expanduser()
        conn = sqlite3.connect(str(rc_db), timeout=5)
        row = conn.execute(
            "SELECT value FROM context WHERE key='account_balance_cents'"
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 100_000  # default $1000
    except Exception:
        return 100_000


def process_pending():
    if not Path(QUEUE_DB).exists():
        log.info("Queue DB not found — nothing to process")
        return 0

    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM quantclaw_signals WHERE status='PENDING' ORDER BY created_at ASC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        log.debug("No PENDING signals")
        return 0

    log.info(f"Processing {len(rows)} PENDING signal(s)")
    processed = 0
    balance_cents = _get_balance_cents()

    for row in rows:
        row_id = row["id"]
        decision_id = row["decision_id"]
        log.info(f"Signal {decision_id}: {row['venue']}/{row['ticker']} {row['direction']} ${row['amount_usd']:.2f}")

        # Build a decision object RivalClaw's router can consume
        decision = SimpleNamespace(
            market_id=row["ticker"] or row["event_id"],
            direction=row["direction"],
            entry_price=row["entry_price"],
            amount_usd=row["amount_usd"],
            shares=row["shares"],
            question=row["event_id"],
            strategy="quantclaw_signal",
            confidence=row["confidence"],
            reasoning=row["reasoning"] or "",
            venue=row["venue"],
            metadata=json.loads(row["signal_json"]) if row["signal_json"] else {},
        )

        # Minimal protocol_result stub (RivalClaw expects this from its protocol engine)
        protocol_result = {
            "approved": True,
            "decision_id": decision_id,
            "source": "quantclaw_bridge",
        }

        try:
            result = execution_router.route_trade(
                decision=decision,
                protocol_result=protocol_result,
                last_market_price=row["entry_price"],
                account_balance_cents=balance_cents,
                cycle_id=f"qc-{int(time.time())}",
                stale_seconds=30,  # signals are fresh from QuantumentalClaw
            )
            status_out = result.get("status", "unknown")
            log.info(f"  → {status_out} (mode: {result.get('mode','?')})")

            # Mark processed
            conn = _get_conn()
            conn.execute(
                "UPDATE quantclaw_signals SET status='PROCESSED', processed_at=datetime('now') WHERE id=?",
                (row_id,)
            )
            conn.commit()
            conn.close()
            processed += 1

        except Exception as e:
            log.error(f"  → FAILED: {e}")
            try:
                conn = _get_conn()
                conn.execute(
                    "UPDATE quantclaw_signals SET status='FAILED', processed_at=datetime('now') WHERE id=?",
                    (row_id,)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    return processed


if __name__ == "__main__":
    n = process_pending()
    log.info(f"Done — processed {n} signal(s)")
