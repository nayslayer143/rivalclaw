#!/usr/bin/env python3
from __future__ import annotations
"""
RivalClaw Telegram Dispatcher — standalone chat interface.

Polls @rivalclaw_bot for incoming messages. Three-layer handling:
  1. Commands (/status, /pnl, etc.) — direct DB queries, zero LLM cost
  2. Haiku chat — default conversational tier, ~$0.0004/msg
  3. Sonnet escalation — deep analysis, auto-detected, ~$0.005/msg

Run:  python3 ~/rivalclaw/rivalclaw_dispatcher.py
Stop: kill $(cat ~/rivalclaw/.dispatcher.lock)
"""

import os
import sys
import re
import json
import time
import signal
import sqlite3
import subprocess
import fcntl
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ── Env loading ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent

def _load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            # Override empty values (setdefault won't overwrite empty strings)
            if not os.environ.get(k):
                os.environ[k] = v

_load_env(_SCRIPT_DIR / ".env")

# ── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", _SCRIPT_DIR / "rivalclaw.db"))

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
FAST_MODEL = os.environ.get("RIVALCLAW_CHAT_MODEL", "qwen2.5:7b")
DEEP_MODEL = os.environ.get("RIVALCLAW_DEEP_MODEL", "qwen2.5:14b")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_INTERVAL = 5
OFFSET_FILE = Path("/tmp/rivalclaw-dispatcher-offset.txt")
LOCK_FILE = _SCRIPT_DIR / ".dispatcher.lock"
REPORT_PATH = _SCRIPT_DIR / "daily" / "hourly-latest.md"
SESSION_STATE_PATH = _SCRIPT_DIR / ".session-state.md"

_SHUTDOWN = False
_HISTORIES: dict[str, list] = {}  # chat_id -> [{"role":..,"content":..}]
_HISTORY_MAX = 10

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are RivalClaw — a focused arbitrage trading bot.

Identity:
- Mechanical over narrative
- Execution-first over theory-first
- Fast over exhaustive
- Skeptical over optimistic
- Minimal over feature-heavy

You are not a general-purpose assistant. You exist to capture real, executable edge from arbitrage opportunities. You trade on Polymarket and Kalshi.

Talk like a sharp, no-bullshit quant trader over text. Keep it short. Lead with the answer. No filler. No disclaimers.

When discussing trades or strategy, reference your actual data (provided below). Never speculate about numbers you don't have."""

# ── DB helpers ───────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_balance() -> dict:
    conn = _db()
    try:
        starting = float(conn.execute(
            "SELECT value FROM context WHERE chat_id='rivalclaw' AND key='starting_balance'"
        ).fetchone()[0])
        closed_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'"
        ).fetchone()[0]
        total_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        total_closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status != 'open'").fetchone()[0]
        total_wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE pnl > 0 AND status != 'open'").fetchone()[0]
        open_pos = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0]
        balance = starting + closed_pnl
        wr = (total_wins / total_closed * 100) if total_closed > 0 else 0
        roi = ((balance - starting) / starting * 100) if starting > 0 else 0
        return {
            "balance": balance, "starting": starting, "pnl": closed_pnl,
            "roi": roi, "total_trades": total_trades, "total_closed": total_closed,
            "wins": total_wins, "win_rate": wr, "open_positions": open_pos,
        }
    finally:
        conn.close()

def get_recent_trades(hours: int = 24) -> list[dict]:
    conn = _db()
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT market_id, direction, strategy, pnl, status, opened_at, closed_at, venue "
            "FROM paper_trades WHERE opened_at > ? OR (closed_at > ? AND closed_at IS NOT NULL) "
            "ORDER BY COALESCE(closed_at, opened_at) DESC",
            (cutoff, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_strategy_stats() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT strategy, COUNT(*) as total, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
            "ROUND(SUM(pnl), 2) as pnl "
            "FROM paper_trades WHERE status != 'open' "
            "GROUP BY strategy ORDER BY pnl DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_open_positions() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT market_id, direction, shares, entry_price, amount_usd, strategy, venue, opened_at "
            "FROM paper_trades WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_regime() -> dict:
    conn = _db()
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        rows = conn.execute(
            "SELECT price_usd FROM spot_prices WHERE crypto_id='bitcoin' "
            "AND fetched_at > ? ORDER BY fetched_at ASC", (cutoff,)
        ).fetchall()
        if len(rows) < 3:
            return {"regime": "unknown", "vol": 0, "trend": 0}
        prices = [r[0] for r in rows]
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5 * 100
        trend = (prices[-1] - prices[0]) / prices[0] * 100
        if vol < 0.2:
            regime = "calm"
        elif vol > 0.8:
            regime = "volatile"
        else:
            regime = "trending" if abs(trend) > 0.3 else "normal"
        return {"regime": regime, "vol": round(vol, 4), "trend": round(trend, 4)}
    finally:
        conn.close()

def get_graduation_status() -> dict:
    conn = _db()
    try:
        rows = conn.execute("SELECT date, balance, roi_pct FROM daily_pnl ORDER BY date ASC").fetchall()
        days = len(rows)
        if days == 0:
            return {"ready": False, "days": 0, "criteria": {}}
        roi_7d = rows[-1]["roi_pct"] if rows else 0
        # Win rate
        total_closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status != 'open'").fetchone()[0]
        total_wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE pnl > 0 AND status != 'open'").fetchone()[0]
        wr = (total_wins / total_closed * 100) if total_closed > 0 else 0
        # Max drawdown from daily balances
        peak = 0
        max_dd = 0
        for r in rows:
            bal = r["balance"]
            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        criteria = {
            "min_history_7d": days >= 7,
            "roi_positive": roi_7d > 0,
            "win_rate_55pct": wr > 55,
            "max_dd_below_25pct": max_dd < 0.25,
        }
        passed = sum(criteria.values())
        return {
            "ready": all(criteria.values()),
            "days": days, "roi_7d": roi_7d, "win_rate": wr,
            "max_drawdown": round(max_dd * 100, 1),
            "passed": f"{passed}/{len(criteria)}",
            "criteria": criteria,
        }
    finally:
        conn.close()

# ── Context builder ──────────────────────────────────────────────────────────

def _get_session_context() -> str:
    """Read Claude Code session state or fall back to tmux buffer."""
    # Prefer state file if recent
    if SESSION_STATE_PATH.exists():
        age = time.time() - SESSION_STATE_PATH.stat().st_mtime
        if age < 300:  # <5 min old
            content = SESSION_STATE_PATH.read_text().strip()
            if content:
                return f"### Claude Code Session\n{content[:1500]}"
    # Fallback: tmux capture
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", "claw-rival", "-p"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()[-50:]
            # Strip ANSI codes
            ansi_re = re.compile(r"\x1b\[[0-9;]*[mGKHJ]")
            clean = "\n".join(ansi_re.sub("", l) for l in lines)
            return f"### Claude Code Session (tmux)\n{clean[:1500]}"
    except Exception:
        pass
    return ""

def build_context() -> str:
    """Assemble full trading context for LLM injection."""
    parts = []

    # Trading state
    try:
        bal = get_balance()
        mode = os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper")
        kill = os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0") == "1"
        regime = get_regime()

        parts.append(
            f"### Trading State\n"
            f"Balance: ${bal['balance']:,.2f} (starting ${bal['starting']:,.0f})\n"
            f"PnL: ${bal['pnl']:+,.2f} ({bal['roi']:.1f}% ROI)\n"
            f"Win Rate: {bal['win_rate']:.1f}% ({bal['wins']}W/{bal['total_closed'] - bal['wins']}L)\n"
            f"Open Positions: {bal['open_positions']}\n"
            f"Total Trades: {bal['total_trades']}\n"
            f"Mode: {mode} | Kill Switch: {'ON' if kill else 'OFF'}\n"
            f"Regime: {regime['regime']} (vol={regime['vol']}%, trend={regime['trend']}%)"
        )
    except Exception as e:
        parts.append(f"### Trading State\n(error: {e})")

    # Last 24h trades
    try:
        trades = get_recent_trades(24)
        if trades:
            closed = [t for t in trades if t.get("pnl") is not None and t["status"] != "open"]
            opened = [t for t in trades if t["status"] == "open"]
            wins = sum(1 for t in closed if t["pnl"] > 0)
            total_pnl = sum(t["pnl"] for t in closed)

            header = (
                f"\n### Last 24h Trades\n"
                f"Opened: {len(opened)} | Closed: {len(closed)} | "
                f"Wins: {wins} | PnL: ${total_pnl:+,.2f}"
            )

            if len(closed) <= 50:
                lines = []
                for t in closed[:50]:
                    ticker = t["market_id"][:30]
                    lines.append(
                        f"  {t['strategy'][:15]:15s} {t['direction']:3s} "
                        f"${t['pnl']:+8.2f} {t['venue'][:4]} {ticker}"
                    )
                parts.append(header + "\n" + "\n".join(lines))
            else:
                # Summarize: last 1h detail + 23h aggregate
                one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
                recent = [t for t in closed if t.get("closed_at", "") > one_hour_ago]
                older = [t for t in closed if t.get("closed_at", "") <= one_hour_ago]
                lines = [header]
                if recent:
                    lines.append(f"\nLast 1h ({len(recent)} trades):")
                    for t in recent[:20]:
                        lines.append(
                            f"  {t['strategy'][:15]:15s} {t['direction']:3s} "
                            f"${t['pnl']:+8.2f} {t['venue'][:4]}"
                        )
                if older:
                    older_wins = sum(1 for t in older if t["pnl"] > 0)
                    older_pnl = sum(t["pnl"] for t in older)
                    older_wr = (older_wins / len(older) * 100) if older else 0
                    lines.append(
                        f"\nPrior 23h: {len(older)} trades | "
                        f"{older_wr:.0f}% WR | ${older_pnl:+,.2f}"
                    )
                parts.append("\n".join(lines))
        else:
            parts.append("\n### Last 24h Trades\nNo trades in last 24 hours.")
    except Exception as e:
        parts.append(f"\n### Last 24h Trades\n(error: {e})")

    # Strategy leaderboard
    try:
        stats = get_strategy_stats()
        if stats:
            lines = ["\n### Strategy Leaderboard"]
            for s in stats[:5]:
                wr = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
                lines.append(
                    f"  {s['strategy'][:20]:20s} {s['total']:4d} trades "
                    f"{wr:5.1f}% WR  ${s['pnl']:+10.2f}"
                )
            parts.append("\n".join(lines))
    except Exception:
        pass

    # Session context
    session = _get_session_context()
    if session:
        parts.append(f"\n{session}")

    # Daily report excerpt
    try:
        if REPORT_PATH.exists():
            report = REPORT_PATH.read_text().strip()
            if report:
                parts.append(f"\n### Latest Report (excerpt)\n{report[-1000:]}")
    except Exception:
        pass

    return "\n".join(parts)

# ── Ollama API ───────────────────────────────────────────────────────────────

def _ollama_call(model: str, system: str, messages: list, timeout: int = 30) -> str:
    """Call local Ollama. Returns assistant text."""
    ollama_msgs = [{"role": "system", "content": system}] + messages
    try:
        resp = requests.post(
            f"{OLLAMA_BASE}/api/chat",
            json={"model": model, "messages": ollama_msgs, "stream": False},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return f"(Ollama error: {resp.status_code})"
        data = resp.json()
        return data.get("message", {}).get("content", "").strip() or "(empty response)"
    except requests.exceptions.Timeout:
        return f"(Ollama timeout — {timeout}s. Try again.)"
    except requests.exceptions.ConnectionError:
        return "(Ollama not reachable — is it running?)"
    except Exception as e:
        return f"(error: {e})"

def chat_fast(history: list, message: str, context: str) -> str:
    system = f"{SYSTEM_PROMPT}\n\n{context}"
    msgs = history[-_HISTORY_MAX:] + [{"role": "user", "content": message}]
    return _ollama_call(FAST_MODEL, system, msgs, timeout=30)

def chat_deep(history: list, message: str, context: str) -> str:
    system = f"{SYSTEM_PROMPT}\n\n{context}"
    msgs = history[-_HISTORY_MAX:] + [{"role": "user", "content": message}]
    return _ollama_call(DEEP_MODEL, system, msgs, timeout=60)

# ── Escalation detection ────────────────────────────────────────────────────

_ESCALATION_PATTERNS = re.compile(
    r"\b(analy[sz]e|deep dive|explain why|compare.*strateg|"
    r"regime.*(impact|implicat|change)|backtest|"
    r"what.*(should|would).*(change|adjust|do differently)|"
    r"post[\s-]?mortem|root cause|why.*(losing|winning|underperform))\b",
    re.IGNORECASE,
)

def should_escalate(text: str) -> bool:
    if text.strip().lower().startswith("/deep"):
        return True
    return bool(_ESCALATION_PATTERNS.search(text))

# ── Command handlers ─────────────────────────────────────────────────────────

def handle_command(text: str) -> str | None:
    """Handle slash commands. Returns response string or None if not a command."""
    cmd = text.strip().lower().split()[0] if text.strip() else ""

    if cmd == "/status":
        bal = get_balance()
        mode = os.environ.get("RIVALCLAW_EXECUTION_MODE", "paper")
        kill = os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0") == "1"
        regime = get_regime()
        return (
            f"RIVALCLAW STATUS\n"
            f"Balance: ${bal['balance']:,.2f} ({bal['roi']:+.1f}% ROI)\n"
            f"PnL: ${bal['pnl']:+,.2f}\n"
            f"Win Rate: {bal['win_rate']:.1f}% ({bal['wins']}W/{bal['total_closed'] - bal['wins']}L)\n"
            f"Open: {bal['open_positions']} | Total: {bal['total_trades']}\n"
            f"Mode: {mode} | Kill: {'ON' if kill else 'OFF'}\n"
            f"Regime: {regime['regime']} (vol={regime['vol']}%)"
        )

    elif cmd == "/pnl":
        stats = get_strategy_stats()
        if not stats:
            return "No closed trades yet."
        lines = ["PNL BY STRATEGY"]
        total = 0
        for s in stats:
            wr = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
            lines.append(f"  {s['strategy'][:20]:20s}  {s['total']:4d}  {wr:5.1f}%  ${s['pnl']:+,.2f}")
            total += s["pnl"]
        lines.append(f"  {'TOTAL':20s}  {'':4s}  {'':5s}  ${total:+,.2f}")
        return "\n".join(lines)

    elif cmd == "/trades":
        trades = get_recent_trades(24)
        closed = [t for t in trades if t.get("pnl") is not None and t["status"] != "open"]
        if not closed:
            return "No trades closed in last 24h."
        wins = sum(1 for t in closed if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in closed)
        lines = [f"LAST 24H: {len(closed)} closed | {wins}W {len(closed)-wins}L | ${total_pnl:+,.2f}"]
        for t in closed[:30]:
            ticker = (t["market_id"] or "")[:25]
            lines.append(
                f"  {t['strategy'][:12]:12s} {t['direction']:3s} "
                f"${t['pnl']:+8.2f} {ticker}"
            )
        if len(closed) > 30:
            lines.append(f"  ... and {len(closed) - 30} more")
        return "\n".join(lines)

    elif cmd == "/positions":
        positions = get_open_positions()
        if not positions:
            return "No open positions."
        lines = [f"OPEN POSITIONS ({len(positions)})"]
        for p in positions[:30]:
            ticker = (p["market_id"] or "")[:25]
            lines.append(
                f"  {p['strategy'][:12]:12s} {p['direction']:3s} "
                f"${p['amount_usd']:.2f} @{p['entry_price']:.3f} {ticker}"
            )
        if len(positions) > 30:
            lines.append(f"  ... and {len(positions) - 30} more")
        return "\n".join(lines)

    elif cmd == "/strategies":
        stats = get_strategy_stats()
        if not stats:
            return "No strategy data yet."
        lines = ["STRATEGY LEADERBOARD"]
        for s in stats:
            wr = (s["wins"] / s["total"] * 100) if s["total"] > 0 else 0
            wl = f"{s['wins']}W/{s['losses']}L"
            lines.append(f"  {s['strategy'][:20]:20s}  {wl:10s}  {wr:5.1f}%  ${s['pnl']:+,.2f}")
        return "\n".join(lines)

    elif cmd == "/regime":
        r = get_regime()
        return f"Market Regime: {r['regime']}\nVolatility: {r['vol']}%\nTrend: {r['trend']}%"

    elif cmd == "/graduation":
        g = get_graduation_status()
        lines = [f"GRADUATION STATUS: {'READY' if g['ready'] else 'NOT READY'} ({g['passed']})"]
        lines.append(f"  History: {g['days']} days")
        if "roi_7d" in g:
            lines.append(f"  7d ROI: {g['roi_7d']:.1f}%")
            lines.append(f"  Win Rate: {g['win_rate']:.1f}%")
            lines.append(f"  Max Drawdown: {g['max_drawdown']}%")
        for k, v in g.get("criteria", {}).items():
            lines.append(f"  {'[x]' if v else '[ ]'} {k}")
        return "\n".join(lines)

    elif cmd == "/kill":
        current = os.environ.get("RIVALCLAW_LIVE_KILL_SWITCH", "0")
        new_val = "0" if current == "1" else "1"
        os.environ["RIVALCLAW_LIVE_KILL_SWITCH"] = new_val
        # Also update .env file
        env_path = _SCRIPT_DIR / ".env"
        if env_path.exists():
            content = env_path.read_text()
            content = re.sub(
                r"RIVALCLAW_LIVE_KILL_SWITCH=\d",
                f"RIVALCLAW_LIVE_KILL_SWITCH={new_val}",
                content,
            )
            env_path.write_text(content)
        status = "ON (trading halted)" if new_val == "1" else "OFF (trading active)"
        return f"Kill switch toggled: {status}"

    elif cmd == "/help":
        return (
            "RIVALCLAW COMMANDS\n"
            "/status     — Balance, PnL, win rate, mode\n"
            "/pnl        — P&L breakdown by strategy\n"
            "/trades     — Last 24h trade summary\n"
            "/positions  — Current open positions\n"
            "/strategies — Strategy leaderboard\n"
            "/regime     — Market regime (calm/trending/volatile)\n"
            "/graduation — Graduation gate status\n"
            "/kill       — Toggle kill switch\n"
            "/help       — This message\n\n"
            "Or just chat — I'll respond with full trading context."
        )

    return None  # Not a command

# ── Telegram bot ─────────────────────────────────────────────────────────────

def send_message(chat_id: str, text: str):
    """Send a Telegram message, chunking if needed."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            requests.post(
                f"{TG_API}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
        except Exception as e:
            print(f"[dispatcher] Send error: {e}")

def send_typing(chat_id: str):
    try:
        requests.post(
            f"{TG_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass

def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0

def save_offset(offset: int):
    OFFSET_FILE.write_text(str(offset))

def get_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"{TG_API}/getUpdates",
            params={"offset": offset, "timeout": POLL_INTERVAL},
            timeout=POLL_INTERVAL + 10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        print(f"[dispatcher] Poll error: {e}")
    return []

def handle_message(msg: dict):
    """Process a single incoming Telegram message."""
    chat_id = str(msg.get("chat", {}).get("id", ""))
    user_id = str(msg.get("from", {}).get("id", ""))
    text = msg.get("text", "").strip()

    if not text:
        return

    # Auth check
    if CHAT_ID and chat_id != CHAT_ID:
        print(f"[dispatcher] Ignored from unauthorized chat {chat_id}")
        return

    print(f"[dispatcher] Message from {user_id}: {text[:80]}")

    # Layer 1: Commands
    cmd_response = handle_command(text)
    if cmd_response is not None:
        send_message(chat_id, cmd_response)
        return

    # Layer 2/3: LLM chat
    send_typing(chat_id)

    # Build context
    context = build_context()

    # Get/init history
    if chat_id not in _HISTORIES:
        _HISTORIES[chat_id] = []
    history = _HISTORIES[chat_id]

    # Route to fast or deep model
    if should_escalate(text):
        print(f"[dispatcher] Escalating to {DEEP_MODEL}: {text[:50]}")
        reply = chat_deep(history, text, context)
    else:
        reply = chat_fast(history, text, context)

    # Update history
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    # Trim history
    if len(history) > _HISTORY_MAX * 2:
        _HISTORIES[chat_id] = history[-_HISTORY_MAX * 2:]

    send_message(chat_id, reply)

# ── Process management ───────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    try:
        fh = open(LOCK_FILE, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        # Keep file handle open to hold lock
        _acquire_lock._fh = fh
        return True
    except (IOError, OSError):
        return False

def _signal_handler(sig, frame):
    global _SHUTDOWN
    _SHUTDOWN = True

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global _SHUTDOWN

    if not BOT_TOKEN:
        print("[dispatcher] ERROR: TELEGRAM_BOT_TOKEN not set in rivalclaw/.env")
        sys.exit(1)

    if not _acquire_lock():
        print("[dispatcher] Another instance is running — exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"[dispatcher] RivalClaw dispatcher started (PID {os.getpid()})")
    print(f"[dispatcher] Bot token: ...{BOT_TOKEN[-6:]}")
    print(f"[dispatcher] Chat ID: {CHAT_ID or '(any)'}")
    print(f"[dispatcher] DB: {DB_PATH}")
    print(f"[dispatcher] Ollama: {OLLAMA_BASE}")
    print(f"[dispatcher] Fast: {FAST_MODEL} | Deep: {DEEP_MODEL}")

    offset = load_offset()
    print(f"[dispatcher] Polling @rivalclaw_bot (offset={offset})...")

    while not _SHUTDOWN:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            save_offset(offset)
            msg = update.get("message", {})
            if msg:
                try:
                    handle_message(msg)
                except Exception as e:
                    print(f"[dispatcher] Error handling message: {e}")
                    import traceback
                    traceback.print_exc()

    print("[dispatcher] Shutting down gracefully")
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
