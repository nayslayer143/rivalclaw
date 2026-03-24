# Active Strategies — 2026-03-24

## Tier 1: Proven Winners

### fair_value_directional
- **PnL:** +$9,113 | **WR:** 50% | **W/L Ratio:** 2.5x | **Trades:** 228
- **Edge:** Black-Scholes fair value vs market price on Kalshi threshold + bracket contracts
- **Kelly:** Full (1.0x) — proven strategy
- **Sweet spot:** Entry price 0.10-0.30 ($101 avg profit in this bucket)

### mean_reversion
- **PnL:** +$314 | **WR:** 45% | **W/L Ratio:** 3.8x | **Trades:** 11
- **Edge:** Bet against crowd when fair value ≈ 0.50 but market disagrees
- **Kelly:** Half (0.5x) — small sample but positive
- **Note:** Only fires on 15-min crypto contracts in coin-flip zone

## Tier 2: Positive / Testing

### time_decay
- **PnL:** +$19.80 | **WR:** 25% | **W/L Ratio:** 11.0x | **Trades:** 4
- **Edge:** Very near expiry (<10 min), buy the cheaper side
- **Kelly:** Full (1.0x) — high ratio but tiny sample
- **Note:** Huge individual wins when it hits, small losses when it doesn't

### cross_strike_arb
- **PnL:** ~$0 | **Trades:** 2
- **Edge:** Bracket sum deviation from 1.0
- **Kelly:** Half (0.5x) — rarely fires
- **Note:** First fired on weather markets (DC temperature brackets)

### spot_momentum
- **PnL:** $0 | **Trades:** 0
- **Edge:** Crypto trend detection from spot price history
- **Kelly:** Half (0.5x) — untested
- **Note:** Needs strong momentum (>0.3% in 10 min) to trigger

### vol_skew
- **PnL:** $0 | **Trades:** 0
- **Edge:** Realized vol > implied vol → buy OTM
- **Kelly:** Half (0.5x) — untested
- **Note:** Needs vol ratio > 1.2x to trigger

### hedge
- **PnL:** -$0.25 | **Trades:** 1
- **Edge:** Defined-risk spreads on Kalshi threshold contracts
- **Kelly:** N/A (30% of primary trade size)
- **Note:** Structural — reduces downside on primary trades

### arbitrage
- **PnL:** $0 | **Trades:** 0
- **Edge:** YES + NO + fees < 1.0
- **Kelly:** Full (1.0x) — guaranteed profit if found
- **Note:** Markets too efficient — rarely fires

## Graveyard

See `strategies/graveyard/` for killed strategies with full post-mortems.

| Strategy | Killed | PnL | Reason |
|----------|--------|-----|--------|
| [near_expiry_momentum](../graveyard/near_expiry_momentum.md) | 2026-03-24 | -$691 | 0.2x ratio, bracket trap, thin edge |
| [bracket_cone](../graveyard/bracket_cone.md) | 2026-03-24 | -$1,039 | Correlated bracket losses, no real diversification |
