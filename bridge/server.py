"""RivalClaw Bridge Server — exposes rivalclaw.db + Kalshi API for the ERS Dashboard."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add rivalclaw root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth import BearerAuthMiddleware
from db_routes import router as db_router
from kalshi_routes import router as kalshi_router
from control_routes import router as control_router

app = FastAPI(title="RivalClaw Bridge", version="1.0.0")

# CORS — allow the dashboard origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "ERS_CORS_ORIGINS", "https://eternalrevenueservice.com,http://localhost:3000"
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BearerAuthMiddleware)

app.include_router(db_router)
app.include_router(kalshi_router)
app.include_router(control_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rivalclaw-bridge"}
