# Strategy Reference

All 28 strategies in `trading_brain.py`, grouped by status and importance.
Line numbers are approximate and may shift as code evolves.

---

## Global Filters

Applied after all strategies produce signals, before execution:

| Filter | Rule | Data Source |
|--------|------|-------------|
| Signal reversal | Flip direction for series in `REVERSE_SERIES` | KXDOGE15M: 15% WR on NO -> 85% reversed |
| YES block | Drop all YES trades (exempt: reversed series) | 15% WR, -$44 net over 88 trades |
| Entry bounds | $0.08 <= entry <= $0.60 (reversed: up to $0.85) | NO sweet spot $0.10-$0.30: 60.8% WR, $225 PnL |
| Confidence floor | Drop if confidence < 0.60 | Losers avg 0.52, winners avg 0.79 |
| Dead zone | $0.30-$0.45 entry requires confidence >= 0.75 | 41-47% WR zone, poor avg PnL |
| Velocity sort | Rank by confidence * velocity_boost(priority) | Faster-resolving markets rank higher |

## Kelly Multipliers

| Category | Multiplier | Strategies |
|----------|------------|------------|
| Proven | 1.0x (`KELLY_FRACTION_PROVEN`) | arbitrage, fair_value_directional, time_decay |
| New | 0.25x (`KELLY_FRACTION_NEW`) | Everything else |
| NO boost | 1.3x | Applied to all NO-direction trades |

## Strategy Priority Order in `analyze()`

Event-level (run on bracket groups first):
`cross_strike_arb` -> `bracket_cone` -> `bracket_neighbor` -> `pairs_trade` -> `election_field_arb`

Per-market cascade (first match wins):
`arbitrage` -> `fair_value_directional` -> `spot_momentum` -> `vol_skew` -> `time_decay` ->
`mean_reversion` -> `expiry_acceleration` -> `closing_convergence` -> `correlation_echo` ->
`polymarket_convergence` -> `liquidity_fade` -> `volume_confirmed` -> `multi_timeframe` ->
`vol_regime` -> `correlation_cascade` -> `forecast_delta` -> `expiry_convergence` ->
`fade_public` -> `vol_straddle` -> `price_lag_arb` -> `bid_gap_arb`

Post-cascade: hedge engine pairs primary trades with hedge legs (DISABLED).

---

## Core Strategies

These are the proven money-makers.

### 1. fair_value_directional

- **Status**: ENABLED
- **Function**: `_check_fair_value()` (line ~476)
- **Direction**: Both (but YES filtered globally, so effectively NO)
- **Kelly**: Proven (1.0x)

Computes Black-Scholes fair value for Kalshi threshold and bracket contracts using
real-time spot prices and realized vol. Trades when market price diverges from fair value
by more than the fee-adjusted edge threshold. Supports crypto, index, and weather underlyings.

**Edge formula**: `edge = fair_value - (market_price + kalshi_fee)`. For NO:
`edge = (1 - fair_value) - (no_price + fee)`.

**Key thresholds**:
- `MIN_FAIR_VALUE_EDGE` = 0.04 (base, adjusted by bucket multiplier)
- Sweet bucket (0.15-0.30): 0.4x threshold
- Dead bucket (0.30-0.70): 2.0x threshold
- Expiry: 2-1440 minutes
- Price bounds: 0.02-0.98 (YES), capped at `MAX_ENTRY_PRICE`

**Performance**: Primary revenue driver. ~50% WR, 2.7x win/loss ratio.

### 2. expiry_acceleration

- **Status**: ENABLED
- **Function**: `_check_expiry_acceleration()` (line ~1184)
- **Direction**: Both
- **Kelly**: New (0.25x)

Amplified fair_value in the last 5 minutes before expiry. Near expiry the vol term
collapses, making the fair value model extremely accurate. Trades more aggressively
with a lower edge threshold (0.008 vs 0.04).

**Edge formula**: Same as fair_value but with `min_edge = 0.008`.

**Key thresholds**:
- Expiry window: 0.5-5 minutes
- Confidence cap: 0.93

### 3. arbitrage

- **Status**: ENABLED
- **Function**: `_check_arbitrage()` (line ~335)
- **Direction**: Both (buys the cheaper side)
- **Kelly**: Proven (1.0x)

Cross-outcome arbitrage: when YES + NO + fees < 1.0, guaranteed profit exists.
Buys the cheaper side. Works on both Polymarket and Kalshi.

**Edge formula**: `edge = 1.0 - (yes_price + no_price + fee_yes + fee_no)`

**Key thresholds**:
- `MIN_EDGE` = 0.005

### 4. time_decay

- **Status**: ENABLED
- **Function**: `_check_time_decay()` (line ~910)
- **Direction**: Both
- **Kelly**: Proven (1.0x)

Very near expiry (<10 min), when spot is approximately equal to strike, the contract should
be near 0.50. Buys whichever side offers positive EV. Exploits the fact that most
15-min windows are boring (price stays flat).

**Edge formula**: `ev = fair_value_side - (entry_price + fee)`

**Key thresholds**:
- `MIN_DECAY_EDGE` = 0.03
- Expiry window: 1-10 minutes
- Threshold contracts only (no brackets)

---

## Secondary Strategies

Active but with new/unproven Kelly (0.25x).

### 5. vol_skew

- **Status**: ENABLED
- **Function**: `_check_vol_skew()` (line ~980)
- **Direction**: Both
- **Kelly**: New (0.25x)

When realized volatility exceeds implied volatility by 20%+, OTM contracts are
underpriced. Reverse-engineers implied vol via binary search, then computes fair value
at realized vol to find the edge.

**Edge formula**: `edge = fair_at_realized_vol - (price + fee)`

**Key thresholds**:
- `MIN_VOL_SKEW_EDGE` = 0.05
- `vol_ratio` >= 1.2 (realized / implied)
- Expiry: 5-60 minutes

### 6. mean_reversion

- **Status**: ENABLED
- **Function**: `_check_mean_reversion()` (line ~835)
- **Direction**: Both
- **Kelly**: New (0.25x)

When fair value is near 0.50 (coin-flip zone) but market disagrees, bets against
the crowd. Only fires on fast Kalshi contracts (15-min crypto). The crowd
consistently overprices one side in coin-flip situations.

**Edge formula**: `edge = abs(yes_price - fair_value) - fee`

**Key thresholds**:
- `MIN_REVERSION_EDGE` = 0.06
- Fair value must be 0.40-0.60
- Expiry: 1-30 minutes

### 7. correlation_echo

- **Status**: ENABLED
- **Function**: `_check_correlation_echo()` (line ~1255)
- **Direction**: NO only
- **Kelly**: New (0.25x)

BTC and ETH are ~85% correlated. When one asset generates a strong signal,
echoes a weaker version to the other asset's bracket contracts.

**Edge formula**: Same fair value model, but `edge > 0.03` threshold.

**Key thresholds**:
- Requires active signal on correlated asset
- Bracket contracts only
- Higher edge threshold than primary (0.03)

### 8. correlation_cascade

- **Status**: ENABLED
- **Function**: `_check_correlation_cascade()` (line ~1817)
- **Direction**: Both
- **Kelly**: New (0.25x)

When BTC spot moves >0.3%, ETH contracts haven't repriced yet. Trades ETH based on
BTC momentum with a ~15-minute lag assumption.

**Edge formula**: Fair value model on ETH, triggered by BTC momentum.

**Key thresholds**:
- BTC move > 0.3%
- ETH markets only
- Expiry: 2-30 minutes
- min_edge = 0.015

### 9. multi_timeframe

- **Status**: ENABLED
- **Function**: `_check_multi_timeframe()` (line ~1595)
- **Direction**: Both
- **Kelly**: New (0.25x)

When 15-min and daily contracts on the same underlying agree on direction,
trades with boosted confidence (+0.05) and halved edge threshold (0.5x `MIN_FAIR_VALUE_EDGE`).

**Edge formula**: Fair value model with 0.5x threshold.

**Key thresholds**:
- Requires consensus across timeframes
- Confidence boosted by +0.05

### 10. liquidity_fade

- **Status**: ENABLED
- **Function**: `_check_liquidity_fade()` (line ~1386)
- **Direction**: Both
- **Kelly**: New (0.25x)

Illiquid brackets (wide bid-ask spread >= 3c) are more likely mispriced because
the market maker hasn't bothered to update. Trades when fair value diverges from
the midpoint.

**Edge formula**: `edge = fair_value - (ask_price + fee)` for YES; analogous for NO.

**Key thresholds**:
- Spread >= $0.03
- YES entry <= $0.35
- Edge > $0.02

### 11. volume_confirmed

- **Status**: ENABLED
- **Function**: `_check_volume_confirmed()` (line ~1458)
- **Direction**: NO only
- **Kelly**: New (0.25x)

Fair value on brackets that have real volume (>= 50 contracts). High-volume brackets
have better price discovery. Low-volume brackets are where losses concentrate
(-$546 on illiquid 15-min).

**Edge formula**: Fair value model, bucket-adjusted threshold.

**Key thresholds**:
- `MIN_VOLUME_THRESHOLD` = 50
- NO only, entry $0.10-$0.35

### 12. polymarket_convergence

- **Status**: ENABLED
- **Function**: `_check_polymarket_convergence()` (line ~1326)
- **Direction**: Both
- **Kelly**: New (0.25x)

Buys the dominant side on Polymarket markets with extreme prices. "Will Charlotte
Hornets win NBA Finals?" at YES=0.01 -> buy NO. Longer-dated markets get
heavy discount (0.3x edge for >1 week).

**Edge formula**: `edge = minority_price * probability_factor - fee`

**Key thresholds**:
- YES >= 0.85 or <= 0.15: strong signal
- YES >= 0.75 or <= 0.25 with < 1 week: moderate signal

### 13. election_field_arb

- **Status**: ENABLED
- **Function**: `_check_election_field()` (line ~1696)
- **Direction**: NO only
- **Kelly**: New (0.25x)

Multi-candidate elections: only one can win. Buy NO on every underdog (YES < 0.15).
12 candidates, buy NO on 11 -- at most 1 loses.

**Edge formula**: `edge = yes_price * 0.9 - fee`

**Key thresholds**:
- Minimum 4 candidates per event
- YES < 0.15 (underdog)
- Max 15 trades per cycle

### 14. fade_public

- **Status**: ENABLED
- **Function**: `_check_fade_public()` (line ~2110)
- **Direction**: NO only
- **Kelly**: New (0.25x)

When a high-volume market is stuck at 40-60% (indecision zone), bet NO.
When neither side is winning, the status quo (event doesn't happen) tends to prevail.

**Edge formula**: `edge = abs(0.50 - yes_price) + 0.02 - fee`

**Key thresholds**:
- `MIN_FADE_VOLUME` = 5000
- YES in 0.40-0.60 range

### 15. vol_straddle

- **Status**: ENABLED
- **Function**: `_check_vol_straddle()` (line ~2160)
- **Direction**: Both (buys cheaper leg)
- **Kelly**: New (0.25x)

After BTC spot moves >0.5%, buy the cheaper leg on 15-min altcoin contracts.
Vol clusters: a big BTC move means the next 15-min period likely sees another big move.

**Edge formula**: `edge = btc_move * 2 - fee`

**Key thresholds**:
- BTC move > 0.5%
- 15-min contracts only
- Cheaper leg must be < $0.40
- Expiry: 5-20 minutes

### 16. price_lag_arb

- **Status**: ENABLED
- **Function**: `_check_price_lag_arb()` (line ~2352)
- **Direction**: Both
- **Kelly**: New (0.25x)

Vol-distance dislocation model (not Black-Scholes). Detects when crypto spot has moved
but the Kalshi contract hasn't repriced. Uses implied probability from distance-to-strike
divided by a vol factor, multiplied by a time-decay amplifier.

**Edge formula**: `decayed_edge = raw_dislocation * decay_factor - latency_penalty`

**Key thresholds**:
- `PRICE_LAG_MIN_EDGE` = 0.05
- `PRICE_LAG_LATENCY_PENALTY` = 0.005
- Max horizon: 180 days

---

## Disabled Strategies

Each disabled for specific data-driven reasons.

### 17. cross_strike_arb

- **Status**: DISABLED (dead code -- always produces YES, blocked by YES filter)
- **Function**: `_check_cross_strike_arb()` (line ~703)
- **Direction**: YES only

Bracket sum deviation from 1.0 -- if sum(yes_prices) + fees < 1.0, buying YES on all
brackets guarantees profit. Always outputs YES direction, which gets blocked by the
global YES filter.

### 18. bracket_cone

- **Status**: ENABLED (but effectively dead -- always produces YES)
- **Function**: `_check_bracket_cone()` (line ~751)
- **Direction**: YES only

Buy the 3 brackets closest to spot. Always YES direction. Blocked by YES filter
in practice. Would need to be reworked for NO direction to be useful.

### 19. bracket_neighbor

- **Status**: DISABLED (0% WR)
- **Function**: `_check_bracket_neighbor()` (line ~1137)
- **Direction**: YES only
- **Reason**: 0% win rate. Adjacent bracket smoothness assumption doesn't hold.

### 20. pairs_trade

- **Status**: DISABLED (13% WR)
- **Function**: `_check_pairs_trade()` (line ~1897)
- **Direction**: YES only
- **Reason**: 13% win rate. Relative bracket mispricing reverts too slowly.

### 21. spot_momentum

- **Status**: DISABLED (0% WR)
- **Function**: `_check_spot_momentum()` (line ~604)
- **Direction**: Both
- **Reason**: 0% win rate. Momentum signal too noisy on 15-min timeframe.

### 22. closing_convergence

- **Status**: DISABLED (-$36 net)
- **Function**: `_check_closing_convergence()` (line ~1078)
- **Direction**: Both
- **Reason**: -$36 net PnL. Entry at 0.78-0.87 has 4:1 loss ratio, requires >80% WR
  but achieves only ~69%.

### 23. forecast_delta

- **Status**: DISABLED
- **Function**: `_check_forecast_delta()` (line ~1968)
- **Direction**: Both
- **Reason**: NWS forecast-to-market lag not consistently exploitable. Vol scaling issues.

### 24. wipeout_reversal

- **Status**: DISABLED (small sample)
- **Function**: `_check_wipeout_reversal()` (line ~1523)
- **Direction**: Both
- **Reason**: Hardcoded disabled in cascade (commented out). Insufficient sample size.

### 25. vol_regime

- **Status**: DISABLED (always produces YES, blocked by filter)
- **Function**: `_check_vol_regime()` (line ~1748)
- **Direction**: YES only
- **Reason**: Only buys YES on OTM brackets. Dead under YES filter.

### 26. expiry_convergence

- **Status**: DISABLED (NO entry > $0.60)
- **Function**: `_check_expiry_convergence()` (line ~2048)
- **Direction**: Both
- **Reason**: NO entries land above $0.60, blocked by `MAX_ENTRY_PRICE`. YES entries
  blocked by YES filter.

### 27. bid_gap_arb

- **Status**: DISABLED (17% WR)
- **Function**: `_check_bid_gap_arb()` (line ~2447)
- **Direction**: Both
- **Reason**: 0/5 win rate on live trades, -$60. Buys wrong side consistently.
  Not a true arb -- bid gap doesn't imply fill at ask.

### 28. hedge

- **Status**: DISABLED (0% WR)
- **Function**: `_find_hedge()` (line ~2530)
- **Direction**: YES only (insurance legs)
- **Reason**: 0% win rate. Insurance costs eat into already-thin edge.
  Hedge legs are always YES at low confidence (0.50), which gets killed by
  the confidence floor (0.60).

---

## Known Issues

- **YES filter dependency**: 8 strategies are effectively dead because they only produce
  YES signals (cross_strike_arb, bracket_cone, bracket_neighbor, pairs_trade, vol_regime,
  expiry_convergence, hedge, plus wipeout_reversal sometimes). If YES performance improves,
  these could be re-enabled.

- **KXXRPD removed**: 0W/4L, reversal blocked by price filter, model misprices XRP vol.

- **Weather vol scaling**: `_check_fair_value` converts forecast error to annualized vol
  via a simplified approach. Could be more precise.

- **Realized vol lookback**: Uses last 200 spot snapshots regardless of timeframe.
  On quiet periods this spans hours; on busy periods just minutes.
