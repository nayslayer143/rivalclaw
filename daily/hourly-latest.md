# RIVALCLAW HOURLY REPORT — 2026-03-25 21:38 UTC

## Wallet
| Metric | Value |
|--------|-------|
| Balance | $55,188.21 |
| Total PnL | $+54,188.21 (+5418.8%) |
| Trades | 1005 (W:370 L:580 Open:55) |
| Win Rate | 38.9% |
| Capital Velocity | 178.9x |

## Hourly Trend
| Hour | Trades | Wins | WR% | Avg PnL | Total PnL |
|------|--------|------|-----|---------|-----------|
| 03-25T14 | 51 | 21 | 41% | $145.84 | $+7,437.77 |
| 03-25T15 | 46 | 20 | 43% | $151.01 | $+6,946.53 |
| 03-25T16 | 54 | 24 | 44% | $77.30 | $+4,173.95 |
| 03-25T17 | 59 | 21 | 36% | $168.13 | $+9,919.57 |
| 03-25T18 | 55 | 14 | 25% | $61.67 | $+3,391.61 |
| 03-25T19 | 62 | 25 | 40% | $57.55 | $+3,568.06 |
| 03-25T20 | 55 | 19 | 35% | $74.03 | $+4,071.42 |
| 03-25T21 | 41 | 12 | 29% | $-128.77 | $-5,279.48 |

## Strategy Leaderboard
| Strategy | Trades | WR% | W/L Ratio | PnL | Status |
|----------|--------|-----|-----------|-----|--------|
| fair_value_directional | 608 | 41% | 2.4x | $+28,631.23 | WINNING |
| bracket_neighbor | 178 | 34% | 9.5x | $+25,799.98 | positive |
| spot_momentum | 9 | 33% | 11.6x | $+1,767.56 | positive |
| hedge | 12 | 25% | 6.8x | $+44.49 | positive |
| time_decay | 9 | 22% | 5.5x | $+36.78 | positive |
| mean_reversion | 18 | 44% | 1.3x | $+16.69 | positive |
| expiry_acceleration | 7 | 29% | 6.6x | $+2.68 | positive |
| cross_strike_arb | 14 | 36% | 1.2x | $-87.32 | LOSING |
| closing_convergence | 34 | 59% | 0.2x | $-144.69 | LOSING |
| liquidity_fade | 3 | 0% | 0.0x | $-221.72 | LOSING |
| near_expiry_momentum | 48 | 29% | 0.2x | $-690.51 | LOSING |
| bracket_cone | 10 | 20% | 1.0x | $-966.96 | LOSING |

## Tuner Changes (this cycle)
- **RIVALCLAW_SLIPPAGE_BPS**: 50.0 -> 60.0 (Median spread=5454.5bps from 1420 observations, n=1420)

## Diagnosis
- WATCH: **cross_strike_arb** — negative $-87 after 14 trades
- KILL: **closing_convergence** — 34 trades, $-145 PnL, dead weight
- KILL: **near_expiry_momentum** — 48 trades, $-691 PnL, dead weight
- STABLE: Win rate holding (35%)
- Cycles run: 1596
- Market regime: **calm** (vol=0.0003)

---
*Next tuner: top of the hour | Next daily report: 9pm PST*