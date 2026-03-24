# KILLED: bracket_cone

**Status:** KILLED (by tournament auto-score 0.0) — 2026-03-24
**Lifetime:** 2026-03-24 21:36 to 2026-03-24 23:00 UTC (~1.5 hours)
**Killed by:** Strategy tournament (ROI < -10% threshold)

## What It Did

Bought YES on the 2-3 bracket contracts closest to current spot price for each event. The thesis was: spreading bets across adjacent brackets widens the "hit zone" — crypto doesn't need to land in one exact $100 range, just within $300.

## Performance

| Metric | Value |
|--------|-------|
| Total trades | 9 |
| Wins | 1 (11%) |
| Losses | 8 |
| Total PnL | -$1,039.08 |
| Avg win | $155.63 |
| Avg loss | -$149.34 |
| W/L ratio | 1.0x |
| Best trade | +$155.63 |
| Worst trade | -$250.60 |

## Why It Failed

### 1. Correlated losses on the same event
Buying 3 adjacent brackets means when BTC moves AWAY from the cone, ALL 3 positions lose simultaneously. Instead of diversifying, it tripled the loss on bad calls. The 1 win (+$155) couldn't overcome 8 correlated losses.

### 2. Bypassed event-level dedup
The cone strategy intentionally opened multiple positions per event (that was its design — buy 2-3 brackets). This violated the lesson we learned from near_expiry_momentum: don't stack bets on correlated outcomes.

### 3. Used fair_value's math but weaker threshold
The cone used 50% of fair_value's edge threshold (MIN_FAIR_VALUE_EDGE * 0.5), accepting weaker signals under the assumption that diversification would hedge. It didn't — it just let in lower-quality trades.

### 4. Short lifetime, but signal was clear
Only 9 trades in 1.5 hours, but -$1,039 with 1.0x W/L ratio means the strategy had neutral edge but massive variance. It was essentially gambling with no reliable edge.

## What We Learned

1. **Adjacent brackets are NOT diversification.** They're correlated bets on the same underlying.
2. **Lowering edge thresholds "because diversification" is a trap.** The math doesn't change just because you spread it across nearby strikes.
3. **The tournament correctly identified and killed this** within 1.5 hours. The automated system works.
4. **One trade per event is the right constraint** — even when the strategy is explicitly designed to violate it.

## Fixes That Were Extracted

- Confirmed that event-level dedup is a hard constraint, not optional
- Validated that the strategy tournament can catch and kill fast-failing strategies automatically

## Could It Be Revived?

Only if restructured fundamentally:
- Use as a HEDGE for an existing fair_value trade, not a standalone strategy
- Buy 1 primary bracket (highest fair value) + 1 adjacent as insurance (smaller size)
- This is essentially what the hedge engine already does
- Redundant with existing hedge system — no need to revive as separate strategy
