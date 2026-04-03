# RivalClaw Architecture

## System overview

RivalClaw is a mechanical trading system that ingests binary contract markets
from Kalshi (and optionally Polymarket), computes fair value from external spot
data, generates trade decisions through 8 quantitative strategies, applies
multi-layer risk controls, and routes orders to the Kalshi API for execution.

Every design decision favors execution realism over theoretical elegance:

- **Mechanical over narrative** -- no LLM in the signal path.
- **Execution-first** -- slippage, fees, partial fills, and latency are
  modeled on every trade, paper or live.
- **Skeptical by default** -- most apparent arbitrage is assumed fake until
  proven otherwise.

---

## Component diagram

```
+============================================================+
|                     MARKET DATA LAYER                       |
|                                                             |
|  kalshi_feed.py      50+ series, RSA auth, paginated fetch  |
|  polymarket_feed.py  gamma API (disabled in production)     |
|  spot_feed.py        CoinGecko: BTC ETH DOGE ADA BNB BCH   |
|                      SOL XRP spot prices                    |
|  index_feed.py       Yahoo Finance: S&P 500, Nasdaq-100     |
|  weather_feed.py     NWS forecast API: 21 US cities         |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|  market_classifier.py    Scores speed + clarity, filters    |
|                          out slow/ambiguous markets          |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|                     SIGNAL LAYER                            |
|                                                             |
|  trading_brain.py   8 strategies:                           |
|    1. arbitrage              cross-outcome spread           |
|    2. fair_value_directional spot-vs-contract mispricing     |
|    3. near_expiry_momentum   directional near close          |
|    4. cross_strike_arb       bracket sum != 1.0 (disabled)  |
|    5. mean_reversion         crowd fade at fair value ~0.50  |
|    6. time_decay             sell overpriced near-expiry     |
|    7. vol_skew               OTM when realized > implied vol |
|    8. calibration            historical price-outcome bias   |
|                                                             |
|  Filters: NO-only, entry $0.08-$0.60, conf >= 0.60,        |
|           dead-zone boost, signal reversal, Kelly sizing     |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|                     RISK LAYER                              |
|                                                             |
|  risk_engine.py                                             |
|    Regime detector    spot vol -> calm/trending/volatile     |
|    Strategy tournament  rolling WR/PnL -> capital alloc     |
|    Exposure caps       per-asset 25%, crypto 40%, total 10% |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|                     EXECUTION LAYER                         |
|                                                             |
|  protocol_adapter.py  openclaw-protocol event-sourced       |
|                       wallet, synthetic order book           |
|  execution_router.py  10-point pre-flight safety check      |
|                       paper / shadow / live routing          |
|                       maker mode with patience window        |
|  kalshi_executor.py   REST API client: build, submit, poll  |
|                       rate limiter, reconciliation           |
|  paper_wallet.py      Legacy: slippage sim, stops, MTM      |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|                     SETTLEMENT LAYER                        |
|                                                             |
|  simulator.py          _resolve_kalshi_trades()             |
|                        _resolve_polymarket_trades()          |
|  graduation.py         daily snapshot, graduation gates     |
|  self_tuner.py         vol recalibration, spread slippage   |
+==========================+==================================+
                           |
                           v
+==========================+==================================+
|                     OBSERVABILITY                           |
|                                                             |
|  event_logger.py       JSONL: snapshots, signals, trades    |
|  hourly_report.py      strategy leaderboard, diagnosis      |
|  paper_monitor.py      live + paper Telegram reports        |
|  notify.py             Telegram bot alerts                  |
|  status_ping.py        15-min quick status pings            |
|  auto_changelog.py     continuous CHANGELOG.md              |
+============================================================+
```

---

## The 8-step cycle

Each invocation of `run.py --run` executes one cycle through `simulator.run_loop()`.
The steps, with code references:

### Step 1 -- Account sync

**Code**: `simulator.py:244-266`

In live or shadow mode, the system calls `kalshi_executor.sync_account()` to
fetch the current Kalshi balance.  If the balance drops to the floor ($25),
the kill switch is automatically activated by writing
`RIVALCLAW_LIVE_KILL_SWITCH=1` to `.env`.  The execution router cycle counter
is reset.

### Step 2 -- Fetch markets

**Code**: `simulator.py:310-317`

`kalshi_feed.fetch_markets()` pages through the Kalshi API for all series in
`FAST_SERIES` (50+ series across crypto, weather, commodities, indices).
Each market record includes yes/no bid-ask, volume, close time, and strike
parameters.  Optional: `polymarket_feed.fetch_markets()` (currently disabled).
All snapshots are written to `market_data` and `kalshi_extra`.

### Step 3 -- Classify and filter

**Code**: `simulator.py:331-332`

`market_classifier.classify_and_filter()` scores each market on resolution
speed (how fast does it settle?) and clarity (how objectively is the outcome
determined?).  Markets below `MIN_PRIORITY` (default 3) are dropped.
Categories: crypto_fast (speed 3), weather (speed 3), crypto_daily (speed 2),
commodities (speed 2), econ (speed 2).

### Step 4 -- Wallet state and spot prices

**Code**: `simulator.py:335-375`

Reads current balance and open positions from the protocol adapter (or legacy
paper wallet).  Fetches spot prices from CoinGecko (8 cryptos) and Yahoo
Finance (S&P 500, Nasdaq-100).  In live mode, the Kalshi account balance
overrides the protocol wallet balance -- the real balance is the truth signal.
Spot prices are logged to `spot_prices` for the self-tuner's realized
volatility computation.

### Step 5 -- Brain analysis

**Code**: `simulator.py:378-404`

`trading_brain.analyze()` runs all 8 strategies against the filtered market
list.  Each strategy produces `TradeDecision` objects with direction, size,
confidence, and reasoning.  Global filters are then applied:

- NO-only direction filter (`BLOCK_YES_TRADES`)
- Entry price bounds ($0.08 -- $0.60)
- Dead zone confidence boost ($0.30 -- $0.45 requires >= 0.75)
- Signal reversal for anti-correlated series
- Fractional Kelly sizing (0.25x for unproven strategies)

The risk engine then runs:
- `detect_regime()` classifies market vol as calm/trending/volatile
- `get_strategy_scores()` runs the rolling tournament
- `adjust_decision()` enforces per-asset and crypto exposure caps

### Step 6 -- Execute trades

**Code**: `simulator.py:440-499`

For each qualified decision not already in the open position set:

1. In live/shadow mode, the decision is clipped to live order caps
   (`RIVALCLAW_LIVE_MAX_ORDER_USD`, `RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER`).
2. `protocol_adapter.execute_trade()` runs the trade through the
   openclaw-protocol engine (synthetic order book, fee model).
3. `execution_router.route_trade()` runs the 10-point pre-flight check and
   either logs (shadow) or submits (live) to Kalshi via `kalshi_executor`.
4. The trade is bridge-written to `paper_trades` for resolution compatibility.

### Step 7 -- Resolve and settle

**Code**: `simulator.py:514-544`

Two resolution paths run every cycle:

- `_resolve_kalshi_trades()` queries the Kalshi API for each open Kalshi
  position.  If the market has a result (yes/no), the trade is closed with
  binary PnL and the protocol wallet is credited.
- `_resolve_polymarket_trades()` queries the Polymarket gamma API for
  resolved markets.  Same binary PnL logic.

Stop-loss (-20%) and take-profit (+50%) checks run on all open positions
using current market prices.

### Step 8 -- Snapshot and metrics

**Code**: `simulator.py:539-551`

`graduation.maybe_snapshot()` writes a daily row to `daily_pnl` if one does
not exist for today.  Cycle timing metrics (fetch, analyze, wallet, total ms)
are logged to `cycle_metrics`.  The structured event logger flushes the run.

---

## Module dependency graph

```
run.py
  |
  +---> simulator.py
          |
          +---> kalshi_feed.py ---------> Kalshi REST API (RSA auth)
          +---> polymarket_feed.py -----> Polymarket gamma API
          +---> spot_feed.py -----------> CoinGecko API
          +---> index_feed.py ----------> Yahoo Finance API
          +---> weather_feed.py --------> NWS API
          +---> market_classifier.py
          +---> trading_brain.py
          |       +---> event_logger.py
          +---> risk_engine.py
          +---> protocol_adapter.py
          |       +---> execution_router.py
          |       |       +---> kalshi_executor.py
          |       |       |       +---> kalshi_feed.py (auth reuse)
          |       |       +---> notify.py
          |       +---> openclaw_protocol (library)
          +---> paper_wallet.py
          |       +---> event_logger.py
          +---> graduation.py
          +---> event_logger.py
          +---> sentiment_feed.py (suspended)

run.py --tune
  +---> self_tuner.py
  +---> hourly_report.py
  +---> auto_changelog.py
  +---> notify.py

Standalone cron:
  paper_monitor.py -----> kalshi_executor.py, notify (Telegram)
  balance_watchdog.py ---> kalshi_executor.py
  rivalclaw_dispatcher.py -> Telegram polling, DB queries, Anthropic API
```

---

## Data stores

### SQLite database (rivalclaw.db)

Single-file database in WAL mode.  All modules share one DB path, configured
via `RIVALCLAW_DB_PATH`.  14 tables -- see README.md for the full table
reference.

Key relationships:
- `paper_trades.market_id` links to `market_data.market_id` and `kalshi_extra.market_id`
- `live_orders.ticker` corresponds to `paper_trades.market_id` for Kalshi trades
- `live_reconciliation.live_order_id` references `live_orders.id`
- `spot_prices.crypto_id` maps to CoinGecko IDs and Yahoo Finance index IDs
- `tuning_log.parameter` maps to `.env` variable names

### Protocol databases

When `USE_PROTOCOL=True` (default), `protocol_adapter.py` initializes an
`openclaw_protocol.SqliteEventStore` in the same directory.  This creates
additional SQLite files for event-sourced trade state, separate from the
main `rivalclaw.db`.

### Environment file (.env)

The `.env` file is both configuration input and mutable state.  The self-tuner
writes updated parameter values back to `.env`.  The balance floor watchdog
writes `RIVALCLAW_LIVE_KILL_SWITCH=1` when balance drops.

### Log files

| File | Source |
|------|--------|
| `rivalclaw.log` | Main cron output (run, tune) |
| `paper_monitor.log` | 5-minute monitor output |
| `daily-update.log` | Daily report script output |
| `logs/events.jsonl` | Structured event log (JSONL, rotated daily) |
| `logs/dispatcher.log` | Telegram dispatcher output |
| `risk_debug.log` | Risk engine debug trace |
| `daily/*.md` | Daily and hourly performance reports |
| `CHANGELOG.md` | Auto-appended hourly summaries |

---

## External dependencies

### Kalshi API

- **Auth**: RSA key-pair signature (KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP,
  KALSHI-ACCESS-SIGNATURE headers).
- **Read path**: `kalshi_feed.py` fetches markets via
  `GET /markets?series_ticker=...` with pagination (200 per page).
- **Write path**: `kalshi_executor.py` submits orders via
  `POST /portfolio/orders`, polls status via `GET /portfolio/orders/:id`,
  cancels via `DELETE /portfolio/orders/:id`.
- **Account**: `GET /portfolio/balance` for balance sync.
- **Rate limit**: Configurable write rate (default 10/sec), sliding window.
- **Environments**: `demo-api.kalshi.co` (paper) and
  `api.elections.kalshi.com` (production).

### CoinGecko (spot_feed.py)

- **Endpoint**: `GET /api/v3/simple/price`
- **Assets**: bitcoin, ethereum, dogecoin, cardano, binancecoin, bitcoin-cash,
  solana, ripple
- **Rate limit**: Free tier, 30 calls/min.  Cached 30 seconds.
- **Purpose**: Fair value computation for crypto binary contracts.

### Yahoo Finance (index_feed.py)

- **Endpoint**: `GET /v8/finance/chart/{ticker}`
- **Tickers**: `^GSPC` (S&P 500), `^NDX` (Nasdaq-100)
- **Rate limit**: No auth, cached 30 seconds.
- **Purpose**: Fair value for Kalshi index contracts (KXINXU, KXNASDAQ100U).

### NWS Weather API (weather_feed.py)

- **Endpoint**: `GET /gridpoints/{WFO}/{x},{y}/forecast`
- **Coverage**: 21 US cities (DC, SF, NYC, Houston, Boston, Atlanta, Dallas,
  Phoenix, Seattle, LA, Miami, Philadelphia, Chicago, Austin, Denver,
  Las Vegas, San Antonio, Minneapolis, OKC, New Orleans, and NYC via OKX).
- **Rate limit**: No auth required.  Cached per cycle.
- **Purpose**: Forecast high/low temperatures for Kalshi weather contracts.

### openclaw-protocol (library)

- **Package**: `openclaw_protocol`
- **Used by**: `protocol_adapter.py`
- **Provides**: `ProtocolEngine`, `SqliteEventStore`, `FileExecutionLock`,
  `TradeIntent`, synthetic order book builder, rollout manager.
- **Purpose**: Event-sourced trade execution engine with position tracking,
  fee model, and observability.  Shared library with the main OpenClaw stack.
