#!/bin/bash
# =============================================================================
# notify-telegram.sh — RivalClaw edition
# Send a Telegram notification from @rivalclaw_bot to Jordan.
#
# Usage:
#   ./notify-telegram.sh "Your message here"
#   echo "Your message" | ./notify-telegram.sh
#
# Token source: TELEGRAM_BOT_TOKEN from ~/rivalclaw/.env (@rivalclaw_bot)
# =============================================================================

set -euo pipefail

RIVALCLAW_ENV="${HOME}/rivalclaw/.env"

# Load RivalClaw's own .env
if [[ -f "$RIVALCLAW_ENV" ]]; then
  eval "$(python3 - "$RIVALCLAW_ENV" <<'PYEOF'
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

# Use RivalClaw's own bot token
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"

if [[ -z "$BOT_TOKEN" ]]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not set in ${RIVALCLAW_ENV}" >&2
  exit 1
fi

CHAT_ID="${TELEGRAM_CHAT_ID:-}"

if [[ -z "$CHAT_ID" ]]; then
  echo "ERROR: TELEGRAM_ALLOWED_USERS not set" >&2
  exit 1
fi

FULL_MESSAGE="${MESSAGE}"

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
