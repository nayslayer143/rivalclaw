# Database Schema

SQLite database at `rivalclaw.db` (configurable via `RIVALCLAW_DB_PATH`).
Uses WAL journal mode for concurrent read/write access.

Migration is defined in `simulator.py` -- `MIGRATION_SQL` constant, applied by
`run.py --migrate`. Additional columns are added idempotently via ALTER TABLE
with OperationalError suppression.

## Tables

### market_data

Price snapshots from all venues.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| market_id | TEXT | NOT NULL | Venue-specific market identifier |
| question | TEXT | NOT NULL | Human-readable market question |
| category | TEXT | | Market category |
| yes_price | REAL | | YES price (0.0-1.0) |
| no_price | REAL | | NO price (0.0-1.0) |
| volume | REAL | | Trading volume |
| end_date | TEXT | | Market close/expiry timestamp (ISO 8601) |
| fetched_at | TEXT | NOT NULL | When this snapshot was taken |
| venue | TEXT | DEFAULT 'polymarket' | Source venue |

**Index:** `idx_market_data_market_time(market_id, fetched_at)`

Used by: stop-loss checks (latest price lookup), market classifier, daily reports.

### paper_trades

All paper and protocol-bridged positions.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| market_id | TEXT | NOT NULL | Market identifier |
| question | TEXT | NOT NULL | Market question text |
| direction | TEXT | NOT NULL | YES or NO |
| shares | REAL | NOT NULL | Number of contracts |
| entry_price | REAL | NOT NULL | Entry price (0.0-1.0) |
| exit_price | REAL | | Exit price at close |
| amount_usd | REAL | NOT NULL | Dollar amount of position |
| pnl | REAL | | Realized PnL (after fees) |
| status | TEXT | NOT NULL | open, closed_win, closed_loss, expired |
| confidence | REAL | NOT NULL, DEFAULT 1.0 | Model confidence score |
| reasoning | TEXT | NOT NULL, DEFAULT '' | Human-readable reasoning + sim metadata |
| strategy | TEXT | NOT NULL, DEFAULT 'arbitrage' | Strategy that generated this trade |
| opened_at | TEXT | NOT NULL | ISO 8601 timestamp |
| closed_at | TEXT | | ISO 8601 timestamp |
| experiment_id | TEXT | | Experiment batch ID |
| instance_id | TEXT | | Instance identifier |
| cycle_started_at_ms | REAL | | Epoch ms when cycle began |
| decision_generated_at_ms | REAL | | Epoch ms when brain decided |
| trade_executed_at_ms | REAL | | Epoch ms when wallet executed |
| signal_to_trade_latency_ms | REAL | | End-to-end latency |
| venue | TEXT | DEFAULT 'polymarket' | Source venue |
| expected_edge | REAL | | Edge estimate at entry |
| binary_outcome | TEXT | | correct or incorrect |
| resolved_price | REAL | | Final resolution price (0.0 or 1.0) |
| resolution_source | TEXT | | kalshi_api or polymarket_api |
| entry_fee | REAL | DEFAULT 0 | Entry fee deducted |
| exit_fee | REAL | | Exit fee deducted |

Key queries:
- Open positions: `WHERE status = 'open'`
- Closed PnL: `SELECT SUM(pnl) FROM paper_trades WHERE status != 'open'`
- Win rate: count `closed_win` / total closed
- Per-strategy performance: `GROUP BY strategy`

### daily_pnl

Daily performance snapshots, one row per day.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| date | TEXT | NOT NULL, UNIQUE | YYYY-MM-DD |
| balance | REAL | NOT NULL | End-of-day balance |
| open_positions | INTEGER | | Count of open trades |
| realized_pnl | REAL | | Day's realized PnL |
| unrealized_pnl | REAL | | Mark-to-market unrealized |
| total_trades | INTEGER | | Cumulative closed trades |
| win_rate | REAL | | Cumulative win rate |
| roi_pct | REAL | | Daily ROI percentage |

Used by: graduation gates (Sharpe, drawdown), daily reports, self-tuner rollback.

### cycle_metrics

Per-cycle timing instrumentation.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| experiment_id | TEXT | | Experiment batch ID |
| instance_id | TEXT | | Instance identifier |
| cycle_started_at | TEXT | | ISO 8601 timestamp |
| markets_fetched | INTEGER | | Markets returned by feeds |
| opportunities_detected | INTEGER | | Brain output count |
| opportunities_qualified | INTEGER | | After risk engine filter |
| trades_executed | INTEGER | | Actually executed |
| stops_closed | INTEGER | | Positions closed by stops |
| fetch_ms | REAL | | Market data fetch time |
| analyze_ms | REAL | | Brain analysis time |
| wallet_ms | REAL | | Trade execution time |
| total_cycle_ms | REAL | | End-to-end cycle time |

**Index:** `idx_cycle_metrics_time(cycle_started_at)`

### context

Key-value configuration state.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| chat_id | TEXT | NOT NULL, PK | Namespace (always 'rivalclaw') |
| key | TEXT | NOT NULL, PK | Config key |
| value | TEXT | NOT NULL | Config value |

Known keys:
- `starting_balance`: Paper starting balance (default 1000.00).
- `trading_status`: Set to `halted` by circuit breaker.

### kalshi_extra

Extended market metadata from Kalshi API.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| market_id | TEXT | NOT NULL | Kalshi ticker |
| event_ticker | TEXT | | Parent event ticker |
| yes_bid | REAL | | Best yes bid (cents) |
| yes_ask | REAL | | Best yes ask (cents) |
| no_bid | REAL | | Best no bid (cents) |
| no_ask | REAL | | Best no ask (cents) |
| last_price | REAL | | Last traded price (cents) |
| volume_24h | REAL | | 24-hour volume |
| open_interest | REAL | | Open interest |
| close_time | TEXT | | Market close timestamp |
| strike_type | TEXT | | greater_or_equal, between, etc. |
| cap_strike | REAL | | Upper bracket strike |
| floor_strike | REAL | | Lower bracket strike |
| rules_primary | TEXT | | Resolution rules text |
| fetched_at | TEXT | NOT NULL | Snapshot timestamp |

**Index:** `idx_kalshi_extra_market_time(market_id, fetched_at)`

Used by: fair value computation (strike parsing), bracket detection, vol skew.

### spot_prices

Crypto and index spot price snapshots.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| crypto_id | TEXT | NOT NULL | Asset ID (e.g., bitcoin, sp500) |
| price_usd | REAL | NOT NULL | Spot price in USD |
| fetched_at | TEXT | NOT NULL | Snapshot timestamp |

**Index:** `idx_spot_prices_crypto_time(crypto_id, fetched_at)`

Used by: self-tuner (realized vol computation), regime detector, fair value models.

### tuning_log

Self-tuner parameter adjustment history.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| date | TEXT | NOT NULL | YYYY-MM-DD |
| parameter | TEXT | NOT NULL | Env var name adjusted |
| old_value | REAL | NOT NULL | Value before adjustment |
| new_value | REAL | NOT NULL | Value after adjustment |
| reason | TEXT | NOT NULL | Human-readable reason |
| sample_size | INTEGER | NOT NULL | Data points used |
| tuned_at | TEXT | NOT NULL | ISO 8601 timestamp |

Used by: daily reports, cooldown enforcement, rollback checks.

### live_orders

All live and shadow order records.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| intent_id | TEXT | NOT NULL | UUID linking decision to order |
| client_order_id | TEXT | UNIQUE, NOT NULL | UUID for Kalshi idempotency |
| kalshi_order_id | TEXT | | Kalshi-assigned order ID |
| ticker | TEXT | NOT NULL | Kalshi market ticker |
| action | TEXT | NOT NULL | buy or sell |
| side | TEXT | NOT NULL | yes or no |
| count | INTEGER | NOT NULL | Number of contracts |
| yes_price | INTEGER | NOT NULL | Limit price in cents |
| order_type | TEXT | DEFAULT 'limit' | Order type |
| status | TEXT | DEFAULT 'pending' | pending/resting/filled/settled/cancelled/rejected/error |
| fill_price | INTEGER | | Actual fill price (cents) |
| fill_count | INTEGER | | Actual contracts filled |
| submitted_at | TEXT | | ISO 8601 submission time |
| filled_at | TEXT | | ISO 8601 fill time |
| mode | TEXT | NOT NULL | live or shadow |
| error_message | TEXT | | API error detail |
| rejection_reason | TEXT | | Pre-flight rejection reason |
| cycle_id | TEXT | | Cycle identifier |
| strategy | TEXT | | Strategy name |
| market_question | TEXT | | Human-readable question |
| order_mode | TEXT | DEFAULT 'taker' | taker or maker |
| brain_price | REAL | | Original brain entry price |
| maker_savings | REAL | | Price improvement from maker mode |
| fill_time_sec | REAL | | Seconds from submit to fill |
| outcome | TEXT | | win or loss (after settlement) |
| pnl_cents | INTEGER | | Realized PnL in cents |

**Indexes:** `idx_live_orders_status(status)`, `idx_live_orders_mode(mode)`

Status lifecycle: `pending` -> `resting` -> `filled` -> `settled` (or `cancelled`/`rejected`/`error` at any stage).

### live_reconciliation

Paper-vs-live slippage tracking.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| live_order_id | INTEGER | FK -> live_orders(id) | Associated live order |
| paper_entry_price | REAL | | What the brain intended |
| live_fill_price | REAL | | What Kalshi actually filled |
| slippage_delta_bps | REAL | | Absolute slippage in basis points |
| paper_amount_usd | REAL | | Intended dollar amount |
| live_amount_usd | REAL | | Actual dollar amount filled |
| reconciled_at | TEXT | | ISO 8601 timestamp |

### account_snapshots

Kalshi account state snapshots.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment |
| balance_cents | INTEGER | | Cash balance in cents |
| portfolio_value_cents | INTEGER | | Portfolio value in cents |
| open_positions | INTEGER | | Count of open positions |
| fetched_at | TEXT | | ISO 8601 timestamp |

One row inserted per `sync_account()` call (every cycle in live/shadow mode).

## Tables Not in MIGRATION_SQL

These tables are created by other modules and may not exist in all deployments:

- **market_scores**: Created by `market_classifier.py`. Stores speed/quality scores.
- **strategy_lab tables**: Created by `strategy_lab/` modules (experiments, hypotheses, etc.).
- **event_log**: Created by `event_logger.py`. Structured event stream for Strategy Lab.

## Relationships

```
paper_trades.market_id  <-->  market_data.market_id   (price lookups)
paper_trades.market_id  <-->  kalshi_extra.market_id   (strike/bracket data)
live_orders.id          --->  live_reconciliation.live_order_id  (FK)
spot_prices.crypto_id   <-->  trading_brain.SERIES_TO_UNDERLYING  (fair value)
tuning_log.parameter    <-->  self_tuner.CLAMPS keys  (parameter bounds)
```

## Index Recommendations

The existing indexes cover the primary query patterns. Additional indexes that
would help at scale:

```sql
-- Fast open-position lookups (used every cycle)
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);

-- Strategy tournament scoring
CREATE INDEX IF NOT EXISTS idx_paper_trades_closed ON paper_trades(strategy, closed_at)
    WHERE status != 'open';

-- Live exposure calculation
CREATE INDEX IF NOT EXISTS idx_live_orders_active ON live_orders(mode, status)
    WHERE status IN ('pending', 'resting', 'filled');

-- Spot price regime detection (last 30 min of BTC)
CREATE INDEX IF NOT EXISTS idx_spot_recent ON spot_prices(crypto_id, fetched_at DESC);
```

## Backup

No automated backup system. The WAL journal mode provides crash recovery.
For manual backup: `sqlite3 rivalclaw.db ".backup rivalclaw-backup.db"`.

The database is approximately 50-100 MB after a month of continuous operation.
The largest tables are `market_data`, `spot_prices`, and `kalshi_extra` due to
per-cycle snapshots.
