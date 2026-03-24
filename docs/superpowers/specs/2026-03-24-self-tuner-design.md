# RivalClaw Self-Tuner Design

## Goal

Automatic, purely mechanical parameter tuning that adjusts RivalClaw's trading thresholds based on realized performance data. No LLM involved — math only.

## Why Mechanical

RivalClaw exists to test whether a simple, fast, mechanical system can compete with Clawmpson's broader, LLM-assisted approach. The self-improvement loop must stay mechanical to preserve experiment integrity. All parameters being tuned are numerical with computable ground truth.

## Architecture

Single new file: `self_tuner.py` (~200 lines). Reads from existing DB tables, computes adjustments, writes updated values to `.env`. Runs daily via cron at 23:30 UTC.

No new data collection needed — uses `market_data`, `paper_trades`, `kalshi_extra`, and `cycle_metrics` tables already populated by the trading loop.

## Tuning Loops

### Loop 1: Realized Volatility

**Problem:** `CRYPTO_VOL` dict in `trading_brain.py` uses hardcoded annual volatility estimates (BTC=0.60, DOGE=0.90, etc.). These are guesses that drift from reality.

**Fix:** Compute rolling realized vol from price data.

- Source: `kalshi_extra.floor_strike` values for each series (these are the "starting prices" for 15-min contracts, updated each window — they ARE spot proxies)
- Method: log returns between consecutive snapshots per underlying, annualized via `std(log_returns) * sqrt(periods_per_year)`
- Lookback: 7 days of data (configurable via `RIVALCLAW_TUNER_LOOKBACK_DAYS`)
- Minimum sample: 20 price snapshots per underlying
- Clamp: [0.30, 1.50] — never unreasonably low or high
- Output: writes `RIVALCLAW_VOL_BITCOIN=0.58`, `RIVALCLAW_VOL_DOGECOIN=0.82`, etc. to `.env`
- Pickup: `trading_brain.py` reads `CRYPTO_VOL` from env vars at import time. Each cron cycle is a fresh process, so updated `.env` takes effect automatically.

### Loop 2: Strategy Scoring

**Problem:** All strategies use static edge thresholds regardless of how well they actually perform.

**Fix:** Adjust per-strategy thresholds based on edge capture rate.

For each strategy (`arbitrage`, `fair_value_directional`, `near_expiry_momentum`):
- Compute from closed trades in lookback window:
  - `win_rate` = wins / total
  - `avg_pnl` = mean(pnl)
  - `total_pnl` = sum(pnl)
  - `trade_count` = count
  - `edge_capture_rate` = realized_pnl / expected_edge (expected edge stored in trade reasoning metadata)
- Minimum sample: 10 closed trades before adjusting
- If `edge_capture_rate < 0.3` (losing >70% of expected edge): raise min_edge threshold by 1 percentage point — make strategy pickier
- If `edge_capture_rate > 0.7` (capturing well): lower min_edge by 0.5 percentage points — make strategy slightly more aggressive
- Parameter clamps:
  - `ARB_MIN_EDGE`: [0.003, 0.05]
  - `RIVALCLAW_MIN_FV_EDGE`: [0.02, 0.15]
  - `RIVALCLAW_MIN_MOMENTUM_PRICE`: [0.70, 0.90]
- Output: writes adjusted values to `.env`

### Loop 3: Execution Calibration

**Problem:** Execution simulation uses static assumptions (SLIPPAGE_BPS=50, FILL_RATE_MIN=0.80) that may not match actual market conditions.

**Fix:** Compare sim assumptions vs realized execution metadata.

- Source: closed trades where `reasoning` contains `[sim:` — parse `ideal_price`, `adjusted_price`, `fill_rate` from the sim metadata string
- Compare average realized slippage vs configured SLIPPAGE_BPS
- Compare average realized fill rate vs configured FILL_RATE_MIN
- If realized slippage consistently lower than configured: lower SLIPPAGE_BPS (more trades pass)
- If realized slippage consistently higher: raise SLIPPAGE_BPS
- Same logic for FILL_RATE_MIN
- Minimum sample: 10 trades with sim metadata
- Clamps: SLIPPAGE_BPS [10, 100], FILL_RATE_MIN [0.60, 0.95]
- Output: writes adjusted values to `.env`

## Safety

### Bounded Adjustments
No parameter moves more than 20% from its current value in a single tuning cycle. Prevents overcorrection from small samples.

### Minimum Samples
- Volatility: 20 price snapshots per underlying
- Strategy scoring: 10 closed trades per strategy
- Execution calibration: 10 trades with sim metadata

If minimum samples aren't met, that loop is skipped (logged as "insufficient data").

### Rollback
- Before writing changes, copy `.env` to `.env.prev`
- If next day's daily ROI < -5% AND tuner made changes the previous day: auto-revert to `.env.prev` and log "rollback triggered"
- Rollback check runs at the start of each tuning cycle

### No Strategy Killing
The tuner adjusts thresholds only. It never disables a strategy entirely. Strategy removal is an operator decision.

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

Every adjustment logged with parameter name, old/new values, reason string, and sample size.

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
Add `tuning_log` table to `simulator.py` MIGRATION_SQL.

### Runtime Pickup
Trading brain reads all thresholds from env vars at module import. Each cron cycle spawns a fresh Python process that loads `.env` via `run.py`. Updated values take effect on the next 2-minute trading cycle after tuning runs.

### Daily Report
`daily-update.sh` adds a "TUNER" section showing parameter changes from the most recent tuning cycle.

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
| RIVALCLAW_FILL_RATE_MIN | 0.80 | [0.60, 0.95] | Loop 3 |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| RIVALCLAW_TUNER_LOOKBACK_DAYS | 7 | Days of history to analyze |
| RIVALCLAW_TUNER_MAX_ADJUST_PCT | 0.20 | Max per-cycle parameter change (20%) |
| RIVALCLAW_TUNER_MIN_TRADES | 10 | Min closed trades before adjusting strategy |
| RIVALCLAW_TUNER_MIN_SNAPSHOTS | 20 | Min price snapshots for vol computation |
| RIVALCLAW_TUNER_ROLLBACK_THRESHOLD | -0.05 | Daily ROI that triggers rollback (-5%) |
