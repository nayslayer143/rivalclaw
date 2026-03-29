"""Bearer token auth middleware for the bridge server."""
from __future__ import annotations

import os

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

API_KEY = os.environ.get("ERS_BRIDGE_API_KEY", "ers-dev-key-change-me")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Allow health check and CORS preflight without auth
        if request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)
