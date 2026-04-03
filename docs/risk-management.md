# Risk Management

## Trading Doctrine

```
apparent edge != executable edge != realized edge
```

ArbClaw only trades when executable edge is positive. A trade is valid ONLY if
all seven conditions hold: spread exists after fees, spread persists, depth
supports size, fill probability meets threshold, resolution is compatible,
data is fresh, and execution assumptions are realistic.

## Pre-Flight 10-Point Safety Checklist

**File:** `execution_router.py` -- `preflight_check()`

Every live/shadow order must pass all 10 checks before submission:

| # | Check | Threshold | Behavior on Fail |
|---|-------|-----------|------------------|
| 1 | Mode check | must be `live` or `shadow` | reject |
| 2 | Kill switch | `RIVALCLAW_LIVE_KILL_SWITCH != 1` | reject |
| 3 | Balance check | `account_balance_cents >= order_cost_cents` | reject |
| 4 | Exposure check | `current_exposure + amount <= max_exposure_usd` | reject |
| 5 | Order size | `amount_usd <= max_order_usd` | clip to max (not reject) |
| 6 | Contract count | `shares <= max_contracts` | clip to max (not reject) |
| 7 | Rate check | per-cycle and per-hour limits | reject |
| 8 | Series check | ticker prefix in `RIVALCLAW_LIVE_SERIES` | reject |
| 8a | Anti-stacking | no existing order for this ticker | reject |
| 8b | 15-min YES block | `RIVALCLAW_BLOCK_15M_YES=1` blocks YES on 15M contracts | reject |
| 8c | Anti-self-hedge | `RIVALCLAW_BLOCK_SELF_HEDGE=1` blocks opposite-side bets | reject |
| 9 | Price sanity | `deviation <= max_price_deviation` from last market price | reject |
| 10 | Staleness | decision age < 300 seconds | reject |

After all checks pass, shares are floored to whole contracts. Orders with < 1
contract are rejected as `order_too_small`.

## Kill Switch and Balance Floor

**Kill switch** (`RIVALCLAW_LIVE_KILL_SWITCH=1`): Immediately rejects all live
orders. Pre-flight check #2 catches this first.

**Balance floor** (`RIVALCLAW_LIVE_BALANCE_FLOOR`, default $25.00): In
`simulator.run_loop()`, if the Kalshi account balance drops to or below this
floor, the system auto-activates the kill switch by writing
`RIVALCLAW_LIVE_KILL_SWITCH=1` to the `.env` file and setting the env var in
the current process. All subsequent orders are rejected.

**Early exit optimization:** If the live balance is below 10 cents (the cheapest
possible order), the entire analysis cycle is skipped to avoid wasting compute
on 700+ rejected orders per day.

## Exposure Limits

**File:** `risk_engine.py` -- `get_portfolio_exposure()`, `check_risk_limits()`

Per-asset exposure caps (percentage of paper balance):

| Asset Bucket | Tickers Matched | Default Max |
|-------------|-----------------|-------------|
| BTC | `BTC`, `KXBTC` | 25% |
| ETH | `ETH`, `KXETH` | 25% |
| DOGE | `DOGE` | 25% |
| BNB | `BNB` | 25% |
| WEATHER | `HIGH`, `LOW`, `TEMP` | 25% |
| INDEX | `INXU`, `NASDAQ` | 25% |
| OTHER | everything else | 25% |
| Total crypto | BTC+ETH+DOGE+BNB | 40% |

Env vars: `RIVALCLAW_MAX_ASSET_PCT` (default 0.25), `RIVALCLAW_MAX_CRYPTO_PCT`
(default 0.40).

Live execution has separate hard limits:

| Limit | Default | Env Var |
|-------|---------|---------|
| Max single order | $2 | `RIVALCLAW_LIVE_MAX_ORDER_USD` |
| Max total exposure | $10 | `RIVALCLAW_LIVE_MAX_EXPOSURE_USD` |
| Max contracts/order | 5 | `RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER` |

## Risk Engine

**File:** `risk_engine.py`

### Regime Detection

`detect_regime()` classifies the market from the last 30 minutes of BTC spot
prices in the `spot_prices` table:

- Computes log returns and their standard deviation (vol) and mean (trend).
- **volatile**: vol > 0.008 (0.8% per interval).
- **trending**: |trend| > 0.5 * vol AND vol > 0.002.
- **calm**: everything else.

### Strategy Tournament

`get_strategy_scores()` scores each strategy on a rolling window of the last
200 closed trades (per strategy):

| ROI Range | Win Rate | Score (Capital Multiplier) |
|-----------|----------|---------------------------|
| < -10% | any | 0.0 (killed) |
| < 0% | any | 0.25 (underweight) |
| > 10% | any | 1.5 (overweight) |
| > 0% | > 40% | 1.0 (normal) |
| > 0% | < 40% | 0.5 (below average) |
| < 5 trades | any | 0.5 (untested) |

Scoring is ROI-driven, not WR-gated. A strategy with 25% WR but 17% ROI is
rated higher than 60% WR with -5% ROI.

### adjust_decision Flow

For each trade decision:

1. `check_risk_limits()` -- reject if asset or crypto exposure cap exceeded.
2. Strategy score lookup -- reject if score = 0.0 (dead strategy).
3. Regime multiplier -- scale by regime (e.g., volatile: 0.6x for most, 1.2x for fair_value).
4. Speed multiplier -- Polymarket: 0.5x, Kalshi: 1.0x.
5. Final sizing: `amount = amount * score * regime_mult * speed_mult`, capped at 10% of balance.
6. Reject if final amount < $0.10.

Blocked decisions are logged to `risk_debug.log`.

## Position Sizing

**File:** `trading_brain.py` -- `_kelly_size()`, `_size_for_strategy()`

Kelly criterion: `f = (p * b - q) / b` where p = confidence, b = odds, q = 1-p.

| Factor | Description | Default |
|--------|-------------|---------|
| `KELLY_FRACTION_PROVEN` | Multiplier for proven strategies | 1.0 |
| `KELLY_FRACTION_NEW` | Multiplier for unproven strategies | 0.25 |
| `NO_DIRECTION_BOOST` | Extra multiplier for NO trades | 1.3 |
| Time-of-day weight | Scales by UTC hour performance | 0.5 to 1.3 |
| `MAX_LOSS_PCT` | Wipeout cap: max loss per trade | 3% of balance |
| `MAX_POSITION_PCT` | Absolute cap per position | 10% of balance |
| `MAX_TRADE_USD` | Hard dollar ceiling | $500 |

Proven strategies: `arbitrage`, `fair_value_directional`, `time_decay`.

## Entry Price Filters

**File:** `trading_brain.py`

| Filter | Default | Env Var |
|--------|---------|---------|
| Min entry price | $0.08 | `RIVALCLAW_MIN_ENTRY_PRICE` |
| Max entry price | $0.60 | `RIVALCLAW_MAX_ENTRY_PRICE` |
| Dead zone min | $0.30 | `RIVALCLAW_DEAD_ZONE_MIN` |
| Dead zone max | $0.45 | `RIVALCLAW_DEAD_ZONE_MAX` |
| Dead zone min confidence | 0.75 | `RIVALCLAW_DEAD_ZONE_CONFIDENCE` |
| Min confidence (global) | 0.60 | `RIVALCLAW_MIN_CONFIDENCE` |

The dead zone ($0.30-$0.45) requires higher confidence (0.75) because trades in
that range have 41-47% WR and poor average PnL.

Direction filter: `RIVALCLAW_BLOCK_YES_TRADES=1` (default) blocks all YES trades.
YES trades showed 15% WR over 88 trades with -$44 net.

## Circuit Breakers

1. **Balance floor** (live): Auto-activates kill switch at $25 Kalshi balance.
2. **Kill switch**: Rejects all live orders instantly.
3. **Stale data rejection**: Orders built on data older than 5 minutes are rejected.
4. **Paper circuit breaker**: If paper balance drops below `RIVALCLAW_RELOAD_THRESHOLD`
   ($100 default), trading status is set to `halted` in the context table and
   the wallet rejects all new trades.
5. **Pending order cleanup**: Orders stuck in `pending` > 10 minutes are auto-cancelled
   to free exposure budget.

## Graduation Gates

**File:** `graduation.py` -- `check_graduation()`

All five criteria must pass simultaneously:

| Gate | Threshold |
|------|-----------|
| Minimum history | >= 7 days of daily_pnl snapshots |
| 7-day ROI | > 0% |
| Win rate | > 55% |
| Sharpe ratio | > 1.0 |
| Max drawdown | < 25% |

ArbClaw does not self-promote to live trading. Graduation status is informational
only. The operator must manually set `RIVALCLAW_EXECUTION_MODE=live`.
