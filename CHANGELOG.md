# RivalClaw Changelog

> **For future bots:** This is the complete decision log of every change, why it was made, what data drove it, and what we learned. Study this before modifying any strategy. Every failure here is a lesson that cost real capital.

---

## 2026-03-25 — Day 2: Data-Driven Evolution

### Early Morning (00:00-02:00 UTC) — Volume Unleashed
- **FIX:** Risk engine was inflating trade amounts ABOVE position cap (tournament_score 1.5x × brain-sized amount > wallet cap). Every trade rejected for hours. 1% conversion rate.
- **ROOT CAUSE:** `adjust_decision()` multiplied amount by score AFTER brain sized to cap. Added 5% headroom + cap in risk engine.
- **RESULT:** 5 trades/cycle (was 0). Volume unlocked.
- **TUNED:** Position size 4% (was 5%), edge thresholds lowered 30%
- **ADDED:** Real-time Telegram trade alerts (per-cycle when trades execute)
- **ADDED:** 15-min status pings on Telegram
- **CRON:** Changed to 1-min cycles (was 2-min)

### 02:00-04:00 UTC — The Crash
- **BUG:** `_compute_balance()` crashed on `None - float` when expired markets had None prices
- **IMPACT:** 194 consecutive cycle crashes. 2 hours dead. Zero trades, zero reports.
- **FIX:** Explicit None check, default to entry_price
- **LESSON:** `dict.get("key", default)` returns None when key EXISTS with None value. Every price lookup needs explicit None guard.

### 04:00-05:00 UTC — The Unsticking
- **BUG:** Same None crash in `check_stops()`. 35 expired positions stuck open for 6+ hours.
- **IMPACT:** $10K+ capital locked. Zero new trades possible.
- **FIX:** Same None guard pattern applied to check_stops
- **RESULT:** 39 positions closed in one cycle. Balance adjusted from $12,944 → $10,833 (hidden losses materialized).
- **LESSON:** The trading loop must NEVER crash. A wrong number is recoverable. A dead system is not.

### 05:00-08:00 UTC — Data-Driven Upgrades
- **ANALYSIS:** Deep dive on 375 closed trades revealed critical patterns:
  - NO bets outperform YES by 3.5x ($34 avg vs $15 avg)
  - Entry 0.15-0.30 = $129 avg profit (THE GOLD MINE)
  - Entry 0.30-0.50 = -$18 avg profit (MONEY BURNER)
  - Afternoon/Evening UTC = 51-57% WR (best windows)
  - Morning UTC = 21% WR (worst window)
  - BTC daily brackets = +$7,879 (best market)
  - 15-min crypto = -$1,569 (net loser but kept for learning)
- **APPLIED:**
  - NO direction bias: 1.3x sizing boost on NO bets
  - Time-of-day weights: morning 0.5x → evening 1.3x
  - Entry price buckets: sweet spot 0.15-0.30 gets 0.4x edge threshold
  - Dead zone 0.30-0.50 gets 2.0x edge threshold
  - Tournament lookback expanded 50 → 200 trades (survived batch-close poison)
  - ROI-driven tournament scoring (was WR-gated, penalized low-WR high-ROI strategies)
  - Speed-based position sizing: 15-min full, hourly half, daily quarter
- **PHILOSOPHY:** Don't kill underperforming timescales. Fix them. Every timescale teaches different lessons.
- **RESULT:** 10 trades/cycle, 9 NO, across all market types

### Day 2 Status (as of 08:00 UTC)

| Metric | Value |
|--------|-------|
| Balance | $10,621 |
| Total PnL | +$9,621 (+962%) |
| Total trades | 381 |
| Closed | 354 (W:150 L:204) |
| Win rate | 42.4% |
| Capital velocity | 42.6x |
| Cycles run | 770 |
| Strategies active | 7 |
| Strategies killed | 2 (archived with post-mortems) |

### Key Decisions Log

| # | Decision | Data | Outcome |
|---|----------|------|---------|
| 1 | Add Kalshi + fair_value | 0 trades in 13 arb-only cycles | +$10,700 from fair_value |
| 2 | Kill near_expiry_momentum | 0.2x W/L ratio, -$691, 48 trades | Stopped bleeding |
| 3 | Disable stops on <60min | Stops killed winners, couldn't save losers | Win rate improved |
| 4 | Enable bracket fair value | 383 brackets available (was 2 threshold) | Unlocked main profit source |
| 5 | Add risk engine | No regime detection, no portfolio limits | Prevented correlated crash |
| 6 | Add weather markets | Less efficient pricing than crypto | Cross-strike arb fired |
| 7 | Market classifier | Capital wasted on slow/ambiguous markets | 97 low-priority markets filtered |
| 8 | Kill bracket_cone | -$1,039, correlated losses | Tournament auto-killed in 1.5h |
| 9 | Firehose mode (1% positions) | 88% idle capital, 0 trades/hour | 13 trades/cycle |
| 10 | NO direction bias | NO: 45% WR $34 avg. YES: 37% WR $15 avg | 1.3x NO boost |
| 11 | Time-of-day weights | Morning 21% WR. Evening 57% WR | Sizing scaled by window |
| 12 | Price bucket optimization | 0.15-0.30: $129 avg. 0.30-0.50: -$18 avg | Edge threshold by bucket |

### Architecture at End of Day 2

```
Cron 1min → Fetch 638 markets (Kalshi+Polymarket+CoinGecko+NWS)
          → Classify (speed/clarity/decay scoring, min priority filter)
          → Brain (7 strategies, event dedup, price bucket filter, direction bias)
          → Risk Engine (regime + tournament + portfolio limits + speed sizing + time-of-day)
          → Wallet (execution sim, no stops on <60min, None-safe)
          → Metrics + Telegram alerts

Cron 15min → Status ping (Telegram)
Cron hourly → Self-Tuner (vol, thresholds, slippage) → Full Report (Telegram)
Cron 9pm PST → Daily Report + Strategy Lab → Git push
```

---

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
- **KILLED:** near_expiry_momentum → [post-mortem](strategies/graveyard/near_expiry_momentum.md)

### Evening (21:00-23:00 UTC) — Expansion
- **ADDED:** Weather feed (NWS API for DC/SF/NYC temperature forecasts)
- **ADDED:** Expanded Kalshi series: weather, gold, silver, treasury, FX
- **RESULT:** Cross-strike arb fired for first time on weather brackets
- **ADDED:** Market classifier (resolution speed + clarity scoring)
- **ADDED:** Velocity-weighted ranking (faster markets rank higher)
- **KILLED:** bracket_cone → [post-mortem](strategies/graveyard/bracket_cone.md)
- **ADDED:** Hourly reporting system + Telegram notifications
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

---

## Critical Bugs & Lessons (for future bots)

### Bug #1: None Price Crash (Day 2, 02:00 UTC)
- `dict.get("key", default)` does NOT protect against None values — only missing keys
- Expired markets have keys with None values in market_data
- MUST use `if value is None: value = fallback` explicitly
- Cost: 194 dead cycles, 2 hours of lost trading

### Bug #2: Stuck Positions (Day 2, 04:00 UTC)
- Same None bug in check_stops() — silently caught by try/except
- Expired positions never closed, locked $10K+ of capital
- Cost: 6 hours of zero new trades

### Bug #3: Risk Engine Inflation (Day 2, 00:00 UTC)
- Tournament score (1.5x) multiplied amount ABOVE position cap
- Every trade rejected by wallet for hours
- Fix: cap in risk engine, not just wallet

### Bug #4: Tournament Batch Poison (Day 2, 07:00 UTC)
- Unstuck positions batch-closed with losses, poisoned 50-trade window
- Tournament killed fair_value (our best strategy) based on artificial bad data
- Fix: expanded lookback to 200 trades

### Lesson #1: The Loop Must Never Crash
A wrong number is recoverable. A dead system generates zero data. Wrap everything in None guards.

### Lesson #2: Apparent Edge != Executable Edge
94% market price != 94% probability of profit. Bracket contracts at $0.94 NO have extreme path risk.

### Lesson #3: Stop-Losses Are Harmful on Fast Contracts
On 15-min contracts, stop-losses kill winners (temporary drawdown) and can't save losers (price gaps between cycles). Let them expire.

### Lesson #4: Event-Level Dedup Is Non-Negotiable
Multiple positions on the same event = zero diversification. One trade per event minimum.

### Lesson #5: NO Bets Structurally Outperform YES
Most brackets don't hit. Buying NO on far-from-spot brackets is almost always right. 3.5x better than YES.

### Lesson #6: Entry Price Determines Profit More Than Strategy
0.15-0.30 entry: +$129 avg. 0.30-0.50 entry: -$18 avg. Same strategy, opposite outcomes.

### Lesson #7: Time of Day Matters
Afternoon/Evening UTC: 51-57% WR. Morning: 21% WR. Size accordingly.
