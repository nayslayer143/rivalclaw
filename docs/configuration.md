# Configuration Reference

All configuration is via environment variables, loaded from `~/.rivalclaw/.env` at
startup by `run.py`. Variables use the `RIVALCLAW_` prefix unless inherited from
the broader ecosystem.

## Execution Mode and Safety

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_EXECUTION_MODE` | `paper` | str | `paper` / `shadow` / `live` | execution_router.py |
| `RIVALCLAW_LIVE_KILL_SWITCH` | `0` | bool | `1` to reject all live orders | execution_router.py |
| `RIVALCLAW_LIVE_BALANCE_FLOOR` | `25.00` | float | Auto-activate kill switch at this USD balance | simulator.py |
| `RIVALCLAW_RELOAD_THRESHOLD` | `100` | float | Paper balance floor -- halts paper trading | simulator.py |
| `RIVALCLAW_BLOCK_15M_YES` | `1` | bool | Block YES bets on 15-min contracts | execution_router.py |
| `RIVALCLAW_BLOCK_SELF_HEDGE` | `1` | bool | Block opposite-side bets on same ticker | execution_router.py |

## Order Limits

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_LIVE_MAX_ORDER_USD` | `2` | float | Max single order size (USD) | execution_router.py |
| `RIVALCLAW_LIVE_MAX_EXPOSURE_USD` | `10` | float | Max total open exposure (USD) | execution_router.py |
| `RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER` | `5` | int | Max contracts per order | execution_router.py |
| `RIVALCLAW_MAX_POSITION_PCT` | `0.10` | float | Max position as fraction of balance | trading_brain.py, paper_wallet.py |
| `RIVALCLAW_MAX_TRADE_USD` | `500.0` | float | Hard dollar ceiling per paper trade | paper_wallet.py |
| `RIVALCLAW_MAX_LOSS_PCT` | `0.03` | float | Max single-trade loss as fraction of balance | trading_brain.py |

## Rate Limits

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE` | `2` | int | Non-weather orders per cycle | execution_router.py |
| `RIVALCLAW_LIVE_MAX_WEATHER_PER_CYCLE` | `30` | int | Weather orders per cycle | execution_router.py |
| `RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR` | `10` | int | Total orders per rolling hour | execution_router.py |
| `RIVALCLAW_KALSHI_WRITE_RATE` | `10` | int | Max API writes per second | kalshi_executor.py |

## Strategy Parameters

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `ARB_MIN_EDGE` | `0.005` | float | Min edge for arbitrage strategy | trading_brain.py |
| `ARB_FEE_RATE` | `0.02` | float | Polymarket fee rate | trading_brain.py |
| `RIVALCLAW_MIN_FV_EDGE` | `0.04` | float | Min edge for fair value directional | trading_brain.py |
| `RIVALCLAW_MIN_REVERSION_EDGE` | `0.06` | float | Min edge for mean reversion | trading_brain.py |
| `RIVALCLAW_MIN_DECAY_EDGE` | `0.03` | float | Min edge for time decay | trading_brain.py |
| `RIVALCLAW_MIN_VOL_SKEW_EDGE` | `0.05` | float | Min edge for vol skew | trading_brain.py |
| `RIVALCLAW_MIN_MOMENTUM_PRICE` | `0.78` | float | Min price for momentum entry | trading_brain.py |
| `RIVALCLAW_KALSHI_FEE` | `0.07` | float | Kalshi taker fee rate | trading_brain.py, paper_wallet.py |
| `RIVALCLAW_NEAR_EXPIRY_HOURS` | `48` | float | Hours to expiry for momentum eligibility | trading_brain.py |
| `RIVALCLAW_KELLY_PROVEN` | `1.0` | float | Kelly fraction for proven strategies | trading_brain.py |
| `RIVALCLAW_KELLY_NEW` | `0.25` | float | Kelly fraction for unproven strategies | trading_brain.py |
| `RIVALCLAW_NO_BOOST` | `1.3` | float | Kelly multiplier for NO direction | trading_brain.py |
| `RIVALCLAW_MIN_CONFIDENCE` | `0.60` | float | Global confidence floor | trading_brain.py |
| `RIVALCLAW_DEAD_ZONE_MIN` | `0.30` | float | Dead zone lower bound (entry price) | trading_brain.py |
| `RIVALCLAW_DEAD_ZONE_MAX` | `0.45` | float | Dead zone upper bound | trading_brain.py |
| `RIVALCLAW_DEAD_ZONE_CONFIDENCE` | `0.75` | float | Min confidence in dead zone | trading_brain.py |
| `RIVALCLAW_VELOCITY_PREFERENCE` | `1.5` | float | Speed preference weight | trading_brain.py |
| `RIVALCLAW_DISABLED_STRATEGIES` | (see below) | csv | Comma-separated strategy names to disable | trading_brain.py |

Default disabled strategies: `bracket_neighbor`, `hedge`, `pairs_trade`,
`bid_gap_arb`, `spot_momentum`, `wipeout_reversal`, `bracket_cone`,
`closing_convergence`, `forecast_delta`, `cross_strike_arb`, `vol_regime`,
`expiry_convergence`.

## Series and Direction Filters

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_LIVE_SERIES` | `KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M` | csv | Allowed series prefixes for live trading | execution_router.py |
| `RIVALCLAW_BLOCK_YES_TRADES` | `1` | bool | Block all YES direction trades | trading_brain.py |
| `RIVALCLAW_REVERSE_SERIES` | `KXDOGE15M` | csv | Series where direction is flipped | trading_brain.py |
| `RIVALCLAW_MIN_ENTRY_PRICE` | `0.08` | float | Minimum entry price | trading_brain.py |
| `RIVALCLAW_MAX_ENTRY_PRICE` | `0.60` | float | Maximum entry price | trading_brain.py |
| `RIVALCLAW_LIVE_MAX_PRICE_DEVIATION` | `0.10` | float | Max fractional deviation from market price | execution_router.py |

## Time-of-Day Weights

| Variable | Default | UTC Hours | File |
|----------|---------|-----------|------|
| `RIVALCLAW_MORNING_WEIGHT` | `0.5` | 08-12 | trading_brain.py |
| `RIVALCLAW_MIDDAY_WEIGHT` | `0.8` | 12-16 | trading_brain.py |
| `RIVALCLAW_AFTERNOON_WEIGHT` | `1.2` | 16-20 | trading_brain.py |
| `RIVALCLAW_EVENING_WEIGHT` | `1.3` | 20-00 | trading_brain.py |
| `RIVALCLAW_NIGHT_WEIGHT` | `0.7` | 00-08 | trading_brain.py |

## Volatility Assumptions

| Variable | Default | Asset | File |
|----------|---------|-------|------|
| `RIVALCLAW_VOL_BITCOIN` | `0.60` | BTC | trading_brain.py |
| `RIVALCLAW_VOL_ETHEREUM` | `0.65` | ETH | trading_brain.py |
| `RIVALCLAW_VOL_DOGECOIN` | `0.90` | DOGE | trading_brain.py |
| `RIVALCLAW_VOL_CARDANO` | `0.80` | ADA | trading_brain.py |
| `RIVALCLAW_VOL_BINANCECOIN` | `0.65` | BNB | trading_brain.py |
| `RIVALCLAW_VOL_BITCOIN_CASH` | `0.75` | BCH | trading_brain.py |
| `RIVALCLAW_VOL_SP500` | `0.17` | S&P 500 | trading_brain.py |
| `RIVALCLAW_VOL_NASDAQ100` | `0.22` | Nasdaq-100 | trading_brain.py |

These are annualized. The self-tuner adjusts them from realized spot data.

## Maker Mode

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_MAKER_ENABLED` | `0` | bool | Enable maker (limit order) mode | execution_router.py |
| `RIVALCLAW_MAKER_OFFSET_PCT` | `0.10` | float | Price improvement (fraction below brain price) | execution_router.py |
| `RIVALCLAW_MAKER_PATIENCE_SEC` | `120` | float | Seconds to wait for fill before cancel | execution_router.py |
| `RIVALCLAW_MAKER_MAX_RESTING` | `5` | int | Max resting orders at once | execution_router.py |
| `RIVALCLAW_MAKER_MIN_FILL_RATE` | `0.30` | float | Min rolling fill rate to stay in maker mode | execution_router.py |

## Self-Tuner

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_TUNER_LOOKBACK_DAYS` | `7` | int | Days of data for tuning | self_tuner.py |
| `RIVALCLAW_TUNER_MAX_ADJUST_PCT` | `0.20` | float | Max parameter change per tune cycle | self_tuner.py |
| `RIVALCLAW_TUNER_MIN_TRADES` | `10` | int | Min trades before adjusting thresholds | self_tuner.py |
| `RIVALCLAW_TUNER_MIN_SNAPSHOTS` | `500` | int | Min spot snapshots for vol tuning | self_tuner.py |
| `RIVALCLAW_TUNER_MIN_SPREAD_SAMPLES` | `50` | int | Min spread samples for slippage tuning | self_tuner.py |
| `RIVALCLAW_TUNER_ROLLBACK_THRESHOLD` | `-0.05` | float | ROI threshold to roll back changes | self_tuner.py |
| `RIVALCLAW_TUNER_COOLDOWN_DAYS` | `3` | int | Days between adjustments to same param | self_tuner.py |

## Risk Engine

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_MAX_CRYPTO_PCT` | `0.40` | float | Max crypto exposure as fraction of balance | risk_engine.py |
| `RIVALCLAW_MAX_ASSET_PCT` | `0.25` | float | Max single-asset exposure fraction | risk_engine.py |
| `RIVALCLAW_TOURNAMENT_LOOKBACK` | `200` | int | Trades per strategy for scoring | risk_engine.py |

## Execution Simulation (Paper)

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_SLIPPAGE_BPS` | `50` | float | Simulated slippage in basis points | paper_wallet.py |
| `RIVALCLAW_LATENCY_PENALTY` | `0.002` | float | Simulated latency cost (fraction) | paper_wallet.py |
| `RIVALCLAW_FILL_RATE_MIN` | `0.80` | float | Min fill rate for paper sim (random 0.80-1.0) | paper_wallet.py |
| `RIVALCLAW_EXECUTION_SIM` | `1` | bool | Enable execution simulation | paper_wallet.py |
| `RIVALCLAW_STOP_LOSS_PCT` | `0.20` | float | Stop-loss threshold | paper_wallet.py |
| `RIVALCLAW_TAKE_PROFIT_PCT` | `0.50` | float | Take-profit threshold | paper_wallet.py |
| `RIVALCLAW_POLYMARKET_FEE` | `0.02` | float | Polymarket fee rate for paper sim | paper_wallet.py |

## External Services

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `KALSHI_API_KEY_ID` | (none) | str | Kalshi API key ID | kalshi_feed.py |
| `KALSHI_PRIVATE_KEY_PATH` | (none) | str | Path to RSA private key PEM | kalshi_feed.py |
| `KALSHI_API_ENV` | `demo` | str | `demo` or `prod` | kalshi_feed.py |
| `TELEGRAM_BOT_TOKEN` | (hardcoded) | str | Telegram bot token | notify.py |
| `TELEGRAM_CHAT_ID` | (none) | str | Telegram chat ID for notifications | notify.py |
| `RIVALCLAW_VENUES` | `kalshi` | csv | Enabled venues (kalshi, polymarket) | simulator.py |
| `RIVALCLAW_SENTIMENT_ENABLED` | `0` | bool | Enable crypto sentiment feed | simulator.py |

## Instance Identity

| Variable | Default | Type | Description | File |
|----------|---------|------|-------------|------|
| `RIVALCLAW_DB_PATH` | `./rivalclaw.db` | str | SQLite database path | simulator.py, all modules |
| `RIVALCLAW_EXPERIMENT_ID` | `arb-bakeoff-2026-03` | str | Experiment identifier | simulator.py |
| `RIVALCLAW_INSTANCE_ID` | `rivalclaw` | str | Instance identifier | simulator.py |
| `RIVALCLAW_STARTING_CAPITAL` | `1000.0` | float | Initial paper balance | paper_wallet.py |
| `RIVALCLAW_STALE_MINUTES` | `30` | float | Stale data threshold (minutes) | trading_brain.py |
