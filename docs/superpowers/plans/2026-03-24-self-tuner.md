# Self-Tuner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatic daily parameter tuning that adjusts RivalClaw's volatility estimates, strategy thresholds, and slippage assumptions based on realized performance data.

**Architecture:** Single new file `self_tuner.py` with three tuning loops (realized vol, strategy scoring, spread-based slippage). Prerequisites: add `spot_prices` table + spot logging to simulator, add `expected_edge` column to paper_trades, make CRYPTO_VOL env-var-driven. Atomic `.env` writes with rollback.

**Tech Stack:** Python 3.9, SQLite, math (stdlib)

**Spec:** `docs/superpowers/specs/2026-03-24-self-tuner-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `simulator.py` | Modify | Add `spot_prices` + `tuning_log` tables to migration, log spot prices each cycle |
| `trading_brain.py` | Modify | Read CRYPTO_VOL from env vars |
| `paper_wallet.py` | Modify | Add `expected_edge` to paper_trades INSERT |
| `self_tuner.py` | Create | All three tuning loops + .env write + rollback logic |
| `run.py` | Modify | Add `--tune` CLI flag |
| `daily-update.sh` | Modify | Add TUNER section to daily report |

---

### Task 1: DB migration — spot_prices, tuning_log, expected_edge

**Files:**
- Modify: `simulator.py:28-120` (MIGRATION_SQL + migrate())

- [ ] **Step 1: Add spot_prices and tuning_log tables to MIGRATION_SQL**

After the existing `kalshi_extra` CREATE block, add:

```sql
CREATE TABLE IF NOT EXISTS spot_prices (
    id INTEGER PRIMARY KEY,
    crypto_id TEXT NOT NULL,
    price_usd REAL NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_prices_crypto_time ON spot_prices(crypto_id, fetched_at);

CREATE TABLE IF NOT EXISTS tuning_log (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    parameter TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    tuned_at TEXT NOT NULL
);
```

- [ ] **Step 2: Add expected_edge column via ALTER TABLE in migrate()**

In `migrate()`, after the existing venue ALTER TABLEs, add:

```python
try:
    conn.execute("ALTER TABLE paper_trades ADD COLUMN expected_edge REAL")
except sqlite3.OperationalError:
    pass
```

- [ ] **Step 3: Run migration and verify**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 run.py --migrate
```

Verify:
```bash
python3 -c "
import sqlite3; conn = sqlite3.connect('rivalclaw.db')
for t in ('spot_prices', 'tuning_log'):
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})').fetchall()]
    print(f'{t}: {cols}')
edge_col = [r[1] for r in conn.execute('PRAGMA table_info(paper_trades)').fetchall() if r[1] == 'expected_edge']
print(f'expected_edge column: {edge_col}')
"
```

- [ ] **Step 4: Commit**

```bash
git add simulator.py && git commit -m "feat: add spot_prices, tuning_log tables and expected_edge column"
```

---

### Task 2: Log spot prices each cycle in simulator.py

**Files:**
- Modify: `simulator.py` (run_loop, after spot_prices fetch)

- [ ] **Step 1: Add spot price logging after the spot_feed call**

After `spot_prices = spot_feed.get_spot_prices()` in run_loop(), add:

```python
# Log spot prices for realized vol computation
if spot_prices:
    conn = _get_conn()
    try:
        for crypto_id, price in spot_prices.items():
            conn.execute(
                "INSERT INTO spot_prices (crypto_id, price_usd, fetched_at) VALUES (?,?,?)",
                (crypto_id, price, cycle_started_iso))
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Run one cycle and verify spot prices logged**

```bash
source venv/bin/activate && python3 run.py --run
```

Then:
```bash
python3 -c "
import sqlite3; conn = sqlite3.connect('rivalclaw.db')
rows = conn.execute('SELECT crypto_id, price_usd, fetched_at FROM spot_prices ORDER BY id DESC LIMIT 6').fetchall()
for r in rows: print(f'  {r[0]}: \${r[1]:,.4f} @ {r[2][:19]}')
print(f'Total: {conn.execute(\"SELECT COUNT(*) FROM spot_prices\").fetchone()[0]}')
"
```

Expected: 6 crypto prices logged.

- [ ] **Step 3: Commit**

```bash
git add simulator.py && git commit -m "feat: log spot prices each cycle for realized vol computation"
```

---

### Task 3: Make CRYPTO_VOL env-var-driven in trading_brain.py

**Files:**
- Modify: `trading_brain.py:40-47` (CRYPTO_VOL dict)

- [ ] **Step 1: Replace hardcoded CRYPTO_VOL with env-var-backed dict**

```python
CRYPTO_VOL = {
    "dogecoin": float(os.environ.get("RIVALCLAW_VOL_DOGECOIN", "0.90")),
    "cardano": float(os.environ.get("RIVALCLAW_VOL_CARDANO", "0.80")),
    "binancecoin": float(os.environ.get("RIVALCLAW_VOL_BINANCECOIN", "0.65")),
    "bitcoin-cash": float(os.environ.get("RIVALCLAW_VOL_BITCOIN_CASH", "0.75")),
    "bitcoin": float(os.environ.get("RIVALCLAW_VOL_BITCOIN", "0.60")),
    "ethereum": float(os.environ.get("RIVALCLAW_VOL_ETHEREUM", "0.65")),
}
```

- [ ] **Step 2: Verify defaults unchanged**

```bash
source venv/bin/activate && python3 -c "
import trading_brain
for k, v in trading_brain.CRYPTO_VOL.items():
    print(f'  {k}: {v}')
"
```

Expected: same values as before (defaults unchanged).

- [ ] **Step 3: Commit**

```bash
git add trading_brain.py && git commit -m "feat: read CRYPTO_VOL from env vars with hardcoded fallbacks"
```

---

### Task 4: Add expected_edge to paper_trades INSERT in paper_wallet.py

**Files:**
- Modify: `paper_wallet.py` (execute_trade function)

- [ ] **Step 1: Extract expected_edge from decision metadata and add to INSERT**

In `execute_trade()`, before the INSERT, add:
```python
expected_edge = (decision.metadata or {}).get("edge", 0.0) if decision.metadata else 0.0
```

Update the INSERT statement to include `expected_edge`:
- Add `expected_edge` to the column list
- Add `expected_edge` to the VALUES placeholders
- Add `expected_edge` to the parameter tuple

- [ ] **Step 2: Run a cycle and verify expected_edge is populated**

```bash
source venv/bin/activate && python3 run.py --run
```

Then:
```bash
python3 -c "
import sqlite3; conn = sqlite3.connect('rivalclaw.db')
rows = conn.execute('SELECT market_id, strategy, expected_edge FROM paper_trades ORDER BY id DESC LIMIT 5').fetchall()
for r in rows: print(f'  {r[0][:40]} | {r[1]} | edge={r[2]}')
"
```

- [ ] **Step 3: Commit**

```bash
git add paper_wallet.py && git commit -m "feat: persist expected_edge from trade decision metadata"
```

---

### Task 5: Create self_tuner.py

**Files:**
- Create: `self_tuner.py`

- [ ] **Step 1: Create self_tuner.py with all three loops**

```python
#!/usr/bin/env python3
"""
RivalClaw self-tuner — mechanical parameter adjustment based on realized data.
Runs daily via cron. No LLM. Math only.
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

# Map strategy to its threshold env var
STRATEGY_PARAM = {
    "arbitrage": "ARB_MIN_EDGE",
    "fair_value_directional": "RIVALCLAW_MIN_FV_EDGE",
    "near_expiry_momentum": "RIVALCLAW_MIN_MOMENTUM_PRICE",
}

# Map env var name to CoinGecko crypto ID
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
    """Read current .env into a dict."""
    vals = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
    return vals


def _write_env(vals):
    """Atomic write of .env via temp file + rename."""
    tmp = ENV_PATH.parent / ".env.tmp"
    tmp.write_text("\n".join(f"{k}={v}" for k, v in sorted(vals.items())) + "\n")
    os.rename(str(tmp), str(ENV_PATH))


def _get_current(env_vals, param):
    """Get current parameter value from env dict or default."""
    _, _, default = CLAMPS[param]
    return float(env_vals.get(param, str(default)))


def _clamp_and_cap(current, raw_new, param):
    """Apply 20% per-cycle cap then clamp to valid range."""
    lo, hi, _ = CLAMPS[param]
    max_delta = abs(current) * MAX_ADJUST_PCT
    delta = raw_new - current
    if abs(delta) > max_delta:
        delta = max_delta if delta > 0 else -max_delta
    result = current + delta
    return max(lo, min(hi, result))


def _log(conn, date, param, old, new, reason, sample_size):
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO tuning_log (date, parameter, old_value, new_value, reason, sample_size, tuned_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (date, param, old, new, reason, sample_size, now))


# -----------------------------------------------------------------------
# Rollback check
# -----------------------------------------------------------------------

def _check_rollback(conn, env_vals, today):
    """Check if we should rollback and/or are in cooldown."""
    # Check cooldown
    cooldown = conn.execute(
        "SELECT new_value FROM tuning_log WHERE parameter='cooldown' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if cooldown:
        expiry = cooldown["new_value"]
        if today < expiry:
            print(f"[tuner] In cooldown until {expiry}. Skipping.")
            return True

    # Check if tuner made changes yesterday
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    changes = conn.execute(
        "SELECT COUNT(*) as cnt FROM tuning_log WHERE date=? AND parameter != 'none' AND parameter != 'cooldown'",
        (yesterday,)).fetchone()["cnt"]
    if changes == 0:
        return False

    # Check today's ROI
    roi_row = conn.execute(
        "SELECT roi_pct FROM daily_pnl WHERE date=? OR date=?", (today, yesterday)
    ).fetchone()
    if roi_row and roi_row["roi_pct"] is not None and roi_row["roi_pct"] < ROLLBACK_THRESHOLD:
        prev_path = ENV_PATH.parent / ".env.prev"
        if prev_path.exists():
            os.rename(str(prev_path), str(ENV_PATH))
            cooldown_expiry = (datetime.date.fromisoformat(today) +
                               datetime.timedelta(days=COOLDOWN_DAYS)).isoformat()
            _log(conn, today, "rollback", 0, 0,
                 f"ROI {roi_row['roi_pct']:.2%} < {ROLLBACK_THRESHOLD:.0%}, reverted .env", 0)
            _log(conn, today, "cooldown", 0, float(cooldown_expiry.replace("-", "")),
                 f"Cooldown until {cooldown_expiry}", 0)
            conn.commit()
            print(f"[tuner] ROLLBACK triggered (ROI={roi_row['roi_pct']:.2%}). Cooldown until {cooldown_expiry}")
            return True
    return False


# -----------------------------------------------------------------------
# Loop 1: Realized Volatility
# -----------------------------------------------------------------------

def _tune_volatility(conn, env_vals, today, cutoff):
    """Compute realized vol from spot_prices table."""
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

        # Log returns
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i] > 0 and prices[i-1] > 0:
                log_returns.append(math.log(prices[i] / prices[i-1]))

        if len(log_returns) < 10:
            continue

        # Annualize: ~720 observations per day (2-min cycles), 365.25 days
        periods_per_year = 365.25 * 24 * 30  # 30 per hour * 24 hours * 365.25 days
        std_dev = (sum(r**2 for r in log_returns) / len(log_returns) -
                   (sum(log_returns) / len(log_returns))**2) ** 0.5
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


# -----------------------------------------------------------------------
# Loop 2: Strategy Scoring
# -----------------------------------------------------------------------

def _tune_strategies(conn, env_vals, today, cutoff):
    """Adjust strategy thresholds based on edge capture rate."""
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
        total_expected = sum(r["expected_edge"] or 0 for r in rows)
        ecr = total_pnl / total_expected if total_expected != 0 else 0.0
        current = _get_current(env_vals, param)

        if ecr < -1.0:
            raw_new = current + 0.02  # Catastrophic: +2 pp
            reason = f"Catastrophic ECR={ecr:.2f}"
        elif ecr < 0.3:
            raw_new = current + 0.01  # Poor: +1 pp
            reason = f"Poor ECR={ecr:.2f}, raising threshold"
        elif ecr > 0.7:
            raw_new = current - 0.005  # Good: -0.5 pp
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


# -----------------------------------------------------------------------
# Loop 3: Spread-Based Slippage Calibration
# -----------------------------------------------------------------------

def _tune_slippage(conn, env_vals, today, cutoff):
    """Calibrate SLIPPAGE_BPS against observed Kalshi bid-ask spreads."""
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

    # Median spread
    sorted_spreads = sorted(spreads_bps)
    median_spread = sorted_spreads[len(sorted_spreads) // 2]

    param = "RIVALCLAW_SLIPPAGE_BPS"
    current = _get_current(env_vals, param)
    # Weighted average: 70% current, 30% observed
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


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def run_tuning():
    """Run all tuning loops. Called by run.py --tune."""
    today = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"[tuner] Starting tuning cycle — {today} (lookback: {LOOKBACK_DAYS}d)")

    conn = _get_conn()
    env_vals = _read_env()

    try:
        # Rollback check
        if _check_rollback(conn, env_vals, today):
            return

        # Backup current .env
        if ENV_PATH.exists():
            prev = ENV_PATH.parent / ".env.prev"
            prev.write_text(ENV_PATH.read_text())

        all_adjustments = {}

        # Loop 1: Realized Volatility
        vol_adj = _tune_volatility(conn, env_vals, today, cutoff)
        all_adjustments.update(vol_adj)

        # Loop 2: Strategy Scoring
        strat_adj = _tune_strategies(conn, env_vals, today, cutoff)
        all_adjustments.update(strat_adj)

        # Loop 3: Spread-Based Slippage
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
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
source venv/bin/activate && python3 -c "import self_tuner; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add self_tuner.py && git commit -m "feat: create self_tuner.py — mechanical parameter tuning"
```

---

### Task 6: Add --tune to run.py

**Files:**
- Modify: `run.py`

- [ ] **Step 1: Add --tune argument**

Add to the argparse block:
```python
parser.add_argument("--tune", action="store_true", help="Run self-tuning cycle")
```

Add to the if/elif chain:
```python
elif args.tune:
    import self_tuner
    self_tuner.run_tuning()
```

- [ ] **Step 2: Test --tune runs**

```bash
source venv/bin/activate && python3 run.py --tune
```

Expected: tuner runs, reports "insufficient data" for most loops (expected — we just started logging spot prices).

- [ ] **Step 3: Commit**

```bash
git add run.py && git commit -m "feat: add --tune CLI flag for self-tuning"
```

---

### Task 7: Add tuner cron job

- [ ] **Step 1: Add cron entry**

```bash
(crontab -l; echo '30 23 * * * cd /Users/nayslayer/rivalclaw && /Users/nayslayer/rivalclaw/venv/bin/python run.py --tune >> /Users/nayslayer/rivalclaw/rivalclaw.log 2>&1 # rivalclaw_tuner') | crontab -
```

- [ ] **Step 2: Verify**

```bash
crontab -l | grep tuner
```

Expected: `30 23 * * *` entry for rivalclaw_tuner.

---

### Task 8: Add TUNER section to daily-update.sh

**Files:**
- Modify: `daily-update.sh`

- [ ] **Step 1: Add tuner query section**

After the existing performance section, add:

```bash
# Tuner adjustments
TUNER_CHANGES=$(sqlite3 "$DB" "SELECT parameter, old_value, new_value, reason, sample_size FROM tuning_log WHERE date='$TODAY' AND parameter != 'none' AND parameter != 'cooldown' ORDER BY tuned_at ASC")
if [ -n "$TUNER_CHANGES" ]; then
    echo "" >> "$REPORT"
    echo "## Tuner Adjustments" >> "$REPORT"
    echo "" >> "$REPORT"
    echo "| Parameter | Old | New | Reason | Samples |" >> "$REPORT"
    echo "|-----------|-----|-----|--------|---------|" >> "$REPORT"
    echo "$TUNER_CHANGES" | while IFS='|' read -r param old new reason samples; do
        echo "| $param | $old | $new | $reason | $samples |" >> "$REPORT"
    done
else
    echo "" >> "$REPORT"
    echo "## Tuner: No adjustments today" >> "$REPORT"
fi
```

- [ ] **Step 2: Commit**

```bash
git add daily-update.sh && git commit -m "feat: add TUNER section to daily report"
```

---

### Task 9: Integration test + push

- [ ] **Step 1: Run full cycle to populate spot data**

```bash
source venv/bin/activate && python3 run.py --run
```

- [ ] **Step 2: Run tuner**

```bash
python3 run.py --tune
```

Expected: Loop 1 skips (not enough spot data yet), Loop 2 skips (not enough closed trades), Loop 3 may run (if enough Kalshi bid/ask data).

- [ ] **Step 3: Verify tuning_log**

```bash
python3 -c "
import sqlite3; conn = sqlite3.connect('rivalclaw.db')
rows = conn.execute('SELECT * FROM tuning_log ORDER BY id DESC LIMIT 10').fetchall()
for r in rows: print(dict(r))
"
```

- [ ] **Step 4: Final commit and push**

```bash
git add -A && git commit -m "feat: complete self-tuner integration — daily mechanical parameter tuning"
git push origin main
```
