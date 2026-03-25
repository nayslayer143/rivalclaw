# RIVALCLAW HOURLY REPORT — 2026-03-25 10:08 UTC

## Wallet
| Metric | Value |
|--------|-------|
| Balance | $12,551.92 |
| Total PnL | $+11,551.92 (+1155.2%) |
| Trades | 422 (W:159 L:238 Open:25) |
| Win Rate | 40.1% |
| Capital Velocity | 45.9x |

## Hourly Trend
| Hour | Trades | Wins | WR% | Avg PnL | Total PnL |
|------|--------|------|-----|---------|-----------|
| 03-24T21 | 28 | 17 | 61% | $22.77 | $+637.50 |
| 03-24T22 | 17 | 8 | 47% | $134.61 | $+2,288.44 |
| 03-24T23 | 8 | 3 | 38% | $-24.14 | $-193.11 |
| 03-25T00 | 7 | 4 | 57% | $604.33 | $+4,230.34 |
| 03-25T07 | 44 | 9 | 20% | $-52.82 | $-2,323.96 |
| 03-25T08 | 16 | 4 | 25% | $-6.69 | $-107.12 |
| 03-25T09 | 11 | 2 | 18% | $0.53 | $+5.83 |
| 03-25T10 | 16 | 3 | 19% | $127.04 | $+2,032.63 |

## Strategy Leaderboard
| Strategy | Trades | WR% | W/L Ratio | PnL | Status |
|----------|--------|-----|-----------|-----|--------|
| fair_value_directional | 308 | 43% | 3.0x | $+12,585.43 | WINNING |
| mean_reversion | 14 | 50% | 2.8x | $+646.22 | WINNING |
| hedge | 4 | 50% | 162.8x | $+51.79 | positive |
| time_decay | 9 | 22% | 5.5x | $+36.78 | positive |
| spot_momentum | 2 | 0% | 0.0x | $-0.10 | testing |
| cross_strike_arb | 2 | 0% | 0.0x | $-110.73 | LOSING |
| near_expiry_momentum | 48 | 29% | 0.2x | $-690.51 | LOSING |
| bracket_cone | 10 | 20% | 1.0x | $-966.96 | LOSING |

## Tuner Changes (this cycle)
- **RIVALCLAW_SLIPPAGE_BPS**: 50.0 -> 60.0 (Median spread=5454.5bps from 1420 observations, n=1420)

## Diagnosis
- KILL: **near_expiry_momentum** — 48 trades, $-691 PnL, dead weight
- DEGRADING: Win rate trending down (39% -> 21%)
- Cycles run: 905
- Market regime: **calm** (vol=0.0004)

---
*Next tuner: top of the hour | Next daily report: 9pm PST*