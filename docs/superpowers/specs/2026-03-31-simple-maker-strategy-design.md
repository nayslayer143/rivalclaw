# Simple Maker Strategy — Design Spec

**Date:** 2026-03-31
**Status:** Approved
**Author:** RivalClaw + Jordan

## Summary

Upgrade from taker (buy at market, immediate fill) to maker (post limit orders at better prices, wait for fills). Same NO-direction fair_value strategy — only the execution changes.

## Motivation

Current taker approach: pay the ask price + 7% fee on every trade. Maker approach: post below the ask, save ~10-16% per contract, potentially zero maker fees. At our volume (10+ trades/hour), even $0.05 savings per contract compounds significantly.

## Design

### Config (env vars)

```
RIVALCLAW_MAKER_ENABLED=1              # Toggle maker mode (0 = taker fallback)
RIVALCLAW_MAKER_OFFSET_PCT=0.10        # Post 10% below brain's entry price
RIVALCLAW_MAKER_PATIENCE_SEC=120       # Wait 2 min for fill before cancel
RIVALCLAW_MAKER_MAX_RESTING=5          # Max resting orders at any time
RIVALCLAW_MAKER_MIN_FILL_RATE=0.30     # Auto-fallback to taker if fill rate < 30%
```

### Execution Flow (maker mode)

1. Brain generates signal: "buy NO on KXBTC-xxx at entry_price=$0.50"
2. Maker offset applied: $0.50 × (1 - 0.10) = $0.45
3. Limit order posted at NO $0.45 (YES price for API = 1 - $0.45 = $0.55)
4. Order rests on the book
5. Poll every 10 seconds for up to 120 seconds
6. If filled: record trade at the better price, log savings vs taker price
7. If unfilled after patience window: cancel order, no cost, try next cycle
8. If resting order count hits max (5): skip new maker orders until one fills/cancels

### Fallback to taker

- Track rolling fill rate over last 20 maker attempts
- If fill rate drops below 30%: auto-switch to taker for that series
- Reset to maker after 10 taker fills (re-test maker viability)
- Manual override: RIVALCLAW_MAKER_ENABLED=0 forces taker everywhere

### Code Changes

#### execution_router.py

In `route_trade()`, before building the order payload:

```python
MAKER_ENABLED = os.environ.get("RIVALCLAW_MAKER_ENABLED", "0") == "1"
MAKER_OFFSET = float(os.environ.get("RIVALCLAW_MAKER_OFFSET_PCT", "0.10"))

if MAKER_ENABLED and _should_use_maker(ticker):
    maker_entry = decision.entry_price * (1 - MAKER_OFFSET)
    decision.entry_price = max(maker_entry, MIN_ENTRY_PRICE)
    order_mode = "maker"
else:
    order_mode = "taker"
```

Add `_should_use_maker(ticker)` function that checks:
- Maker enabled globally
- Series fill rate > min threshold
- Resting order count < max

#### kalshi_executor.py

Add `poll_or_cancel()` function:

```python
def poll_or_cancel(order_id, patience_sec=120, poll_interval=10):
    """Poll a resting order, cancel if not filled within patience window."""
    deadline = time.time() + patience_sec
    while time.time() < deadline:
        status = poll_order_status(order_id)
        if status.get("status") in ("executed", "cancelled"):
            return status
        time.sleep(poll_interval)
    cancel_order(order_id)
    return {"status": "cancelled_timeout"}
```

#### Anti-stacking adjustment

The `_has_any_live_order_for_ticker()` check currently blocks new orders on tickers with existing filled/resting orders. For maker mode:
- Allow ONE resting order per ticker (the current maker quote)
- Cancel old resting order before posting new one
- Filled orders still block (no stacking of executed positions)

Change the check to:
```python
if order_mode == "maker":
    # Cancel any existing resting order on this ticker first
    _cancel_resting_for_ticker(ticker)
    # Then only block if there's a filled (not resting) order
    block = _has_filled_order_for_ticker(ticker)
else:
    block = _has_any_live_order_for_ticker(ticker)
```

#### DB tracking

Add columns to `live_orders` or a new `maker_stats` table:
- `order_mode`: "maker" or "taker"
- `brain_price`: original price from brain (before maker offset)
- `maker_savings`: brain_price - fill_price (if filled)
- `fill_time_sec`: time from submission to fill (for patience tuning)

Rolling fill rate query:
```sql
SELECT COUNT(CASE WHEN status='filled' THEN 1 END) * 1.0 / COUNT(*)
FROM live_orders WHERE order_mode='maker'
ORDER BY rowid DESC LIMIT 20
```

### What does NOT change

- trading_brain.py — same signals, fair values, strategies
- risk_engine.py — same position limits, exposure caps
- Direction filter — still NO-only + DOGE reversal
- Entry price bounds — still $0.08-$0.60
- Kill switch, process lock, all safety systems
- Paper wallet flow — paper trades still execute at brain price (instant)

### Risk controls

1. Max 5 resting orders at any time (capital preservation)
2. Auto-cancel after patience window (no orphaned orders)
3. Auto-fallback to taker if fill rate < 30% per series
4. Same kill switch, exposure limits, anti-stacking (for filled orders)
5. Maker mode starts disabled (MAKER_ENABLED=0) — opt-in

### Expected improvement

| Metric | Taker | Maker (50% fill rate) |
|--------|-------|----------------------|
| Entry price | Market ask | 10% better |
| Fee | 7% taker | ~0% maker |
| Trades/hour | 10 | ~5 (unfilled cancelled) |
| Savings/trade | $0 | ~$0.085 |
| Net PnL impact | Baseline | +8-15% improvement |

### Paper testing plan

1. Run maker mode on paper for 50 trades
2. Track: fill rate, avg savings vs brain price, time to fill
3. Compare maker PnL vs baseline taker PnL
4. Tune MAKER_OFFSET_PCT based on fill rate data
5. If maker PnL > taker PnL over 50 trades, graduate to live

### Implementation order

1. Add env vars and config
2. Add `poll_or_cancel()` to kalshi_executor.py
3. Add maker pricing logic to execution_router.py
4. Add `_cancel_resting_for_ticker()` and adjust anti-stacking
5. Add DB tracking columns
6. Add fill rate monitoring and auto-fallback
7. Paper test for 50 trades
8. Review results, tune offset, go live
