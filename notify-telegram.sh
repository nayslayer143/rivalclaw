#!/bin/bash
# =============================================================================
# notify-telegram.sh — RivalClaw edition
# Send a Telegram notification from @rivalclaw_bot to Jordan.
#
# Usage:
#   ./notify-telegram.sh "Your message here"
#   echo "Your message" | ./notify-telegram.sh
#
# Token priority:
#   1. RIVALCLAW_BOT_TOKEN from ~/openclaw/.env  (own bot — sends as @rivalclaw_bot)
#   2. TELEGRAM_BOT_TOKEN from ~/openclaw/.env   (fallback — sends as @ogdenclashbot with [RivalClaw] prefix)
# =============================================================================

set -euo pipefail

OPENCLAW_ENV="${HOME}/openclaw/.env"

# Load .env safely via Python (avoids bash issues with unquoted spaces in values)
if [[ -f "$OPENCLAW_ENV" ]]; then
  eval "$(python3 - "$OPENCLAW_ENV" <<'PYEOF'
import sys, os
path = sys.argv[1]
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip()
        # Only export simple safe keys
        if k.isidentifier() or all(c.isalnum() or c == '_' for c in k):
            print(f"export {k}={v!r}")
PYEOF
  )"
fi

# Resolve message: arg or stdin
if [[ $# -ge 1 ]]; then
  MESSAGE="$1"
else
  MESSAGE=$(cat)
fi

if [[ -z "${MESSAGE:-}" ]]; then
  echo "ERROR: No message provided" >&2
  exit 1
fi

# Pick bot token
RIVAL_TOKEN="${RIVALCLAW_BOT_TOKEN:-}"
MAIN_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
PREFIX=""

if [[ -n "$RIVAL_TOKEN" && "$RIVAL_TOKEN" != PASTE* ]]; then
  BOT_TOKEN="$RIVAL_TOKEN"
else
  # Fall back to main bot with label prefix
  if [[ -z "$MAIN_TOKEN" ]]; then
    echo "ERROR: No Telegram bot token found in ${OPENCLAW_ENV}" >&2
    exit 1
  fi
  BOT_TOKEN="$MAIN_TOKEN"
  PREFIX="[RivalClaw] "
fi

# Resolve chat_id from TELEGRAM_ALLOWED_USERS
CHAT_ID=$(python3 -c "
import os, json
v = os.environ.get('TELEGRAM_ALLOWED_USERS', '')
try:
    parsed = json.loads(v)
    if isinstance(parsed, list):
        print(parsed[0])
    else:
        print(str(parsed))
except Exception:
    print(v.strip())
")

if [[ -z "$CHAT_ID" ]]; then
  echo "ERROR: TELEGRAM_ALLOWED_USERS not set" >&2
  exit 1
fi

FULL_MESSAGE="${PREFIX}${MESSAGE}"

RESPONSE=$(curl -s -X POST \
  "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\": \"${CHAT_ID}\", \"text\": $(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' <<< "$FULL_MESSAGE"), \"parse_mode\": \"\"}")

OK=$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('ok', False))" <<< "$RESPONSE")

if [[ "$OK" == "True" ]]; then
  echo "✓ RivalClaw notification sent"
else
  echo "⚠ Send failed: ${RESPONSE}" >&2
  exit 1
fi
