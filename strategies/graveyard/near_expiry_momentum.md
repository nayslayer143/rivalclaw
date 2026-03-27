# KILLED: near_expiry_momentum

**Status:** KILLED — 2026-03-24
**Lifetime:** 2026-03-24 08:34 to 2026-03-24 21:30 UTC (~13 hours)
**Killed by:** Manual review after Day 1 performance analysis

## What It Did

Bet on price continuation when a market was strongly directional (YES >= 0.78 or <= 0.22) and near expiry (< 48 hours). The thesis was: if a market is already pricing YES at 85% with 6 hours left, it will likely resolve YES.

## Performance

| Metric | Value |
|--------|-------|
| Total trades | 48 |
| Wins | 14 (29%) |
| Losses | 34 |
| Total PnL | -$690.51 |
| Avg win | $4.38 |
| Avg loss | -$22.11 |
| W/L ratio | 0.2x |
| Best trade | +$11.64 |
| Worst trade | -$68.89 |

## Why It Failed

### 1. Bracket contracts were the primary kill vector
The strategy treated Kalshi bracket contracts ("BTC between $70,250-$70,350") the same as threshold contracts. Bracket NO at $0.94 looks like a high-confidence bet (94% chance of winning), but the underlying price only needs to approach the bracket range to trigger massive drawdowns. A $500 BTC move can swing a bracket from $0.06 to $0.30, triggering the -20% stop-loss and crystallizing a loss on a position that would have expired profitably.

### 2. Stop-loss was counterproductive
On fast-resolving contracts (<60 min), stop-losses killed winners more often than they saved losers. The price could gap from -5% to -80% between 2-minute cycles, so the stop didn't limit loss to -20% — it closed at whatever the gap price was. Meanwhile, temporary drawdowns that would have recovered got stopped out.

Evidence: expired trades (no stop-loss triggered) averaged better PnL than closed_loss trades.

### 3. Edge was too thin (0.6%)
The strategy's typical edge was 0.006 (0.6%) after fees. This is far too thin to survive:
- Kalshi taker fee: ~7% of min(price, 1-price)
- Execution sim slippage: 50-60bps
- Random fill rate variation: 80-100%

A 0.6% edge needs >95% win rate to be profitable. The strategy delivered 29%.

### 4. Zero diversification
Without event-level dedup, the strategy opened 5-10 positions on different brackets of the SAME BTC event. This concentrated risk in one outcome, turning what looked like 10 independent bets into one large bet.

## What We Learned

1. **Apparent edge != executable edge.** 94% market price != 94% probability of profit.
2. **Stop-losses are harmful on fast contracts.** Let positions ride to expiry.
3. **Bracket contracts need different treatment** than threshold contracts.
4. **Event-level dedup is essential** to prevent correlated position clustering.
5. **Thin edge (<1%) doesn't survive friction** in prediction markets.

## Fixes That Were Extracted

These fixes were applied to the broader system:
- Stop-losses disabled for positions expiring < 60 minutes
- Event-level dedup (one trade per event_ticker)
- Bracket contracts excluded from simple momentum strategies
- Bracket contracts handled via fair_value_directional with proper math instead

## Could It Be Revived?

Possibly, with major changes:
- Only on threshold contracts (never brackets)
- Only on Polymarket (not Kalshi — lower fees)
- Higher minimum price threshold (0.90+ instead of 0.78)
- No stop-loss, hold to expiry only
- Minimum edge requirement of 3%+ instead of 0.5%

Not worth reviving while fair_value_directional is generating +$9,000 on the same markets.
