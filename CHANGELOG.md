# RivalClaw Changelog

## 2026-03-24 — Day 1: Genesis → $8,717

### Morning (08:00-11:00 UTC)
- **INIT:** System started with arb-only strategy on Polymarket
- **ADDED:** Kalshi API integration (RSA auth, fast-resolution series)
- **ADDED:** CoinGecko spot feed for crypto prices
- **ADDED:** fair_value_directional strategy (Black-Scholes on Kalshi contracts)
- **ADDED:** near_expiry_momentum strategy
- **CRON:** Set to 2-minute cycles (was 5-min)
- **RESULT:** First 3 trades executed — all near_expiry_momentum on BTC bracket NO

### Midday (11:00-15:00 UTC) — The Crash
- **LOSS:** -$624 from near_expiry_momentum on bracket contracts
- **ROOT CAUSE:** Bracket contracts have extreme intra-period price swings that trigger stop-losses before expiry
- **FIX:** Excluded bracket contracts from momentum strategy
- **FIX:** Added event-level dedup (one trade per event_ticker)
- **ADDED:** 8-strategy engine: arb, fair_value, spot_momentum, cross_strike_arb, mean_reversion, time_decay, vol_skew, calibration(stub)
- **ADDED:** Hedge engine (defined-risk spreads)
- **ADDED:** Fractional Kelly (0.25x for unproven, 1.0x for proven)
- **ADDED:** Auto wallet reload at <$100 balance

### Afternoon (15:00-21:00 UTC) — The Recovery
- **FIX:** Disabled stop-losses on fast contracts (<60 min to expiry)
- **FIX:** Enabled bracket fair value computation (P(floor <= spot <= cap))
- **TUNED:** Lowered edge thresholds (FV: 4% → 2%), position size (10% → 5%)
- **TUNED:** Lower sim friction (slippage 50→30bps, fill 80→90%)
- **RESULT:** fair_value_directional started printing money. +$7,390 by end of session.
- **ADDED:** Self-tuner (3 loops: vol, strategy scoring, spread calibration)
- **ADDED:** Risk engine (regime detection, strategy tournament, portfolio limits)
- **KILLED:** near_expiry_momentum (post-mortem in strategies/graveyard/)

### Evening (21:00-23:00 UTC) — Expansion
- **ADDED:** Weather feed (NWS API for DC/SF/NYC temperature forecasts)
- **ADDED:** Expanded Kalshi series: weather, gold, silver, treasury, FX
- **RESULT:** Cross-strike arb fired for first time on weather brackets
- **ADDED:** Market classifier (resolution speed + clarity scoring)
- **ADDED:** Velocity-weighted ranking (faster markets rank higher)
- **KILLED:** bracket_cone (auto-killed by tournament, -$1,039, correlated losses)
- **ADDED:** Hourly reporting system
- **MOVED:** Daily report to 9pm PST
- **MOVED:** Self-tuner from daily to hourly

### Day 1 Summary

| Metric | Value |
|--------|-------|
| Starting balance | $1,000 |
| Ending balance | $8,717 |
| Total PnL | +$7,717 (+772%) |
| Total trades | 308 |
| Win rate | 45.2% |
| Best strategy | fair_value_directional (+$9,113) |
| Worst strategy | bracket_cone (-$1,039) |
| Strategies tested | 9 |
| Strategies killed | 2 |
| Capital velocity | 29.2x |
| Cycles run | 479 |
| Markets scanned | 638 per cycle |

### Architecture at End of Day 1

```
Cron 2min → Fetch (Kalshi+Polymarket+CoinGecko+NWS)
          → Classify (speed/clarity/decay scoring)
          → Brain (7 active strategies, event dedup)
          → Risk Engine (regime + tournament + limits)
          → Wallet (execution sim, no stops on fast)
          → Metrics

Cron hourly → Self-Tuner (vol, thresholds, slippage) → Report
Cron 9pm PST → Daily Report → Git push
```
