#!/usr/bin/env python3
"""
RivalClaw self-tuner — mechanical parameter adjustment based on realized data.
Runs daily via cron. No LLM. Math only.

Three tuning loops:
  1. Realized Volatility — compute from spot_prices table
  2. Strategy Scoring — adjust thresholds based on edge capture rate
  3. Spread-Based Slippage — calibrate against Kalshi bid-ask spreads
"""
from __future__ import annotations
import datetime
import math
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))
ENV_PATH = Path(__file__).parent / ".env"

# Tuner configuration
LOOKBACK_DAYS = int(os.environ.get("RIVALCLAW_TUNER_LOOKBACK_DAYS", "7"))
MAX_ADJUST_PCT = float(os.environ.get("RIVALCLAW_TUNER_MAX_ADJUST_PCT", "0.20"))
MIN_TRADES = int(os.environ.get("RIVALCLAW_TUNER_MIN_TRADES", "10"))
MIN_SNAPSHOTS = int(os.environ.get("RIVALCLAW_TUNER_MIN_SNAPSHOTS", "500"))
MIN_SPREAD_SAMPLES = int(os.environ.get("RIVALCLAW_TUNER_MIN_SPREAD_SAMPLES", "50"))
ROLLBACK_THRESHOLD = float(os.environ.get("RIVALCLAW_TUNER_ROLLBACK_THRESHOLD", "-0.05"))
COOLDOWN_DAYS = int(os.environ.get("RIVALCLAW_TUNER_COOLDOWN_DAYS", "3"))

# Parameter clamps: {env_var: (min, max, default)}
CLAMPS = {
    "RIVALCLAW_VOL_BITCOIN": (0.30, 1.50, 0.60),
    "RIVALCLAW_VOL_ETHEREUM": (0.30, 1.50, 0.65),
    "RIVALCLAW_VOL_DOGECOIN": (0.30, 1.50, 0.90),
    "RIVALCLAW_VOL_CARDANO": (0.30, 1.50, 0.80),
    "RIVALCLAW_VOL_BINANCECOIN": (0.30, 1.50, 0.65),
    "RIVALCLAW_VOL_BITCOIN_CASH": (0.30, 1.50, 0.75),
    "ARB_MIN_EDGE": (0.003, 0.05, 0.005),
    "RIVALCLAW_MIN_FV_EDGE": (0.02, 0.15, 0.04),
    "RIVALCLAW_MIN_MOMENTUM_PRICE": (0.70, 0.90, 0.78),
    "RIVALCLAW_SLIPPAGE_BPS": (10, 100, 50),
}

STRATEGY_PARAM = {
    "arbitrage": "ARB_MIN_EDGE",
    "fair_value_directional": "RIVALCLAW_MIN_FV_EDGE",
    "near_expiry_momentum": "RIVALCLAW_MIN_MOMENTUM_PRICE",
}

VOL_TO_CRYPTO = {
    "RIVALCLAW_VOL_BITCOIN": "bitcoin",
    "RIVALCLAW_VOL_ETHEREUM": "ethereum",
    "RIVALCLAW_VOL_DOGECOIN": "dogecoin",
    "RIVALCLAW_VOL_CARDANO": "cardano",
    "RIVALCLAW_VOL_BINANCECOIN": "binancecoin",
    "RIVALCLAW_VOL_BITCOIN_CASH": "bitcoin-cash",
}


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _read_env():
    vals = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
    return vals


def _write_env(vals):
    tmp = ENV_PATH.parent / ".env.tmp"
    tmp.write_text("\n".join(f"{k}={v}" for k, v in sorted(vals.items())) + "\n")
    os.rename(str(tmp), str(ENV_PATH))


def _get_current(env_vals, param):
    _, _, default = CLAMPS[param]
    return float(env_vals.get(param, str(default)))


def _clamp_and_cap(current, raw_new, param):
    lo, hi, _ = CLAMPS[param]
    max_delta = abs(current) * MAX_ADJUST_PCT
    delta = raw_new - current
    if abs(delta) > max_delta:
        delta = max_delta if delta > 0 else -max_delta
    return max(lo, min(hi, current + delta))


def _log(conn, date, param, old, new, reason, sample_size):
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO tuning_log (date, parameter, old_value, new_value, reason, sample_size, tuned_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (date, param, old, new, reason, sample_size, now))


# ---------------------------------------------------------------------------
# Rollback check
# ---------------------------------------------------------------------------

def _check_rollback(conn, env_vals, today):
    cooldown = conn.execute(
        "SELECT new_value FROM tuning_log WHERE parameter='cooldown' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if cooldown:
        try:
            expiry_num = str(int(cooldown["new_value"]))
            expiry = f"{expiry_num[:4]}-{expiry_num[4:6]}-{expiry_num[6:8]}"
            if today < expiry:
                print(f"[tuner] In cooldown until {expiry}. Skipping.")
                return True
        except (ValueError, IndexError):
            pass

    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    changes = conn.execute(
        "SELECT COUNT(*) as cnt FROM tuning_log WHERE date=? AND parameter != 'none' AND parameter != 'cooldown'",
        (yesterday,)).fetchone()["cnt"]
    if changes == 0:
        return False

    roi_row = conn.execute(
        "SELECT roi_pct FROM daily_pnl ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if roi_row and roi_row["roi_pct"] is not None and roi_row["roi_pct"] < ROLLBACK_THRESHOLD:
        prev_path = ENV_PATH.parent / ".env.prev"
        if prev_path.exists():
            os.rename(str(prev_path), str(ENV_PATH))
            cooldown_expiry = (datetime.date.fromisoformat(today) +
                               datetime.timedelta(days=COOLDOWN_DAYS)).isoformat()
            _log(conn, today, "rollback", 0, 0,
                 f"ROI {roi_row['roi_pct']:.4f} < {ROLLBACK_THRESHOLD}, reverted .env", 0)
            _log(conn, today, "cooldown", 0, float(cooldown_expiry.replace("-", "")),
                 f"Cooldown until {cooldown_expiry}", 0)
            conn.commit()
            print(f"[tuner] ROLLBACK triggered (ROI={roi_row['roi_pct']:.2%}). Cooldown until {cooldown_expiry}")
            return True
    return False


# ---------------------------------------------------------------------------
# Loop 1: Realized Volatility
# ---------------------------------------------------------------------------

def _tune_volatility(conn, env_vals, today, cutoff):
    adjustments = {}
    for env_var, crypto_id in VOL_TO_CRYPTO.items():
        rows = conn.execute(
            "SELECT price_usd FROM spot_prices WHERE crypto_id=? AND fetched_at>? ORDER BY fetched_at ASC",
            (crypto_id, cutoff)).fetchall()

        if len(rows) < MIN_SNAPSHOTS:
            print(f"[tuner/vol] {crypto_id}: {len(rows)} snapshots < {MIN_SNAPSHOTS} min. Skipping.")
            continue

        prices = [r["price_usd"] for r in rows if r["price_usd"] and r["price_usd"] > 0]
        if len(prices) < MIN_SNAPSHOTS:
            continue

        log_returns = []
        for i in range(1, len(prices)):
            if prices[i] > 0 and prices[i-1] > 0:
                log_returns.append(math.log(prices[i] / prices[i-1]))

        if len(log_returns) < 10:
            continue

        # Annualize: 30 observations/hour * 24h * 365.25d
        periods_per_year = 365.25 * 24 * 30
        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r)**2 for r in log_returns) / len(log_returns)
        std_dev = math.sqrt(variance)
        realized_vol = std_dev * math.sqrt(periods_per_year)

        current = _get_current(env_vals, env_var)
        new_val = _clamp_and_cap(current, realized_vol, env_var)

        if abs(new_val - current) > 0.001:
            adjustments[env_var] = new_val
            _log(conn, today, env_var, current, new_val,
                 f"Realized vol={realized_vol:.3f} from {len(log_returns)} returns [{crypto_id}]",
                 len(log_returns))
            print(f"[tuner/vol] {crypto_id}: {current:.3f} -> {new_val:.3f} (realized={realized_vol:.3f})")
        else:
            print(f"[tuner/vol] {crypto_id}: {current:.3f} unchanged (realized={realized_vol:.3f})")

    return adjustments


# ---------------------------------------------------------------------------
# Loop 2: Strategy Scoring
# ---------------------------------------------------------------------------

def _tune_strategies(conn, env_vals, today, cutoff):
    adjustments = {}
    for strategy, param in STRATEGY_PARAM.items():
        rows = conn.execute(
            "SELECT pnl, expected_edge FROM paper_trades "
            "WHERE strategy=? AND status != 'open' AND closed_at>? AND expected_edge IS NOT NULL",
            (strategy, cutoff)).fetchall()

        if len(rows) < MIN_TRADES:
            print(f"[tuner/strat] {strategy}: {len(rows)} trades < {MIN_TRADES} min. Skipping.")
            continue

        total_pnl = sum(r["pnl"] or 0 for r in rows)
        total_expected = sum(abs(r["expected_edge"] or 0) for r in rows)
        ecr = total_pnl / total_expected if total_expected > 0 else 0.0
        current = _get_current(env_vals, param)

        if ecr < -1.0:
            raw_new = current + 0.02
            reason = f"Catastrophic ECR={ecr:.2f}"
        elif ecr < 0.3:
            raw_new = current + 0.01
            reason = f"Poor ECR={ecr:.2f}, raising threshold"
        elif ecr > 0.7:
            raw_new = current - 0.005
            reason = f"Good ECR={ecr:.2f}, lowering threshold"
        else:
            print(f"[tuner/strat] {strategy}: ECR={ecr:.2f}, no change needed")
            continue

        new_val = _clamp_and_cap(current, raw_new, param)
        if abs(new_val - current) > 0.0001:
            adjustments[param] = new_val
            _log(conn, today, param, current, new_val,
                 f"{reason} ({len(rows)} trades, PnL=${total_pnl:.2f})",
                 len(rows))
            print(f"[tuner/strat] {strategy}: {current:.4f} -> {new_val:.4f} (ECR={ecr:.2f})")

    return adjustments


# ---------------------------------------------------------------------------
# Loop 3: Spread-Based Slippage Calibration
# ---------------------------------------------------------------------------

def _tune_slippage(conn, env_vals, today, cutoff):
    rows = conn.execute(
        "SELECT yes_bid, yes_ask FROM kalshi_extra "
        "WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL "
        "AND yes_bid > 0 AND yes_ask > 0 AND fetched_at > ?",
        (cutoff,)).fetchall()

    if len(rows) < MIN_SPREAD_SAMPLES:
        print(f"[tuner/slip] {len(rows)} spread samples < {MIN_SPREAD_SAMPLES} min. Skipping.")
        return {}

    spreads_bps = []
    for r in rows:
        mid = (r["yes_ask"] + r["yes_bid"]) / 2
        if mid > 0:
            spread = (r["yes_ask"] - r["yes_bid"]) / mid * 10000
            if spread >= 0:
                spreads_bps.append(spread)

    if len(spreads_bps) < MIN_SPREAD_SAMPLES:
        print(f"[tuner/slip] {len(spreads_bps)} valid spreads < {MIN_SPREAD_SAMPLES}. Skipping.")
        return {}

    sorted_spreads = sorted(spreads_bps)
    median_spread = sorted_spreads[len(sorted_spreads) // 2]

    param = "RIVALCLAW_SLIPPAGE_BPS"
    current = _get_current(env_vals, param)
    raw_new = current * 0.7 + median_spread * 0.3
    new_val = _clamp_and_cap(current, raw_new, param)

    if abs(new_val - current) > 0.5:
        _log(conn, today, param, current, new_val,
             f"Median spread={median_spread:.1f}bps from {len(spreads_bps)} observations",
             len(spreads_bps))
        print(f"[tuner/slip] SLIPPAGE_BPS: {current:.0f} -> {new_val:.0f} (median spread={median_spread:.1f}bps)")
        return {param: new_val}
    else:
        print(f"[tuner/slip] SLIPPAGE_BPS: {current:.0f} unchanged (median spread={median_spread:.1f}bps)")
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_tuning():
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"[tuner] Starting tuning cycle — {today} (lookback: {LOOKBACK_DAYS}d)")

    conn = _get_conn()
    env_vals = _read_env()

    try:
        if _check_rollback(conn, env_vals, today):
            return

        if ENV_PATH.exists():
            prev = ENV_PATH.parent / ".env.prev"
            prev.write_text(ENV_PATH.read_text())

        all_adjustments = {}

        vol_adj = _tune_volatility(conn, env_vals, today, cutoff)
        all_adjustments.update(vol_adj)

        strat_adj = _tune_strategies(conn, env_vals, today, cutoff)
        all_adjustments.update(strat_adj)

        slip_adj = _tune_slippage(conn, env_vals, today, cutoff)
        all_adjustments.update(slip_adj)

        if all_adjustments:
            for k, v in all_adjustments.items():
                env_vals[k] = str(round(v, 6))
            _write_env(env_vals)
            print(f"[tuner] Wrote {len(all_adjustments)} adjustments to .env")
        else:
            _log(conn, today, "none", 0, 0, "no adjustment needed", 0)
            print("[tuner] No adjustments needed")

        conn.commit()
    finally:
        conn.close()

    print(f"[tuner] Tuning cycle complete — {today}")
