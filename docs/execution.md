# Kalshi Execution Deep-Dive

RivalClaw submits real orders to Kalshi via RSA-authenticated REST API calls.
This document covers the full execution pipeline from authentication through settlement.

## RSA Authentication

**File:** `kalshi_feed.py` -- `_auth_headers()`, `_load_private_key()`, `_sign_request()`

1. Load RSA private key from PEM file at `KALSHI_PRIVATE_KEY_PATH`.
2. Generate millisecond timestamp: `str(int(time.time() * 1000))`.
3. Build the signing message: `timestamp + METHOD + /trade-api/v2 + path` (UTF-8 encoded).
4. Sign with RSA-PSS (SHA-256 digest, MGF1-SHA256, salt length = digest length).
5. Base64-encode the signature.
6. Construct headers:

```
KALSHI-ACCESS-KEY:       <KALSHI_API_KEY_ID>
KALSHI-ACCESS-TIMESTAMP: <millisecond timestamp>
KALSHI-ACCESS-SIGNATURE: <base64 RSA-PSS signature>
Content-Type:            application/json
```

The executor (`kalshi_executor.py`) reuses `kalshi_feed._auth_headers()` for all
write operations. The API base URL is determined by `KALSHI_API_ENV` (prod vs demo).

## Order Lifecycle

### 1. Build Payload

`kalshi_executor.build_order_payload()` constructs a limit order dict:

- Converts dollar price to cents, clamped 1-99.
- Generates a UUID4 `client_order_id` for idempotency.
- Always `type: "limit"`.

### 2. Submit

`kalshi_executor.submit_order()` POSTs to `/portfolio/orders`:

- Checks the local `RateLimiter` first. If the per-second budget is exhausted,
  the order is rejected locally before hitting the API.
- On HTTP 429: exponential backoff (1s, 2s, 4s), up to 3 retries.
- On HTTP 401: immediate failure (auth_failed).
- On success: returns the Kalshi response JSON containing `order.order_id`.

### 3. Poll for Fill

**Taker mode:** `poll_order_status()` -- polls GET `/portfolio/orders/{id}` up to
3 times at 2-second intervals. Returns as soon as status is `executed`, `canceled`,
or `cancelled`.

**Maker mode:** `poll_or_cancel()` -- polls at 10-second intervals until the
patience window expires (default 120s). If not filled by deadline, sends a
DELETE to cancel the resting order. Returns status `cancelled_timeout`.

### 4. Fill or Reject

After polling, `execution_router.route_trade()` updates the DB:

- `executed` -> status `filled`, records fill_price and fill_count.
- `canceled`/`cancelled` -> status as-is.
- API error -> status `error` with error_message.

### 5. Settlement

`reconcile_filled_orders()` transitions filled orders to `settled`:

- Queries GET `/markets/{ticker}` for the `result` field.
- Determines win/loss: compares bet side against resolution outcome.
- Computes PnL in cents: `payout - cost`.
- Sets `outcome` (win/loss) and `pnl_cents` on the live_orders row.

## Rate Limiting

**Class:** `kalshi_executor.RateLimiter`

Sliding-window counter: resets every 1.0 second. Default limit: 10 writes/sec
(`RIVALCLAW_KALSHI_WRITE_RATE`). The `acquire()` method returns False when the
window is full. The execution router checks this before every API call.

Per-cycle and per-hour limits are enforced separately in `execution_router.py`:

| Limit | Default | Env Var |
|-------|---------|---------|
| Per second (API) | 10 | `RIVALCLAW_KALSHI_WRITE_RATE` |
| Per cycle | 2 | `RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE` |
| Per cycle (weather) | 30 | `RIVALCLAW_LIVE_MAX_WEATHER_PER_CYCLE` |
| Per hour | 10 | `RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR` |

## Maker Mode

When `RIVALCLAW_MAKER_ENABLED=1`, the router may post limit orders below the
current market price to capture the spread.

**Decision flow in `_should_use_maker()`:**

1. Check `RIVALCLAW_MAKER_ENABLED` is 1.
2. Count resting orders -- must be below `RIVALCLAW_MAKER_MAX_RESTING` (default 5).
3. Query rolling fill rate for the series prefix (last 20 maker orders).
   If fill rate < `RIVALCLAW_MAKER_MIN_FILL_RATE` (default 0.30), fall back to taker.

**Offset pricing:** Entry price is reduced by `RIVALCLAW_MAKER_OFFSET_PCT` (default
10%), floored at $0.08. The original brain price is saved for savings tracking.

**Cancel-before-replace:** Before posting a new maker order on a ticker,
`cancel_resting_for_ticker()` cancels any existing resting order for that ticker.

**Fill monitoring:** After the order is submitted, `poll_or_cancel()` watches it
for `RIVALCLAW_MAKER_PATIENCE_SEC` (default 120s). If unfilled, the order is
cancelled automatically.

**Auto-fallback:** When the series fill rate drops below the threshold, maker mode
is disabled for that series and orders revert to taker.

**Savings tracking:** On fill, `maker_savings = brain_price - actual_cost` and
`fill_time_sec` are written to the live_orders row.

## Anti-Stacking (3 Layers)

Prevents the same market from being traded multiple times:

1. **In-memory set** (`_submitted_market_ids`): Tracks every ticker with a live
   order submitted this process session. Checked before pre-flight. Cleared per-
   market via `clear_settled_market()` after settlement.

2. **DB check** (`_has_any_live_order_for_ticker()`): Queries live_orders for any
   row with matching ticker in status pending/resting/filled. Fails closed --
   returns True (blocks) on any DB error.
   For maker mode, `_has_filled_order_for_ticker()` is used instead (resting orders
   are expected and do not block).

3. **Protocol positions** (`get_open_market_ids()`): The simulator checks open
   paper_trades and skips any decision whose market_id already has an open position.

## Reconciliation

Three reconciliation passes run during `sync_account()`:

### Resting -> Filled/Cancelled

`reconcile_resting_orders()` polls each resting order via GET `/portfolio/orders/{id}`:
- `executed` -> update to `filled`
- `cancelled`/`canceled`/`expired` -> update to `cancelled`
- HTTP 404 -> update to `cancelled`

### Pending -> Cancelled (Stale)

Orders in `pending` status older than 10 minutes are auto-cancelled with
`rejection_reason='stale_pending'`. These consume exposure budget and never resolve.

### Filled -> Settled

`reconcile_filled_orders()` checks each filled order against GET `/markets/{ticker}`:
- If `result` field is present, the market has resolved.
- Computes win/loss and PnL, updates status to `settled`.

## Price Conversion (NO Trades)

For NO trades, the brain's `entry_price` represents the NO cost (what we pay).
The Kalshi API accepts `yes_price` as the limit. Conversion:

```
api_yes_price = 1.0 - entry_price
```

Example: Brain says NO at $0.20 cost -> API yes_price = $0.80 -> 80 cents.

For logging in live_orders, the `yes_price` column stores the API value (in cents).
For reconciliation, the fill price is converted back to NO cost so slippage
comparison is apples-to-apples:

```
recon_fill_cents = 100 - fill_price   # for NO trades
```

## Slippage Tracking

The `live_reconciliation` table stores paper-vs-live price deltas:

- `paper_entry_price`: what the brain intended.
- `live_fill_price`: what Kalshi actually filled at (normalized to same frame).
- `slippage_delta_bps`: `abs(live - paper) / paper * 10000`.

If slippage exceeds 500 bps, a Telegram alert is fired via `send_live_alert()`.

## Telegram Alerts

The executor and router send live alerts for:

- `order_submitted` -- every order sent to Kalshi.
- `order_filled` -- confirmed fill with price and count.
- `order_rejected` -- API error or pre-flight failure.
- `rate_limited` -- local or Kalshi rate limit hit.
- `slippage_warning` -- fill slippage > 500 bps.

Alerts use `notify.send_live_alert()` which posts to the configured Telegram bot.
