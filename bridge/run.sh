#!/bin/bash
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a
cd bridge
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8400 --log-level info
