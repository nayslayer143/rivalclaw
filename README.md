# RivalClaw

Mechanical arbitrage trading system that competes head-to-head against the
OpenClaw (Clawmpson) trading stack.  Trades binary contracts on **Kalshi**
across crypto, weather, commodities, and equity-index markets.

RivalClaw exists to answer one question:

> Can a narrow, fast, mechanical system outperform a broader, integrated
> architecture on real, execution-adjusted metrics?

## Design principles

- Mechanical over narrative.
- Execution-first over theory-first.
- Fast over exhaustive.
- Skeptical over optimistic.
- Minimal over feature-heavy.

---

## Architecture overview

```
                               +-------------------+
                               |   Kalshi REST API  |
                               | (RSA auth, prod)   |
                               +--------+----------+
                                        ^
                                        | submit / poll / settle
                                        v
+------------------+    +---------------+---------------+
|  Market Feeds    |    |       Execution Router        |
|                  |    |  10-pt pre-flight, kill switch |
|  kalshi_feed     +--->|  anti-stack, rate limits       |
|  spot_feed       |    +------+--------+---------------+
|  index_feed      |           ^        |
|  weather_feed    |           |        v
+--------+---------+    +------+--------+---------------+
         |              |     Kalshi Executor            |
         v              |  order build, submit, poll,    |
+--------+---------+    |  reconciliation, rate limiter  |
| Market Classifier|    +-------------------------------+
| speed + clarity  |           ^
+--------+---------+           |
         |              +------+--------+
         v              | Protocol      |
+--------+---------+    | Adapter       |
| Trading Brain    |    | (openclaw-    |
| 8 strategies     +--->|  protocol)    |
| Kelly sizing     |    +---------------+
| direction filter |           ^
+--------+---------+           |
         |              +------+--------+
         v              | Paper Wallet  |
+--------+---------+    | slippage sim, |
| Risk Engine      |    | stops, MTM    |
| regime detection +--->+---------------+
| strategy tourney |
| exposure caps    |
+------------------+
         |
         v
+------------------+    +------------------+
| Graduation Gates |    | Self-Tuner       |
| 14-day window    |    | vol calibration  |
| Sharpe > 1.0     |    | spread slippage  |
+------------------+    +------------------+
                               |
                        +------v-----------+
                        | Resolution Loop  |
                        | Kalshi API settle|
                        | Polymarket gamma |
                        +------------------+
```

Data flows top-to-bottom each cycle.  The resolution loop runs at the end of
every cycle to close settled positions and credit the wallet.

---

## File map

| File | Purpose |
|------|---------|
| `run.py` | CLI entry point: `--run`, `--tune`, `--report`, `--ping`, `--migrate` |
| `simulator.py` | Cycle orchestrator: fetch, classify, analyze, trade, resolve, snapshot |
| `trading_brain.py` | 8-strategy quant engine with Kelly sizing and direction filters |
| `risk_engine.py` | Regime detection, strategy tournament, portfolio exposure caps |
| `execution_router.py` | 10-point pre-flight check, shadow/live routing, maker mode |
| `kalshi_executor.py` | Kalshi REST API: order build, submit, poll, reconciliation |
| `kalshi_feed.py` | RSA-authenticated Kalshi market data ingestion (50+ series) |
| `polymarket_feed.py` | Polymarket gamma API market data ingestion |
| `spot_feed.py` | CoinGecko spot prices for 8 cryptos (fair value input) |
| `index_feed.py` | Yahoo Finance spot prices for S&P 500 and Nasdaq-100 |
| `weather_feed.py` | NWS forecast API for 21 cities (temperature fair value) |
| `sentiment_feed.py` | Crypto sentiment feed via Fear and Greed index (suspended) |
| `market_classifier.py` | Resolution speed and clarity scoring, priority filtering |
| `paper_wallet.py` | Execution simulation: slippage, partial fills, stops, MTM |
| `protocol_adapter.py` | Bridge to openclaw-protocol engine (event-sourced wallet) |
| `graduation.py` | Graduation gates: win rate, Sharpe, drawdown, ROI thresholds |
| `self_tuner.py` | Mechanical parameter tuning from realized vol and strategy scores |
| `notify.py` | Telegram alerts via @rivalclaw_bot |
| `hourly_report.py` | Hourly performance report generator |
| `auto_changelog.py` | Appends hourly summaries to CHANGELOG.md |
| `status_ping.py` | 15-minute lightweight Telegram status ping |
| `paper_monitor.py` | 5-minute Telegram monitor: live settlements and paper stats |
| `balance_watchdog.py` | Suspends live trading if Kalshi balance hits floor ($25) |
| `trade_monitor.py` | Detects new orders, fills, and resolutions for commentary |
| `rivalclaw_dispatcher.py` | Telegram chat interface: commands, Haiku chat, Sonnet escalation |
| `catalog_reader.py` | Loads openclaw-strategies catalog JSON for reference |
| `event_logger.py` | Structured JSONL event logger for Strategy Lab |
| `daily-update.sh` | Daily report generator, git commit, and push |
| `notify-telegram.sh` | Shell helper for Telegram notifications |

---

## Quickstart

```bash
# 1. Clone
git clone git@gitlab.com:<org>/rivalclaw.git
cd rivalclaw

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Dependencies
pip install requests cryptography openclaw-protocol

# 4. Configure
cp .env.example .env
# Required keys:
#   KALSHI_API_KEY_ID       — from Kalshi dashboard
#   KALSHI_PRIVATE_KEY_PATH — RSA private key PEM
#   KALSHI_API_ENV          — "demo" or "prod"
#   TELEGRAM_CHAT_ID        — for notifications (optional)

# 5. Migrate database
python run.py --migrate

# 6. Single cycle (paper mode, default)
python run.py --run

# 7. Hourly tune + report
python run.py --tune
```

---

## Configuration reference

All configuration is via environment variables, loaded from `.env` by `run.py`.

### Execution mode

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_EXECUTION_MODE` | `paper` | `paper`, `shadow`, or `live` |
| `RIVALCLAW_LIVE_KILL_SWITCH` | `0` | `1` to halt all live order submission |
| `RIVALCLAW_LIVE_BALANCE_FLOOR` | `25.00` | Auto-activate kill switch below this USD balance |

### Live order limits

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_LIVE_MAX_ORDER_USD` | `2` | Max single order size in USD |
| `RIVALCLAW_LIVE_MAX_EXPOSURE_USD` | `10` | Max total open exposure in USD |
| `RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER` | `5` | Max contracts per order |
| `RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE` | `2` | Max orders per cycle |
| `RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR` | `10` | Max orders per hour |
| `RIVALCLAW_LIVE_SERIES` | `KXDOGE15M,...` | Allowed Kalshi series prefixes (comma-sep) |
| `RIVALCLAW_LIVE_MAX_PRICE_DEVIATION` | `0.10` | Max fractional deviation from market price |

### Trade filters

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_BLOCK_YES_TRADES` | `1` | Block all YES-direction trades |
| `RIVALCLAW_MAX_ENTRY_PRICE` | `0.60` | Max entry price (NO cost) |
| `RIVALCLAW_MIN_ENTRY_PRICE` | `0.08` | Min entry price |
| `RIVALCLAW_MIN_CONFIDENCE` | `0.60` | Confidence floor for all trades |
| `RIVALCLAW_DEAD_ZONE_MIN` | `0.30` | Dead zone lower bound |
| `RIVALCLAW_DEAD_ZONE_MAX` | `0.45` | Dead zone upper bound |
| `RIVALCLAW_DEAD_ZONE_CONFIDENCE` | `0.75` | Min confidence inside dead zone |
| `RIVALCLAW_REVERSE_SERIES` | `KXDOGE15M` | Series to reverse signal direction |

### Risk limits

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_MAX_POSITION_PCT` | `0.10` | Max single position as pct of balance |
| `RIVALCLAW_MAX_CRYPTO_PCT` | `0.40` | Max crypto exposure as pct of balance |
| `RIVALCLAW_MAX_ASSET_PCT` | `0.25` | Max single-asset exposure pct |
| `RIVALCLAW_STOP_LOSS_PCT` | `0.20` | Stop-loss trigger (unrealized loss) |
| `RIVALCLAW_TAKE_PROFIT_PCT` | `0.50` | Take-profit trigger (unrealized gain) |

### Kalshi API

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | -- | API key ID from Kalshi dashboard |
| `KALSHI_PRIVATE_KEY_PATH` | -- | Path to RSA private key PEM |
| `KALSHI_API_ENV` | `prod` | `demo` or `prod` |

### Maker mode

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_MAKER_ENABLED` | `0` | Enable maker (limit order) strategy |
| `RIVALCLAW_MAKER_OFFSET_PCT` | `0.10` | Price offset from brain price |
| `RIVALCLAW_MAKER_PATIENCE_SEC` | `120` | Wait time before cancelling resting maker |

### Self-tuner

| Variable | Default | Description |
|----------|---------|-------------|
| `RIVALCLAW_TUNER_LOOKBACK_DAYS` | `7` | Rolling window for tuner analysis |
| `RIVALCLAW_TUNER_MAX_ADJUST_PCT` | `0.20` | Max per-tune parameter adjustment |
| `RIVALCLAW_TUNER_MIN_TRADES` | `10` | Min trades before tuning a strategy |

---

## Cron setup

Five cron entries drive the system (all times UTC):

```cron
# 1. Main trading cycle -- every minute
* * * * *  cd ~/rivalclaw && venv/bin/python run.py --run >> rivalclaw.log 2>&1

# 2. Hourly self-tune + report + changelog
0 * * * *  ~/rivalclaw/venv/bin/python run.py --tune >> rivalclaw.log 2>&1

# 3. Daily report + git push (04:00 UTC)
0 4 * * *  bash ~/rivalclaw/daily-update.sh >> daily-update.log 2>&1

# 4. Commander sync (:27 past each hour)
27 * * * *  cd ~/openclaw-audit && python3 commander/commander.py --claw rivalclaw >> commander/logs/cron.log 2>&1

# 5. Paper monitor -- 5-minute Telegram updates
*/5 * * * *  ~/rivalclaw/venv/bin/python paper_monitor.py >> paper_monitor.log 2>&1
```

`run.py` uses an exclusive file lock (`.rivalclaw_run.lock`) to prevent
overlapping cycles when cron fires faster than a cycle completes.

---

## Database tables

All state lives in `rivalclaw.db` (SQLite, WAL mode).  14 tables:

| Table | Purpose |
|-------|---------|
| `market_data` | Polymarket and Kalshi price snapshots per fetch |
| `paper_trades` | Open and closed positions with latency timestamps |
| `daily_pnl` | Daily balance, ROI, win rate snapshots |
| `cycle_metrics` | Per-cycle timing: fetch, analyze, wallet, total ms |
| `context` | Key-value config store (starting_balance, trading_status) |
| `kalshi_extra` | Kalshi bid/ask, open interest, strike bounds, rules text |
| `spot_prices` | CoinGecko and Yahoo Finance spot price history |
| `tuning_log` | Self-tuner parameter change audit trail |
| `live_orders` | Kalshi order submissions with intent, status, fill data |
| `live_reconciliation` | Paper vs live fill price comparison (slippage delta) |
| `account_snapshots` | Kalshi account balance and portfolio value snapshots |
| `market_scores` | Strategy tournament scoring data |
| `paper_trades_pre_audit_2026_03_26` | Backup before audit cleanup |
| `paper_trades_pre_protocol_20260327` | Backup before protocol migration |

---

## Current trading doctrine

Derived from analysis of 1,400+ trades (as of 2026-03-31):

- **Direction**: NO-only.  YES trades have 15% win rate over 88 trades (-$44 net).
  All YES trades are blocked via `RIVALCLAW_BLOCK_YES_TRADES=1`.
- **Entry price**: $0.08 -- $0.60.  The NO $0.10--$0.30 range is the sweet spot
  (60.8% WR, $225 of $226 live PnL).
- **Dead zone**: $0.30 -- $0.45 entry prices have 41--47% WR and poor average PnL.
  Trades in this range require confidence >= 0.75.
- **Confidence floor**: 0.60.  Losing trades average 0.52 confidence; winners
  average 0.79.
- **Markets**: Crypto 15-min (DOGE, ADA, BNB, BCH, BTC, ETH), weather high/low
  temp (21 cities), hourly crypto range, daily commodities, equity indices.
- **Signal reversal**: KXDOGE15M model is anti-correlated (15% WR on NO);
  direction is flipped to capture the inverse signal.

---

## Deep dives

See `docs/` for detailed documentation:

- [docs/architecture.md](docs/architecture.md) -- System architecture, module
  dependency graph, and the 8-step cycle walkthrough
