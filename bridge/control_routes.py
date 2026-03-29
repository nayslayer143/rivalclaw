"""Control routes — execution mode, kill switch, config management."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

# Add rivalclaw root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import kalshi_executor

router = APIRouter(prefix="/api/control", tags=["control"])

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path.home() / "rivalclaw" / "rivalclaw.db"))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _write_context(key: str, value: str) -> None:
    """Write a key-value pair to the context table so the cron picks it up."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO context (chat_id, key, value) VALUES ('rivalclaw', ?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ── Request models ──────────────────────────────────────────────────────────


class ModeRequest(BaseModel):
    mode: str  # paper | shadow | live


class KillSwitchRequest(BaseModel):
    active: bool


# ── Execution Mode ──────────────────────────────────────────────────────────


@router.get("/mode")
async def get_mode():
    mode = os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper")
    return {"mode": mode}


@router.post("/mode")
async def set_mode(req: ModeRequest):
    try:
        if req.mode not in ("paper", "shadow", "live"):
            return {"error": f"Invalid mode: {req.mode}. Must be paper, shadow, or live."}

        os.environ["RIVALCLAW_EXECUTION_MODE"] = req.mode
        _write_context("execution_mode", req.mode)
        return {"mode": req.mode, "persisted": True}
    except Exception as e:
        return {"error": str(e)}


# ── Kill Switch ─────────────────────────────────────────────────────────────


@router.get("/kill-switch")
async def get_kill_switch():
    active = os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0") == "1"
    return {"active": active}


@router.post("/kill-switch")
async def set_kill_switch(req: KillSwitchRequest):
    try:
        value = "1" if req.active else "0"
        os.environ["RIVALCLAW_LIVE_KILL_SWITCH"] = value
        _write_context("live_kill_switch", value)

        result = {"active": req.active, "persisted": True}

        # If activating kill switch, cancel all resting orders
        if req.active:
            cancel_result = kalshi_executor.batch_cancel_orders()
            result["cancel_result"] = cancel_result

        return result
    except Exception as e:
        return {"error": str(e)}


# ── Config ──────────────────────────────────────────────────────────────────


@router.get("/config")
async def get_config():
    config = {}
    for key, value in os.environ.items():
        if key.startswith("RIVALCLAW_LIVE_"):
            config[key] = value
    return config


@router.post("/config")
async def set_config(updates: dict):
    try:
        applied = {}
        for key, value in updates.items():
            os.environ[key] = str(value)
            applied[key] = str(value)
        return {"applied": applied}
    except Exception as e:
        return {"error": str(e)}


# ── Sync ────────────────────────────────────────────────────────────────────


@router.post("/sync-balance")
async def sync_balance():
    try:
        return kalshi_executor.sync_account()
    except Exception as e:
        return {"error": str(e)}


@router.post("/sync-positions")
async def sync_positions():
    try:
        return kalshi_executor.get_positions()
    except Exception as e:
        return {"error": str(e)}
