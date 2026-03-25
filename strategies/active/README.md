# Active Strategies — Updated 2026-03-25

> **For future bots:** Each strategy has performance data, edge source, and operational notes. Study the graveyard too — those failures taught us more than the wins.

## Intelligence Layer (applies to ALL strategies)

These data-driven multipliers were extracted from 375 trades and apply across every strategy:

| Signal | Data Source | Multiplier |
|--------|------------|------------|
| NO direction bias | NO: 45% WR $34 avg vs YES: 37% WR $15 avg | 1.3x sizing on NO |
| Entry 0.15-0.30 | $129 avg profit (THE GOLD MINE) | 0.4x edge threshold |
| Entry 0.30-0.50 | -$18 avg profit (MONEY BURNER) | 2.0x edge threshold |
| Afternoon 16-20 UTC | 57% WR, +$2,784 | 1.2x sizing |
| Evening 20-00 UTC | 51% WR, +$5,257 | 1.3x sizing |
| Morning 08-12 UTC | 21% WR, -$303 | 0.5x sizing |

## Tier 1: Proven Winners

### fair_value_directional
- **PnL:** +$10,700 | **WR:** 46% | **W/L Ratio:** 2.6x | **Trades:** 271
- **Edge:** Black-Scholes fair value on Kalshi threshold + bracket contracts
- **Kelly:** Full (1.0x) × NO boost × time-of-day weight
- **Sweet spot:** NO direction, entry 0.15-0.30, BTC/ETH daily brackets
- **Best single trade:** +$2,187 (ETH bracket NO at 0.15 → 0.99)
- **Market types:** BTC brackets (+$7,879), ETH brackets (+$3,952)
- **Real-time vol:** Uses 200 recent spot snapshots, not static estimates

### mean_reversion
- **PnL:** +$646 | **WR:** 50% | **W/L Ratio:** 2.8x | **Trades:** 14
- **Edge:** Bet against crowd when fair value ≈ 0.50 but market disagrees
- **Kelly:** Half (0.5x) × direction + time weights
- **Best trade:** +$278 (DOGE 15-min YES in coin-flip zone)

## Tier 2: Positive / Testing

### time_decay
- **PnL:** +$17.66 | **WR:** 17% | **W/L Ratio:** 14.2x | **Trades:** 6
- **Edge:** Very near expiry (<10 min), buy the cheaper side
- **Kelly:** Full (1.0x) — extreme ratio, tiny sample
- **Note:** When it wins, it wins huge. Needs more data.

### hedge
- **PnL:** +$25.29 | **WR:** 33% | **W/L Ratio:** 160x | **Trades:** 3
- **Edge:** Defined-risk spreads on Kalshi threshold contracts
- **Kelly:** 30% of primary trade size
- **Note:** Structural insurance, not a standalone profit strategy

## Tier 3: Scanning / Rarely Fires

### cross_strike_arb
- **PnL:** -$110.73 | **WR:** 0% | **Trades:** 2
- **Edge:** Bracket sum deviation from 1.0
- **Note:** First fired on weather brackets. Needs >2% sum gap after fees. Very rare.

### spot_momentum
- **PnL:** $0 | **Trades:** 0
- **Edge:** Crypto trend detection from spot_prices history
- **Note:** Needs >0.2% move in 10 min. Hasn't triggered yet.

### vol_skew
- **PnL:** $0 | **Trades:** 0
- **Edge:** Buy OTM when realized vol > 1.2x implied vol
- **Note:** Needs sufficient vol divergence. Watching.

### arbitrage
- **PnL:** $0 | **Trades:** 0
- **Edge:** YES + NO + fees < 1.0. Pure arb.
- **Note:** Markets too efficient. Costs nothing to scan.

## Performance by Market Type

| Market | Trades | WR% | PnL | Avg | Verdict |
|--------|--------|-----|-----|-----|---------|
| BTC daily/hourly | 162 | 47% | +$7,879 | $48.64 | **BEST MARKET** |
| ETH daily/hourly | 73 | 48% | +$3,952 | $54.14 | **STRONG** |
| 15-min crypto | 127 | 34% | -$1,569 | -$12.36 | Learning (improving) |
| Weather (DC/SF) | 3 | 0% | -$146 | -$48.73 | Too few trades |
| NYC Temp | 10 | 20% | -$18 | -$1.82 | Neutral |

## Graveyard

See `strategies/graveyard/` for killed strategies with full post-mortems.

| Strategy | Killed | PnL | Reason | Post-Mortem |
|----------|--------|-----|--------|-------------|
| near_expiry_momentum | 2026-03-24 | -$691 | 0.2x ratio, bracket trap | [Full report](../graveyard/near_expiry_momentum.md) |
| bracket_cone | 2026-03-24 | -$1,039 | Correlated bracket losses | [Full report](../graveyard/bracket_cone.md) |
