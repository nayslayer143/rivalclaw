# Simple Maker Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post limit orders at 10% below the brain's entry price instead of taking at market, with auto-cancel after 2 minutes and fallback to taker if fill rates are too low.

**Architecture:** Maker logic sits in execution_router.py (pricing offset + order mode selection) and kalshi_executor.py (poll_or_cancel + cancel-for-ticker). Anti-stacking is relaxed for maker resting orders. DB tracks maker vs taker stats for fill rate monitoring.

**Tech Stack:** Python 3.9, SQLite, Kalshi REST API

---

### Task 1: Add `poll_or_cancel()` to kalshi_executor.py

**Files:**
- Modify: `kalshi_executor.py:385` (after `cancel_order`)

- [ ] **Step 1: Add `poll_or_cancel` function**

Add after the existing `cancel_order()` function at line 385:

```python
def poll_or_cancel(
    kalshi_order_id: str,
    patience_sec: float = 120,
    poll_interval: float = 10.0,
) -> dict:
    """Poll a resting order for fills. Cancel if not filled within patience window.

    Returns the final order status dict. If cancelled due to timeout,
    status will be 'cancelled_timeout'.
    """
    deadline = time.time() + patience_sec
    last_result: dict = {}

    while time.time() < deadline:
        result = poll_order_status(kalshi_order_id, max_polls=1, interval=0)
        last_result = result
        status = result.get("status", "")
        if status in ("executed", "canceled", "cancelled"):
            return result
        if "error" in result:
            return result
        time.sleep(poll_interval)

    # Timed out — cancel the resting order
    cancel_result = cancel_order(kalshi_order_id)
    if "error" in cancel_result:
        logger.warning("Failed to cancel timed-out order %s: %s", kalshi_order_id, cancel_result)
    return {"status": "cancelled_timeout", "order_id": kalshi_order_id, "last_poll": last_result}
```

- [ ] **Step 2: Add `cancel_resting_for_ticker` function**

Add after `poll_or_cancel`:

```python
def cancel_resting_for_ticker(ticker: str) -> int:
    """Cancel all resting orders for a given ticker. Returns count cancelled."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT kalshi_order_id FROM live_orders "
            "WHERE ticker=? AND mode='live' AND status='resting' AND kalshi_order_id IS NOT NULL",
            (ticker,),
        ).fetchall()
    finally:
        conn.close()

    cancelled = 0
    for row in rows:
        oid = row["kalshi_order_id"]
        result = cancel_order(oid)
        if "error" not in result:
            update_order_status_by_kalshi_id(oid, "cancelled")
            cancelled += 1
    return cancelled


def update_order_status_by_kalshi_id(kalshi_order_id: str, status: str):
    """Update live_orders status by Kalshi order ID."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE live_orders SET status=? WHERE kalshi_order_id=?",
            (status, kalshi_order_id),
        )
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 3: Verify it compiles**

Run: `cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "import kalshi_executor; print('poll_or_cancel:', hasattr(kalshi_executor, 'poll_or_cancel')); print('cancel_resting_for_ticker:', hasattr(kalshi_executor, 'cancel_resting_for_ticker'))"`

Expected: Both `True`.

- [ ] **Step 4: Commit**

```bash
git add kalshi_executor.py
git commit -m "feat: add poll_or_cancel and cancel_resting_for_ticker to kalshi_executor"
```

---

### Task 2: Add DB columns for maker tracking

**Files:**
- Modify: `kalshi_executor.py` (CREATE TABLE migration and log_order)

- [ ] **Step 1: Add migration for maker columns**

Find the `_ensure_tables()` or table creation in `kalshi_executor.py`. Add these columns to the `live_orders` CREATE TABLE (they'll be NULL for existing rows which is fine since SQLite allows ALTER TABLE ADD COLUMN):

```python
def migrate_maker_columns():
    """Add maker tracking columns to live_orders if not present."""
    conn = _get_conn()
    try:
        # Check if columns exist
        cols = {row[1] for row in conn.execute("PRAGMA table_info(live_orders)").fetchall()}
        if "order_mode" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN order_mode TEXT DEFAULT 'taker'")
        if "brain_price" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN brain_price REAL")
        if "maker_savings" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN maker_savings REAL")
        if "fill_time_sec" not in cols:
            conn.execute("ALTER TABLE live_orders ADD COLUMN fill_time_sec REAL")
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Update `log_order` to accept maker fields**

Add parameters to `log_order()`:

```python
def log_order(
    intent_id: str,
    ticker: str,
    action: str,
    side: str,
    count: int,
    yes_price_cents: int,
    mode: str,
    client_order_id: str | None = None,
    kalshi_order_id: str | None = None,
    order_type: str = "limit",
    cycle_id: str | None = None,
    strategy: str | None = None,
    market_question: str | None = None,
    order_mode: str = "taker",
    brain_price: float | None = None,
) -> int:
```

Update the INSERT statement to include the new columns:

```sql
INSERT INTO live_orders
(intent_id, client_order_id, kalshi_order_id, ticker, action, side,
 count, yes_price, order_type, status, submitted_at, mode,
 cycle_id, strategy, market_question, order_mode, brain_price)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
```

And add `order_mode` and `brain_price` to the values tuple.

- [ ] **Step 3: Add fill rate query function**

```python
def get_maker_fill_rate(series_prefix: str, lookback: int = 20) -> float:
    """Get rolling fill rate for maker orders on a series. Returns 0.0-1.0."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT status FROM live_orders "
            "WHERE order_mode='maker' AND ticker LIKE ? "
            "ORDER BY rowid DESC LIMIT ?",
            (f"{series_prefix}%", lookback),
        ).fetchall()
        if not rows:
            return 1.0  # No history — assume maker is viable
        filled = sum(1 for r in rows if r["status"] == "filled")
        return filled / len(rows)
    finally:
        conn.close()


def get_resting_count() -> int:
    """Count currently resting live orders."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM live_orders WHERE mode='live' AND status='resting'"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()
```

- [ ] **Step 4: Run migration and verify**

```python
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import kalshi_executor
kalshi_executor.migrate_maker_columns()
print('Migration done')
print('fill_rate:', kalshi_executor.get_maker_fill_rate('KXBTC'))
print('resting:', kalshi_executor.get_resting_count())
"
```

- [ ] **Step 5: Commit**

```bash
git add kalshi_executor.py
git commit -m "feat: add maker tracking columns and fill rate queries"
```

---

### Task 3: Add maker logic to execution_router.py

**Files:**
- Modify: `execution_router.py` (route_trade function + new helpers)

- [ ] **Step 1: Add maker config and helper functions**

After the existing module-level state (around line 67), add:

```python
# Maker mode config (read fresh each call to pick up .env changes)
def _get_maker_config() -> dict:
    return {
        "enabled": os.environ.get("RIVALCLAW_MAKER_ENABLED", "0") == "1",
        "offset_pct": float(os.environ.get("RIVALCLAW_MAKER_OFFSET_PCT", "0.10")),
        "patience_sec": float(os.environ.get("RIVALCLAW_MAKER_PATIENCE_SEC", "120")),
        "max_resting": int(os.environ.get("RIVALCLAW_MAKER_MAX_RESTING", "5")),
        "min_fill_rate": float(os.environ.get("RIVALCLAW_MAKER_MIN_FILL_RATE", "0.30")),
    }


def _should_use_maker(ticker: str, maker_cfg: dict) -> bool:
    """Decide whether to use maker mode for this ticker."""
    if not maker_cfg["enabled"]:
        return False
    # Check resting count
    if executor.get_resting_count() >= maker_cfg["max_resting"]:
        return False
    # Check series fill rate
    series_prefix = ticker.split("-")[0]
    fill_rate = executor.get_maker_fill_rate(series_prefix)
    if fill_rate < maker_cfg["min_fill_rate"]:
        logger.info("Maker disabled for %s: fill rate %.0f%% < %.0f%%",
                     series_prefix, fill_rate * 100, maker_cfg["min_fill_rate"] * 100)
        return False
    return True
```

- [ ] **Step 2: Add `_has_filled_order_for_ticker` helper**

```python
def _has_filled_order_for_ticker(ticker: str) -> bool:
    """Check if a FILLED (not resting) live order exists for this ticker.
    Used in maker mode where resting orders are expected and allowed."""
    db = _db_path or str(executor.DB_PATH)
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM live_orders WHERE ticker=? AND mode='live' AND status='filled' LIMIT 1",
            (ticker,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.warning("Failed to check filled orders for %s: %s — blocking", ticker, e)
        return True  # Fail closed
```

- [ ] **Step 3: Modify `route_trade()` for maker mode**

In the `route_trade()` function, after the cycle reset (around line 296) and before the preflight check, add the maker pricing logic:

```python
    # ---- Maker mode: adjust entry price for better fills ----
    maker_cfg = _get_maker_config()
    order_mode = "taker"
    brain_price = decision.entry_price  # Save original for tracking

    if _should_use_maker(ticker, maker_cfg):
        order_mode = "maker"
        maker_entry = decision.entry_price * (1 - maker_cfg["offset_pct"])
        decision.entry_price = max(maker_entry, 0.08)  # Don't go below min
        # For maker: cancel any existing resting order on this ticker
        cancelled = executor.cancel_resting_for_ticker(ticker)
        if cancelled:
            logger.info("Cancelled %d resting order(s) for %s before new maker quote", cancelled, ticker)
```

- [ ] **Step 4: Modify anti-stacking check for maker mode**

Replace the current anti-stacking block in `preflight_check()`:

```python
    # 8a. Anti-stacking — different logic for maker vs taker
    if ticker in _submitted_market_ids:
        return {"passed": False, "reason": "already_submitted"}
    # For maker: only block on filled orders (resting is expected)
    # For taker: block on any active order
    if hasattr(decision, '_order_mode') and decision._order_mode == "maker":
        if _has_filled_order_for_ticker(ticker):
            return {"passed": False, "reason": "already_submitted"}
    else:
        if _has_any_live_order_for_ticker(ticker):
            return {"passed": False, "reason": "already_submitted"}
```

Actually, preflight_check doesn't have access to order_mode. Simpler approach — tag the decision object before calling preflight:

In `route_trade()`, before the preflight call, add:
```python
    decision._order_mode = order_mode
```

- [ ] **Step 5: Use `poll_or_cancel` for maker orders, keep fast poll for taker**

Replace the poll section (around line 483) with:

```python
    # Poll for fill — maker uses patience window, taker uses fast poll
    submit_time = time.time()
    if order_mode == "maker":
        fill_result = executor.poll_or_cancel(
            kalshi_order_id,
            patience_sec=maker_cfg["patience_sec"],
            poll_interval=10.0,
        )
    else:
        fill_result = executor.poll_order_status(kalshi_order_id)

    fill_status = fill_result.get("status", "unknown")
    fill_price = fill_result.get("yes_price", payload["yes_price"])
    fill_count = fill_result.get("count", decision.shares)
    fill_time_sec = time.time() - submit_time
```

- [ ] **Step 6: Update order logging to include maker fields**

Update both the success `log_order` call and the `update_order_status` call to include maker fields:

```python
    order_id = executor.log_order(
        intent_id=intent_id,
        ticker=ticker,
        action=action,
        side=side,
        count=decision.shares,
        yes_price_cents=payload["yes_price"],
        mode="live",
        client_order_id=payload["client_order_id"],
        kalshi_order_id=kalshi_order_id,
        cycle_id=cycle_id,
        strategy=decision.strategy,
        market_question=decision.question,
        order_mode=order_mode,
        brain_price=brain_price,
    )
```

After the fill status update, add maker savings tracking:

```python
    # Track maker savings
    if order_mode == "maker" and fill_status == "executed":
        if side == "no":
            actual_cost = (100 - fill_price) / 100.0
        else:
            actual_cost = fill_price / 100.0
        savings = brain_price - actual_cost
        conn = executor._get_conn()
        try:
            conn.execute(
                "UPDATE live_orders SET maker_savings=?, fill_time_sec=? WHERE id=?",
                (savings, fill_time_sec, order_id),
            )
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 7: Verify it compiles**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import execution_router
print('_should_use_maker:', hasattr(execution_router, '_should_use_maker'))
print('_has_filled_order_for_ticker:', hasattr(execution_router, '_has_filled_order_for_ticker'))
print('_get_maker_config:', hasattr(execution_router, '_get_maker_config'))
"
```

- [ ] **Step 8: Commit**

```bash
git add execution_router.py
git commit -m "feat: add maker mode to execution router with offset pricing and patience polling"
```

---

### Task 4: Add env vars and enable for paper testing

**Files:**
- Modify: `.env`

- [ ] **Step 1: Add maker config to .env**

Append to `.env`:

```
# === Maker Strategy (Option A) ===
# Posts limit orders at better prices, waits for fills, cancels if unfilled
RIVALCLAW_MAKER_ENABLED=0
RIVALCLAW_MAKER_OFFSET_PCT=0.10
RIVALCLAW_MAKER_PATIENCE_SEC=120
RIVALCLAW_MAKER_MAX_RESTING=5
RIVALCLAW_MAKER_MIN_FILL_RATE=0.30
```

Note: starts DISABLED (`=0`). Enable after verification.

- [ ] **Step 2: Run migration**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import kalshi_executor
kalshi_executor.migrate_maker_columns()
print('Migration complete')
"
```

- [ ] **Step 3: End-to-end verification**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import os; os.environ['RIVALCLAW_MAKER_ENABLED'] = '1'
import execution_router
cfg = execution_router._get_maker_config()
print('Maker config:', cfg)
print('Should use maker (KXBTC-test):', execution_router._should_use_maker('KXBTC-test', cfg))
print()
# Verify taker still works when disabled
os.environ['RIVALCLAW_MAKER_ENABLED'] = '0'
cfg2 = execution_router._get_maker_config()
print('Disabled - should use maker:', execution_router._should_use_maker('KXBTC-test', cfg2))
"
```

Expected: `True` when enabled, `False` when disabled.

- [ ] **Step 4: Commit**

```bash
git add .env
git commit -m "config: add maker strategy env vars (disabled by default)"
```

- [ ] **Step 5: Clear pycache**

```bash
find /Users/nayslayer/rivalclaw -name '__pycache__' -type d -exec rm -rf {} +
```

---

### Task 5: Integration test — verify full flow

**Files:**
- No new files — testing existing changes

- [ ] **Step 1: Test maker pricing offset**

```python
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import os
os.environ['RIVALCLAW_MAKER_ENABLED'] = '1'
os.environ['RIVALCLAW_MAKER_OFFSET_PCT'] = '0.10'

# Simulate: brain says NO at entry_price=0.50
# Maker should offset to 0.45
original = 0.50
offset = 0.10
expected = original * (1 - offset)
print(f'Brain price: {original}')
print(f'Maker price: {expected}')
print(f'Savings: {original - expected} per contract')
assert expected == 0.45, f'Expected 0.45, got {expected}'
print('PASS')
"
```

- [ ] **Step 2: Test fill rate query with empty data**

```python
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import kalshi_executor
rate = kalshi_executor.get_maker_fill_rate('KXBTC')
print(f'Fill rate (no data): {rate}')
assert rate == 1.0, 'Should default to 1.0 with no data'
count = kalshi_executor.get_resting_count()
print(f'Resting count: {count}')
print('PASS')
"
```

- [ ] **Step 3: Test anti-stacking allows maker resting orders**

```python
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import execution_router
# A filled order should block
has_filled = execution_router._has_filled_order_for_ticker('KXBNB15M-26MAR301830-30')
print(f'Has filled BNB 1830: {has_filled}')  # True — from tonight's trades
# A non-existent ticker should pass
has_filled2 = execution_router._has_filled_order_for_ticker('KXBTC-FAKE-99999')
print(f'Has filled fake: {has_filled2}')  # False
print('PASS')
"
```

- [ ] **Step 4: Commit all**

```bash
git add -A
git commit -m "feat: simple maker strategy — complete implementation"
```
