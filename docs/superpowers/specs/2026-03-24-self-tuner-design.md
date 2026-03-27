# RivalClaw Self-Tuner Design

## Goal

Automatic, purely mechanical parameter tuning that adjusts RivalClaw's trading thresholds based on realized performance data. No LLM involved — math only.

## Why Mechanical

RivalClaw exists to test whether a simple, fast, mechanical system can compete with Clawmpson's broader, LLM-assisted approach. The self-improvement loop must stay mechanical to preserve experiment integrity. All parameters being tuned are numerical with computable ground truth.

## Architecture

Single new file: `self_tuner.py` (~250 lines). Reads from existing DB tables, computes adjustments, writes updated values to `.env`. Runs daily via cron at 23:30 UTC.

## Prerequisites (Code Changes)

Before the tuner can work, two changes to existing code are needed:

1. **`trading_brain.py`**: Change `CRYPTO_VOL` dict to read from env vars with hardcoded fallbacks:
   ```python
   CRYPTO_VOL = {
       "dogecoin": float(os.environ.get("RIVALCLAW_VOL_DOGECOIN", "0.90")),
       ...
   }
   ```

2. **`paper_wallet.py`**: Add `expected_edge REAL` column to `paper_trades` table (via migration). Populate at trade time from `decision.metadata["edge"]`.

3. **`simulator.py`**: Add `spot_prices` table to migration. Log spot prices each cycle from `spot_feed.get_spot_prices()`.

## Data Sources

- **`spot_prices`** table (new) — CoinGecko spot prices logged each cycle, used for realized vol computation
- **`paper_trades`** table — closed trades with `expected_edge` column (new), strategy, pnl, venue
- **`kalshi_extra`** table — bid/ask spreads for slippage calibration
- **`daily_pnl`** table — for rollback checks

## Tuning Loops

### Loop 1: Realized Volatility

**Problem:** `CRYPTO_VOL` dict uses hardcoded annual volatility estimates (BTC=0.60, DOGE=0.90, etc.). These are guesses that drift from reality.

**Fix:** Compute rolling realized vol from actual spot price data.

- Source: `spot_prices` table — CoinGecko spot prices logged every 2-minute cycle by the simulator. This provides a continuous time series of actual prices per crypto (~720 observations/day).
- NOT from `kalshi_extra.floor_strike` — those are per-contract strike prices, not a spot time series. The 15-min contract floor_strikes only give ~96 data points/day for DOGE and are bracket boundaries (not spot) for BTC/ETH hourly contracts.
- Method: log returns between consecutive spot snapshots per underlying, annualized via `std(log_returns) * sqrt(periods_per_year)` where `periods_per_year = 365.25 * 24 * 30` (one observation per 2-minute cycle)
- Lookback: 7 days of data (configurable via `RIVALCLAW_TUNER_LOOKBACK_DAYS`)
- Minimum sample: 500 spot snapshots per underlying (~1 day of 2-min cycles)
- Clamp: [0.30, 1.50] — never unreasonably low or high
- Output: writes `RIVALCLAW_VOL_BITCOIN=0.58`, `RIVALCLAW_VOL_DOGECOIN=0.82`, etc. to `.env`
- Pickup: `trading_brain.py` reads `CRYPTO_VOL` from env vars at import time (see Prerequisites). Each cron cycle is a fresh process, so updated `.env` takes effect automatically.

### Loop 2: Strategy Scoring

**Problem:** All strategies use static edge thresholds regardless of how well they actually perform.

**Fix:** Adjust per-strategy thresholds based on edge capture rate.

For each strategy (`arbitrage`, `fair_value_directional`, `near_expiry_momentum`):
- Compute from closed trades in lookback window:
  - `win_rate` = wins / total
  - `avg_pnl` = mean(pnl)
  - `total_pnl` = sum(pnl)
  - `trade_count` = count
  - `edge_capture_rate` = sum(pnl) / sum(expected_edge) — uses the structured `expected_edge` column (see Prerequisites), NOT parsed from reasoning text
- Minimum sample: 10 closed trades before adjusting
- Adjustment logic:
  - If `edge_capture_rate < -1.0` (catastrophic — losing far more than expected): raise threshold by 2 percentage points, log as "catastrophic underperformance"
  - If `edge_capture_rate < 0.3` (poor — losing >70% of expected edge): raise threshold by 1 percentage point
  - If `edge_capture_rate > 0.7` (capturing well): lower threshold by 0.5 percentage points
  - Otherwise: no change
- The 20% per-cycle cap is applied AFTER the raw adjustment. Example: if current value is 0.005 and raw adjustment is +0.01, the 20% cap limits the move to 0.005 * 0.20 = 0.001, resulting in 0.006.
- For `near_expiry_momentum`: raising `MIN_MOMENTUM_PRICE` means requiring stronger directional signal (pickier). Lowering it accepts weaker signals (more aggressive). This is the correct direction — poor edge capture means we're entering on weak signals, so we raise the bar.
- Parameter clamps:
  - `ARB_MIN_EDGE`: [0.003, 0.05]
  - `RIVALCLAW_MIN_FV_EDGE`: [0.02, 0.15]
  - `RIVALCLAW_MIN_MOMENTUM_PRICE`: [0.70, 0.90]
- Output: writes adjusted values to `.env`

### Loop 3: Spread-Based Slippage Calibration

**Problem:** Execution simulation uses static slippage assumptions (SLIPPAGE_BPS=50) that may not match actual market microstructure.

**Fix:** Calibrate slippage against observed bid-ask spreads, not against the sim's own output (which would be circular in paper trading).

- Source: `kalshi_extra` table — `yes_bid`, `yes_ask` fields provide real bid-ask spread data from Kalshi
- For each market with bid AND ask data in the lookback window:
  - `spread_bps = (yes_ask - yes_bid) / ((yes_ask + yes_bid) / 2) * 10000`
- Compute median spread across all Kalshi markets
- If median spread is consistently lower than `SLIPPAGE_BPS`: our sim is too conservative — lower SLIPPAGE_BPS toward observed spread (encourages more trades to pass)
- If median spread is consistently higher: our sim underestimates friction — raise SLIPPAGE_BPS
- Adjustment: new SLIPPAGE_BPS = weighted average of current value (70%) and observed median spread (30%). This prevents sudden jumps.
- `FILL_RATE_MIN` is NOT tuned by this loop (no observable fill rate data in paper trading). It stays at the configured value until live trading.
- Minimum sample: 50 market snapshots with bid/ask data
- Clamp: SLIPPAGE_BPS [10, 100]
- Output: writes adjusted value to `.env`

## Cold Start Reality

As of 2026-03-24, the system has:
- 3 open trades (all `near_expiry_momentum` on Kalshi)
- 0 closed trades with wins
- 0 `arbitrage` or `fair_value_directional` trades
- ~1 day of spot price data

**Expected activation timeline:**
- **Loop 1 (Volatility):** Active after ~1 day of spot data collection (500+ snapshots). This will be the first loop to activate.
- **Loop 2 (Strategy Scoring):** Active per-strategy only after 10+ closed trades for that strategy. `near_expiry_momentum` may reach this within 2-3 days. `fair_value_directional` and `arbitrage` may take weeks.
- **Loop 3 (Spread Calibration):** Active after 50+ Kalshi market snapshots with bid/ask data. Should activate within 1-2 days.

The tuner logs "insufficient data" for skipped loops. This is expected and correct — tuning with insufficient data is worse than not tuning.

## Safety

### Bounded Adjustments
No parameter moves more than 20% from its current value in a single tuning cycle. The 20% cap is applied AFTER the raw adjustment is computed, then the result is clamped to the parameter's valid range.

### Minimum Samples
- Volatility: 500 spot snapshots per underlying
- Strategy scoring: 10 closed trades per strategy
- Spread calibration: 50 market snapshots with bid/ask data

If minimum samples aren't met, that loop is skipped (logged as "insufficient data").

### Rollback
- Before writing changes, copy `.env` to `.env.tmp.new`, then atomically rename to `.env` (prevents partial reads from concurrent cron cycles)
- Also copy pre-tuning `.env` to `.env.prev` for rollback
- At the start of each tuning cycle, check `daily_pnl` for the most recent completed day: if ROI < -5% AND `tuning_log` shows changes were made within the last 24 hours → auto-revert to `.env.prev` and log "rollback triggered"
- After a rollback, set a 3-day cooldown: skip all tuning for 3 days to prevent oscillation. Store cooldown expiry in `tuning_log` with `parameter='cooldown'`.

### No Strategy Killing
The tuner adjusts thresholds only. It never disables a strategy entirely. Strategy removal is an operator decision.

### Atomic .env Writes
All `.env` writes use write-to-temp + atomic rename pattern: write to `.env.tmp`, then `os.rename('.env.tmp', '.env')`. Prevents concurrent trading cycles from reading a partially-written file.

## Logging

New DB table: `tuning_log`

```sql
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

New DB table: `spot_prices`

```sql
CREATE TABLE IF NOT EXISTS spot_prices (
    id INTEGER PRIMARY KEY,
    crypto_id TEXT NOT NULL,
    price_usd REAL NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_prices_crypto_time ON spot_prices(crypto_id, fetched_at);
```

Every tuning adjustment logged with parameter name, old/new values, reason string, and sample size.

If nothing changed: single row with `parameter='none'`, `reason='no adjustment needed'`.

## Integration

### CLI
`run.py --tune` runs one tuning cycle.

### Cron
```
30 23 * * * cd /Users/nayslayer/rivalclaw && /Users/nayslayer/rivalclaw/venv/bin/python run.py --tune >> /Users/nayslayer/rivalclaw/rivalclaw.log 2>&1
```
Runs at 23:30 UTC, 15 minutes before `daily-update.sh` (23:45), so the daily report captures tuning changes.

### DB Migration
Add `tuning_log` and `spot_prices` tables to `simulator.py` MIGRATION_SQL. Add `expected_edge` column to `paper_trades` via ALTER TABLE.

### Spot Price Logging
In `simulator.py run_loop()`, after fetching spot prices, log them to `spot_prices` table:
```python
for crypto_id, price in spot_prices.items():
    conn.execute("INSERT INTO spot_prices (crypto_id, price_usd, fetched_at) VALUES (?,?,?)",
                 (crypto_id, price, now))
```

### Runtime Pickup
Trading brain reads all thresholds from env vars at module import. Each cron cycle spawns a fresh Python process that loads `.env` via `run.py`. Updated values take effect on the next 2-minute trading cycle after tuning runs.

### Daily Report
`daily-update.sh` adds a "TUNER" section. SQL query:
```sql
SELECT parameter, old_value, new_value, reason, sample_size
FROM tuning_log
WHERE date = '$TODAY'
ORDER BY tuned_at ASC
```
Format as a markdown table in the daily report.

### Gonzoclaw
No dashboard changes needed. Tuning improvements manifest as better/worse trading performance, already visible in existing metrics.

## Env Var Summary

New env vars written by tuner (all optional, brain falls back to hardcoded defaults):

| Variable | Default | Clamp | Source |
|----------|---------|-------|--------|
| RIVALCLAW_VOL_BITCOIN | 0.60 | [0.30, 1.50] | Loop 1 |
| RIVALCLAW_VOL_ETHEREUM | 0.65 | [0.30, 1.50] | Loop 1 |
| RIVALCLAW_VOL_DOGECOIN | 0.90 | [0.30, 1.50] | Loop 1 |
| RIVALCLAW_VOL_CARDANO | 0.80 | [0.30, 1.50] | Loop 1 |
| RIVALCLAW_VOL_BINANCECOIN | 0.65 | [0.30, 1.50] | Loop 1 |
| RIVALCLAW_VOL_BITCOIN_CASH | 0.75 | [0.30, 1.50] | Loop 1 |
| ARB_MIN_EDGE | 0.005 | [0.003, 0.05] | Loop 2 |
| RIVALCLAW_MIN_FV_EDGE | 0.04 | [0.02, 0.15] | Loop 2 |
| RIVALCLAW_MIN_MOMENTUM_PRICE | 0.78 | [0.70, 0.90] | Loop 2 |
| RIVALCLAW_SLIPPAGE_BPS | 50 | [10, 100] | Loop 3 |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| RIVALCLAW_TUNER_LOOKBACK_DAYS | 7 | Days of history to analyze |
| RIVALCLAW_TUNER_MAX_ADJUST_PCT | 0.20 | Max per-cycle parameter change (20%) |
| RIVALCLAW_TUNER_MIN_TRADES | 10 | Min closed trades before adjusting strategy |
| RIVALCLAW_TUNER_MIN_SNAPSHOTS | 500 | Min spot snapshots for vol computation |
| RIVALCLAW_TUNER_MIN_SPREAD_SAMPLES | 50 | Min market snapshots for spread calibration |
| RIVALCLAW_TUNER_ROLLBACK_THRESHOLD | -0.05 | Daily ROI that triggers rollback (-5%) |
| RIVALCLAW_TUNER_COOLDOWN_DAYS | 3 | Days to skip tuning after rollback |
