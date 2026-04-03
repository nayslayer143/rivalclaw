# Trading Pipeline

End-to-end flow from market data ingestion through trade execution to settlement.
Each cycle runs every 5 minutes via cron (`run.py --run` -> `simulator.run_loop()`).

---

## 1. Market Data Ingestion (`kalshi_feed.py`)

### Authentication

RSA key-pair signature scheme. Three headers per request:

- `KALSHI-ACCESS-KEY` -- API key ID from env `KALSHI_API_KEY_ID`
- `KALSHI-ACCESS-TIMESTAMP` -- Unix epoch milliseconds
- `KALSHI-ACCESS-SIGNATURE` -- RSA-PSS signature of `timestamp + METHOD + /trade-api/v2/path`

Private key loaded from `KALSHI_PRIVATE_KEY_PATH`. Signing uses PSS padding with
SHA-256 and `DIGEST_LENGTH` salt. Prod vs demo base URL selected by `KALSHI_API_ENV`.

### Series Coverage

57 series across 5 asset classes:

| Class | Series | Count | Resolution |
|-------|--------|-------|------------|
| Crypto 15-min | KXDOGE15M, KXADA15M, KXBNB15M, KXBCH15M, KXBTC15M, KXETH15M | 6 | 15 min |
| Crypto hourly | KXBTC, KXETH, KXBTCMAXD, KXBTCD, KXETHD, KXSOLD, KXXRPD | 7 | 1 hr |
| Index hourly | KXINXU (S&P 500), KXNASDAQ100U | 2 | 1 hr |
| Weather daily | 21 high-temp cities + 7 low-temp cities | 28 | Same day |
| Commodities/FX | KXGOLDD, KXSILVERD, KXTNOTED, KXUSDJPY, KXINXSPX, KXINXNDX | 6 | Daily |

For each series: `GET /events?series_ticker=X&status=open` then
`GET /markets?event_ticker=Y&status=open` for each event.

### Caching

Data cached to two SQLite tables per cycle:

- `market_data` -- market_id, question, yes/no price, volume, close time, venue
- `kalshi_extra` -- event_ticker, bid/ask, open interest, strike_type, floor/cap strike, rules

`CACHE_MAX_AGE_HOURS = 0` (always fetch fresh). Falls back to cache on API failure.

### Normalization

Raw Kalshi fields (dollar suffixes) converted to internal format:
- `yes_bid_dollars` -> `yes_bid` (cents)
- `last_price_dollars` -> `last_price` (cents)
- Final output: float prices 0.0-1.0 via `_cents_to_float()`

Spot prices fetched in parallel:
- Crypto: CoinGecko via `spot_feed.py`
- Indices: Yahoo Finance via `index_feed.py`
- Weather: NWS forecast via `weather_feed.py`

All spot prices logged to `spot_prices` table for realized vol computation.

---

## 2. Market Classification (`market_classifier.py`)

### Categories

8 categories scored on speed (0-3) and clarity (0-3):

| Category | Speed | Clarity | Examples |
|----------|-------|---------|----------|
| crypto_fast | 3 | 3 | "DOGE price", "15 min" |
| weather | 3 | 3 | "temperature", "high temp" |
| sports | 3 | 3 | "NBA", "Super Bowl" |
| crypto_daily | 2 | 3 | "Bitcoin price", "price range" |
| commodities | 2 | 3 | "gold price", "USD/JPY" |
| econ | 2 | 2 | "CPI", "FOMC", "earnings" |
| event | 1 | 1 | "launch", "Oscar" |
| politics | 0 | 0 | "election", "vote" |

### Time Decay Score

Based on hours to close:

- <6h: 3
- <24h: 2
- <72h: 1
- 72h+: 0

### Priority Formula

```
priority = (speed * 2) + (clarity * 2) + time_decay
```

Max possible: 15. Minimum threshold: `MIN_PRIORITY = 3` (env configurable).

### Filtering

Two gates:
1. Priority >= `MIN_PRIORITY` (default 3)
2. Closes within `MAX_EXPIRY_HOURS` (default 24h)

Markets failing either gate are rejected. Scores stored in `market_scores` table.

---

## 3. Brain Analysis (`trading_brain.py`)

### Priority Cascade

Strategies run in fixed order per market. First match wins (no stacking).

Event-level strategies run first on grouped brackets:
1. cross_strike_arb
2. bracket_cone
3. bracket_neighbor (DISABLED)
4. pairs_trade (DISABLED)
5. election_field_arb

Then per-market strategies in cascade order:
1. arbitrage
2. fair_value_directional
3. spot_momentum (DISABLED)
4. vol_skew
5. time_decay
6. mean_reversion
7. expiry_acceleration
8. closing_convergence (DISABLED)
9. correlation_echo
10. polymarket_convergence
11. liquidity_fade
12. volume_confirmed
13. multi_timeframe
14. vol_regime (DISABLED)
15. correlation_cascade
16. forecast_delta (DISABLED)
17. expiry_convergence (DISABLED)
18. fade_public
19. vol_straddle
20. price_lag_arb
21. bid_gap_arb (DISABLED)

Max 2 trades per event_ticker (`MAX_TRADES_PER_EVENT`).

### Fair Value Computation

Black-Scholes-style binary option model:

- **Threshold contracts** (`greater_or_equal`/`less`): `P = N(d2)` where
  `d2 = ln(spot/strike) / (vol * sqrt(T))`
- **Bracket contracts** (`between`): `P = P(above_floor) - P(above_cap)`

Volatility source priority:
1. Realized vol from last 200 spot snapshots (annualized)
2. Static baseline (BTC 0.60, DOGE 0.90, S&P 0.17, etc.)
3. Weather: forecast error / spot, annualized

Edge = fair_value - (market_price + fee). Bucket-adjusted thresholds:
- 0.15-0.30 entry: 0.4x threshold (sweet spot -- $129 avg profit)
- 0.30-0.50 entry: 2.0x threshold (money burner -- -$18 avg)

### Kelly Sizing

```
kelly = (confidence * b - (1 - confidence)) / b
where b = (1 / entry_price) - 1
```

Multiplied by:
- **Proven factor**: 1.0x for arbitrage/fair_value/time_decay, 0.25x for everything else
- **NO boost**: 1.3x for NO direction (45% WR vs 37% YES)
- **Time-of-day weight**: 0.5x morning (08-12 UTC) to 1.3x evening (20-00 UTC)

Hard caps: `MAX_POSITION_PCT` (10% of balance), `MAX_LOSS_PCT` (3% of balance per trade).

### Post-Decision Filters

Applied globally after all strategies generate signals:

1. **Signal reversal**: KXDOGE15M flipped (15% WR on NO -> 85% WR reversed)
2. **YES block**: All YES trades blocked (15% WR, -$44 net over 88 trades). Exemption for reversed series.
3. **Entry price bounds**: $0.08-$0.60 (NO sweet spot is $0.10-$0.30)
4. **Confidence floor**: >= 0.60 (losers avg 0.52, winners avg 0.79)
5. **Dead zone**: $0.30-$0.45 entry requires confidence >= 0.75 (41-47% WR zone)
6. **Velocity sort**: final ranking = confidence * velocity_boost from market priority

---

## 4. Risk Engine (`risk_engine.py`)

### Regime Detection

Classifies BTC spot data from last 30 minutes:

| Regime | Condition | Effect |
|--------|-----------|--------|
| volatile | 15-min return std > 0.8% | fair_value 1.2x, others 0.6x |
| trending | abs(trend) > vol * 0.5 | momentum 1.5x, mean_reversion 0.5x |
| calm | default | time_decay 1.3x, momentum 0.5x |

### Strategy Tournament

Scores each strategy from last 200 closed trades (ROI-driven, not WR-gated):

| ROI | Score | Effect |
|-----|-------|--------|
| < -10% | 0.0 | Killed -- no allocation |
| < 0% | 0.25 | Underweight |
| > 10% | 1.5 | Overweight |
| > 0%, WR > 40% | 1.0 | Normal |
| else | 0.5 | Below average |

Minimum 5 trades before scoring (untested strategies get 0.5).

### Exposure Limits

- Total crypto: 40% of balance
- Single asset (BTC/ETH/DOGE/BNB/WEATHER/INDEX): 25% of balance

### Final Sizing

```
final_amount = brain_amount * tournament_score * regime_mult * speed_mult
```

Speed multiplier: 1.0 for Kalshi, 0.5 for Polymarket.
Capped at 95% of `MAX_POSITION_PCT * balance`. Trades < $0.10 are dropped.

---

## 5. Execution (`execution_router.py` -> `kalshi_executor.py`)

### Execution Modes

| Mode | Behavior |
|------|----------|
| paper | Skip (no action). Default mode. |
| shadow | Log order to `live_orders` as dry run. No API call. |
| live | Full pre-flight check, submit to Kalshi, poll for fills. |

Mode set by `RIVALCLAW_EXECUTION_MODE` env var.

### 10-Point Pre-Flight Checklist

1. **Mode check** -- must be "live" or "shadow"
2. **Kill switch** -- `RIVALCLAW_LIVE_KILL_SWITCH=1` blocks all orders
3. **Balance check** -- Kalshi account must cover order cost
4. **Exposure check** -- total open + new order <= `max_exposure_usd` (default $10)
5. **Order size check** -- clipped to `max_order_usd` (default $2)
6. **Contract count** -- clipped to `max_contracts` (default 5)
7. **Rate check** -- per-cycle (2 non-weather, 30 weather) and per-hour (10) limits
8. **Series check** -- ticker must match allowed series prefix list
   - 8a. Anti-stacking: no duplicate orders on same ticker
   - 8b. Block YES on 15-min contracts (9% live WR)
   - 8c. Anti-self-hedge: no opposite-side bet on ticker with active position
9. **Price sanity** -- entry within 10% of last market price
10. **Staleness check** -- decision must be < 5 minutes old

Shares floored to whole contracts; orders < 1 contract are rejected.

### Order Submission

1. Build payload: ticker, action (buy), side (yes/no), count, yes_price (cents, 1-99)
2. Submit via `POST /portfolio/orders` with RSA auth
3. Exponential backoff on 429 (up to 3 retries)
4. Local rate limiter: 10 writes/second sliding window

### Maker Mode (Optional)

When `RIVALCLAW_MAKER_ENABLED=1`:
- Entry price offset by `MAKER_OFFSET_PCT` (default 10%) for better fills
- Resting orders polled with patience window (default 120s)
- Cancelled if not filled within window (`poll_or_cancel`)
- Max resting orders: 5. Minimum fill rate threshold: 30%.
- Existing resting orders on same ticker cancelled before new quote

### Fill Tracking

- Taker: polled 3 times at 2s intervals
- Maker: polled every 10s up to patience window, then cancelled
- Fill data logged to `live_orders` (fill_price, fill_count, filled_at)
- Reconciliation logged to `live_reconciliation` (slippage_delta_bps)
- Slippage > 500 bps triggers Telegram alert

---

## 6. Resolution (`simulator.py`)

### Kalshi Resolution

For each open paper trade with `market_id LIKE 'KX%'`:

1. `GET /markets/{ticker}` to check `result` field
2. If result is "yes" or "no":
   - Compare against our bet direction
   - Win: exit_price = 1.0, loss: exit_price = 0.0
   - PnL = shares * (exit_price - entry_price) - entry_fee
   - Status set to `closed_win` or `closed_loss`
   - Resolution source: `kalshi_api`

### Polymarket Resolution

For each open paper trade with `venue = 'polymarket'`:

1. `GET gamma-api.polymarket.com/markets/{id}`
2. Check `resolved`/`closed` flag and `outcomePrices` array
3. Winning outcome: label with price >= 0.95
4. Same win/loss/PnL logic as Kalshi

### Live Order Settlement

Filled live orders transition: `filled` -> `settled` after market resolves.
Protocol wallet credited via `protocol_adapter.credit_resolution()`.

### Balance Floor

If Kalshi account balance drops to `$25` (configurable):
- Kill switch auto-activated in `.env`
- All live trading halted immediately

Paper balance circuit breaker: if estimated balance < `$100`, trading halted
and `context.trading_status` set to `halted`.

---

## Cycle Timing

Every cycle logs to `cycle_metrics`:

| Metric | Description |
|--------|-------------|
| fetch_ms | Market data + spot price fetching |
| analyze_ms | Brain strategy cascade + signal generation |
| wallet_ms | Trade execution + stop checks |
| total_cycle_ms | End-to-end cycle time |

Also tracked: markets_fetched, opportunities_detected, opportunities_qualified,
trades_executed, stops_closed.

---

## Data Flow Diagram

```
kalshi_feed.fetch_markets()          spot_feed + index_feed + weather_feed
        |                                        |
        v                                        v
market_classifier.classify_and_filter()    spot_prices dict
        |                                        |
        +--------------------+-------------------+
                             |
                             v
                trading_brain.analyze()
                     |           |
                     v           v
             risk_engine       decisions[]
          .adjust_decision()
                     |
                     v
           protocol_adapter.execute_trade()
                     |
            +--------+--------+
            |                 |
            v                 v
      paper_wallet      execution_router.route_trade()
                              |
                     +--------+--------+
                     |        |        |
                     v        v        v
                  paper    shadow     live
                                       |
                                       v
                              kalshi_executor.submit_order()
                                       |
                                       v
                              poll_order_status() / poll_or_cancel()
```
