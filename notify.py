#!/usr/bin/env python3
"""
RivalClaw notification system — sends hourly reports via Telegram.
Uses the @rivalclaw_bot Telegram bot token.

Setup: Set TELEGRAM_CHAT_ID in rivalclaw/.env
To get your chat ID: message the bot, then run:
  curl https://api.telegram.org/bot<TOKEN>/getUpdates | jq '.result[0].message.chat.id'
"""
from __future__ import annotations
import os
import requests
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8615622626:AAGwarVufm4u1TdUoKhPQUCb4-OhkJH-01A")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
REPORT_PATH = Path(__file__).parent / "daily" / "hourly-latest.md"


def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message via Telegram bot. Returns True on success."""
    if not CHAT_ID:
        print("[rivalclaw/notify] TELEGRAM_CHAT_ID not set — skipping notification")
        return False

    # Telegram has a 4096 char limit per message
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for chunk in chunks:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk, "parse_mode": parse_mode},
                timeout=10,
            )
            if resp.status_code != 200:
                # Retry without parse_mode if markdown fails
                resp = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": chunk},
                    timeout=10,
                )
            if resp.status_code == 200:
                print(f"[rivalclaw/notify] Telegram sent ({len(chunk)} chars)")
            else:
                print(f"[rivalclaw/notify] Telegram error: {resp.status_code} {resp.text[:100]}")
                return False
        except Exception as e:
            print(f"[rivalclaw/notify] Telegram failed: {e}")
            return False

    return True


def send_live_alert(event: str, details: str = "") -> bool:
    """Send a live trading alert via Telegram.

    Events: order_submitted, order_filled, order_rejected, kill_switch,
            mode_change, rate_limited, slippage_warning, balance_low
    """
    prefix = {
        "order_submitted": "ORDER SENT",
        "order_filled": "FILLED",
        "order_rejected": "REJECTED",
        "kill_switch": "KILL SWITCH ACTIVATED",
        "mode_change": "MODE CHANGE",
        "rate_limited": "RATE LIMITED",
        "slippage_warning": "SLIPPAGE WARNING",
        "balance_low": "LOW BALANCE",
    }.get(event, event.upper())
    msg = f"[RivalClaw LIVE] {prefix}\n{details}"
    return send_telegram(msg, parse_mode="")


def send_hourly_report():
    """Read the latest hourly report and send via Telegram."""
    if not REPORT_PATH.exists():
        print("[rivalclaw/notify] No hourly report found")
        return False

    report = REPORT_PATH.read_text()
    # Strip markdown table formatting for Telegram readability
    clean = report.replace("|", "│").replace("---", "───")
    return send_telegram(clean, parse_mode="")


if __name__ == "__main__":
    send_hourly_report()
