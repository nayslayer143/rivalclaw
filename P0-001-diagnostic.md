# P0-001 Diagnostic: RivalClaw Cycle Time Explosion

**Date:** 2026-03-27
**Instance:** RivalClaw (`~/rivalclaw/`)
**Severity:** P0 -- production cycles 5.6x over budget, cascading overlaps

---

## Executive Summary

RivalClaw cycles average 28 minutes against a 5-minute budget. The root cause is a **feedback loop between three compounding issues**: (1) no file lock on the cron runner, (2) open trade accumulation triggering O(n) HTTP resolution calls, and (3) per-trade balance recomputation doing full table scans over 12K+ rows. Together these turned a 3-second cycle into a 30-minute one, with 25 concurrent processes running right now.

---

## Root Causes (ranked by impact)

### 1. No file lock + 1-minute cron = unbounded process stacking (CRITICAL)

**Evidence:**
- Crontab: `* * * * *` (every 1 minute, not 5 minutes as budgeted)
- `run.py` has no flock/pidfile guard
- 25 concurrent `run.py --run` processes observed via `ps aux`
- Each process independently fetches markets, opens trades, and runs resolution

**Impact:** When a cycle takes >1 minute, the next cron invocation starts a parallel cycle. With 28-minute average cycles, up to 28 processes stack. Each one independently opens trades, inflating the open-trade count, which makes the next cycle even slower.

### 2. O(n) HTTP calls in trade resolution functions (PRIMARY BOTTLENECK)

**Evidence:**
- `_resolve_kalshi_trades()` (simulator.py:369-433) makes **one HTTP call per open Kalshi trade** to check resolution status. Currently 106 open Kalshi trades = 106 sequential HTTP calls with 30s timeout each.
- `_resolve_polymarket_trades()` (simulator.py:436-534) makes **one HTTP call per unique Polymarket market_id**. Currently 10 unique Polymarket market IDs.
- These run AFTER the timed `wallet_ms` phase but BEFORE `total_cycle_ms` is recorded.

**Timing breakdown (latest 100 cycles):**

| Phase | Avg time | % of cycle |
|-------|----------|------------|
| fetch_ms | 71s | 3.6% |
| analyze_ms | 1.6s | 0.1% |
| wallet_ms (trade execution) | 1,241s (20.7 min) | 63% |
| Unaccounted (stops + resolution) | 663s (11 min) | 33% |
| **total_cycle_ms** | **1,977s (33 min)** | **100%** |

The "unaccounted" 663s is almost entirely HTTP resolution calls on open trades.

### 3. Per-trade balance recomputation = full table scan over 12K rows (AMPLIFIER)

**Evidence:**
- `paper_wallet.execute_trade()` calls `get_state()` which runs `SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status != 'open'` -- a full scan of 12,227 rows.
- This happens once per trade decision (17.4 trades/cycle average in latest period).
- Cost: 24 seconds per trade execution = 17.4 * 24 = 418s wallet_ms.
- In early cycles (100 trades in table): 0.3s wallet_ms total. Now (12K rows): 1,241s.

### 4. Open trade accumulation (CONSEQUENCE, not cause)

**Evidence -- open trades at each time bucket:**

| Bucket | Time | Open trades | wallet_ms (avg) |
|--------|------|-------------|-----------------|
| 1-24 | Mar 24 - Mar 26 10:23 | 0 | <1s |
| 25 | Mar 26 12:06 | 175 | 275s |
| 29 | Mar 26 19:57 | 259 | 521s |
| 30 | Mar 26 22:00 | 438 | 1,160s |
| Now | Mar 27 01:00 | 183 (after expirations) | ~1,241s |

Open trades are Polymarket positions on long-dated events (Guinea-Bissau elections, Ukraine geopolitics, etc.) that do not expire quickly. The `polymarket_convergence` strategy opens many small positions that accumulate because there is no position limit per strategy.

---

## Timeline of Degradation

| Phase | Cycle range | Avg cycle time | What happened |
|-------|------------|----------------|---------------|
| Healthy | 0-400 | 3.5-5.6s | Small table, no open positions |
| Slow fetch | 400-1600 | 12-58s | Market count grew (400->700), fetch slowed. Still functional. |
| Inflection | 1700-1800 | 99-122s | Kalshi resolution calls begin accumulating. wallet_ms jumps 40-80s. |
| Runaway | 2400+ | 290-1977s | Open trades > 175. Resolution + balance recomputation dominate. Processes stack. |

The change point was around cycle 1700 (2026-03-25T23:17), when long-dated Polymarket trades started accumulating faster than they expired.

---

## Evidence: DB Queries

### First 100 vs Last 100 cycles

```
first_100: avg_total=3.5s, avg_fetch=2.9s, avg_analyze=0.0s, avg_wallet=0.3s
last_100:  avg_total=1831s, avg_fetch=71s,  avg_analyze=1.9s, avg_wallet=1142s
```

wallet_ms increased **3,807x**. fetch_ms increased **24x**. analyze_ms stayed flat.

### Trade status distribution

```
open: 196 (still accumulating)
expired: 11,189 (Kalshi 15-min contracts cycle out)
closed_win: 395
closed_loss: 420
```

### Concurrent processes at time of investigation

```
$ ps aux | grep "rivalclaw.*run.py --run" | grep -v grep | wc -l
25
```

---

## Recommended Fixes (in priority order)

### Fix 1: Add flock to cron entry (IMMEDIATE -- 5 min fix)

Change the crontab entry from:
```
* * * * * cd /Users/nayslayer/rivalclaw && /Users/nayslayer/rivalclaw/venv/bin/python run.py --run >> rivalclaw.log 2>&1
```
To:
```
*/5 * * * * flock -n /tmp/rivalclaw-run.lock cd /Users/nayslayer/rivalclaw && /Users/nayslayer/rivalclaw/venv/bin/python run.py --run >> rivalclaw.log 2>&1
```

This (a) prevents concurrent runs, and (b) restores the intended 5-minute interval.

### Fix 2: Batch resolution HTTP calls (HIGH -- reduces O(n) to O(1))

Replace per-trade Kalshi resolution (`_resolve_kalshi_trades`) with a batch approach:
- Fetch all open Kalshi event tickers in one query
- Use Kalshi's batch events endpoint or filter by `status=settled` in a single list call
- Same for Polymarket: batch-fetch resolution status

This would reduce 106+ HTTP calls to 1-2 calls per venue.

### Fix 3: Cache balance computation (HIGH -- eliminates full table scans)

In `paper_wallet.execute_trade()`, `get_state()` is called per trade attempt, each time scanning 12K rows. Fix:
- Compute state ONCE at the start of the wallet phase in `simulator.run_loop()`
- Pass the state object to `execute_trade()` instead of recomputing
- Only update the cached balance after each successful trade (simple arithmetic)

### Fix 4: Add position limit per strategy (MEDIUM -- prevents reaccumulation)

The `polymarket_convergence` strategy has 90 open positions. Add a per-strategy cap (e.g., 20 max open positions per strategy) to prevent unbounded accumulation of long-dated positions.

### Fix 5: Close stale positions (CLEANUP -- one-time)

The 196 currently open positions are inflating cycle time. Run a one-time cleanup to expire positions older than their market end_date, or positions that have been open > 7 days with no resolution path.

---

## Risk Assessment

### If we do nothing:
- Cycles will continue to stack (25+ concurrent processes)
- Open trade count will grow further as new strategies open positions
- Kalshi API rate limits will worsen (already seeing 429 errors in logs)
- SQLite WAL contention from 25 concurrent writers risks database corruption
- Market data becomes increasingly stale (fetched 28 min before trade execution)
- All arb edge evaporates -- stale prices mean fake opportunities

### Fix urgency:
- **Fix 1 (flock):** Must deploy immediately. Prevents the cascading overlap that is the primary amplifier.
- **Fix 2 (batch resolution):** Should deploy within 24 hours. Reduces the core bottleneck from minutes to seconds.
- **Fix 3 (cache balance):** Should deploy within 48 hours. Eliminates the O(n) table scan amplifier.
- **Fix 4 + 5:** Can wait for next sprint. Prevents reoccurrence.

### Expected improvement:
With fixes 1-3 applied, estimated cycle time: **5-15 seconds** (matching the first 400 cycles when the table was small and no open trades existed). The system was healthy at that scale and these fixes restore those conditions.

---

*Diagnostic performed 2026-03-27. Data from rivalclaw.db (3,087 cycles, 12,227 trades). No code changes made.*
