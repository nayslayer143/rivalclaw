#!/usr/bin/env python3
"""
Balance watchdog — suspends live trading if balance drops to or below the floor.
Runs as a cron job every 5 minutes.
"""
import os
import sys
import logging
from datetime import datetime

FLOOR_USD = 25.00
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
LOG_PATH = os.path.join(os.path.dirname(__file__), "rivalclaw.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
)

os.environ["KALSHI_API_KEY_ID"] = "44dd8633-1448-4777-b41b-7f69a295b1e3"
os.environ["KALSHI_API_ENV"] = "prod"
os.environ["KALSHI_PRIVATE_KEY_PATH"] = "/Users/nayslayer/.kalshi/live-private.pem"

sys.path.insert(0, os.path.dirname(__file__))

try:
    import kalshi_executor
    result = kalshi_executor.get_balance()
    balance_cents = result.get("balance", 0)
    balance_usd = balance_cents / 100
except Exception as e:
    logging.error("Failed to fetch balance: %s", e)
    sys.exit(1)

if balance_usd > FLOOR_USD:
    # All good, nothing to do
    sys.exit(0)

# Read current kill switch state
with open(ENV_PATH) as f:
    content = f.read()

if "RIVALCLAW_LIVE_KILL_SWITCH=1" in content:
    # Already suspended
    sys.exit(0)

# Flip the kill switch
new_content = content.replace(
    "RIVALCLAW_LIVE_KILL_SWITCH=0",
    "RIVALCLAW_LIVE_KILL_SWITCH=1",
)

with open(ENV_PATH, "w") as f:
    f.write(new_content)

logging.warning(
    "KILL SWITCH ACTIVATED — balance $%.2f hit floor $%.2f. All live trading suspended.",
    balance_usd, FLOOR_USD,
)
print(
    f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
    f"WATCHDOG: balance ${balance_usd:.2f} <= floor ${FLOOR_USD:.2f} — kill switch activated."
)
