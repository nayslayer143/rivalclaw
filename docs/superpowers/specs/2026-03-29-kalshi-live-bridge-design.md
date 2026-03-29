# RivalClaw Kalshi Live Trading Bridge + ERS Dashboard

**Date:** 2026-03-29
**Status:** Draft
**Scope:** Two sub-projects delivering live Kalshi trading and a dashboard at eternalrevenueservice.com

---

## Sub-Project 1: Trading Bridge (Backend)

### Overview

Extend `protocol_adapter.py` with a live execution backend that routes trade intents to Kalshi's REST API. Three execution modes: paper (default), shadow (dry-run logging), and live (real orders).

### Architecture

```
trading_brain.py generates decision
  -> simulator.py routes to protocol_adapter.py
    -> protocol_adapter builds TradeIntent + logs command
      -> execution_router decides destination:
          paper:  protocol engine only (current behavior)
          shadow: protocol engine + log what WOULD be submitted
          live:   protocol engine + submit to Kalshi REST API
      -> result recorded in live_orders table
```

### New Files

| File | Purpose |
|------|---------|
| `kalshi_executor.py` | Kalshi order submission, status polling, account sync, rate limiting |
| `execution_router.py` | Routes trade intents to paper/shadow/live based on config + safety checks |

### Modified Files

| File | Change |
|------|--------|
| `protocol_adapter.py` | After `_engine.execute_entry()`, call execution_router |
| `simulator.py` | Add account sync step at cycle start when mode != paper |
| `.env` | New config vars for execution mode + risk limits |
| `CLAUDE.md` | Update doctrine to permit live trading under safety controls |

### Execution Modes

| Mode | Env Value | Behavior |
|------|-----------|----------|
| Paper | `paper` | Current behavior. No changes. Default. |
| Shadow | `shadow` | Paper trade executes. `kalshi_executor.py` logs what order it WOULD submit (ticker, side, count, price) to `live_orders` table with `mode=shadow`. No HTTP calls to Kalshi order endpoints. |
| Live | `live` | Paper trade executes. Real limit order submitted to Kalshi. Order status polled and recorded. Reconciliation logged. |

### `kalshi_executor.py` — Capabilities

**Order Submission:**
- POST `/trade-api/v2/portfolio/orders`
- Fields: `ticker`, `action` (buy/sell), `side` (yes/no), `count`, `type` (limit), `yes_price` (cents 1-99), `client_order_id` (UUID4)
- Exponential backoff on 429: 1s -> 2s -> 4s -> max 60s
- Telegram alert on repeated 429s

**Order Management:**
- GET `/trade-api/v2/portfolio/orders/{order_id}` — status check
- DELETE `/trade-api/v2/portfolio/orders/{order_id}` — cancel single order
- POST `/trade-api/v2/portfolio/orders/{order_id}/amend` — amend (count and/or price)
- POST `/trade-api/v2/portfolio/orders/{order_id}/decrease` — reduce contract count
- POST `/trade-api/v2/portfolio/orders/batch` — batch cancel (each counts as 0.2 write ops)

**Note on units:** Kalshi API uses cents (integers 1-99) for prices. RivalClaw paper wallet uses dollars (floats 0.01-0.99). The executor handles conversion at the boundary: `cents = int(round(dollar_price * 100))`.

**Order Status Polling:**
- After submission, poll up to 3 times at 2s intervals
- Statuses tracked: pending, filled, partial, rejected, cancelled
- On fill: record fill_price, fill_count, filled_at

**Account Sync (called once per cycle when mode != paper):**
- GET `/trade-api/v2/portfolio/balance` — real account balance (cents)
- GET `/trade-api/v2/portfolio/positions` — real open positions
- GET `/trade-api/v2/portfolio/fills` — recent fill records
- Results cached in `account_snapshots` table

**Rate Limiting:**
- Token bucket: 10 writes/sec (Basic tier)
- Configurable via `RIVALCLAW_KALSHI_WRITE_RATE` env var
- Request counter per second window, blocks if exceeded
- All rate limit info exposed via API for dashboard consumption

### `execution_router.py` — Pre-Flight Safety Checks

Every order passes ALL checks before submission. Any failure = reject.

1. **Mode check** — is execution mode `live`? If not, shadow-log only.
2. **Kill switch** — is `RIVALCLAW_LIVE_KILL_SWITCH=1`? Reject all.
3. **Balance check** — does Kalshi account have funds >= order cost?
4. **Exposure check** — would total open exposure exceed `LIVE_MAX_EXPOSURE_USD`?
5. **Order size check** — is order <= `LIVE_MAX_ORDER_USD`?
6. **Contract count check** — is count <= `LIVE_MAX_CONTRACTS_PER_ORDER`?
7. **Rate check** — have we hit per-cycle or per-hour order limits?
8. **Series check** — is ticker prefix in `LIVE_SERIES` allowlist?
9. **Price sanity** — is order price within 10% of last known market price?
10. **Staleness check** — is market data < 5 minutes old?

Rejection reason logged to `live_orders` table with `status=rejected`.

### Safety Controls

| Control | Default | Env Var |
|---------|---------|---------|
| Max single order | $2 | `RIVALCLAW_LIVE_MAX_ORDER_USD` |
| Max total exposure | $10 | `RIVALCLAW_LIVE_MAX_EXPOSURE_USD` |
| Max contracts per order | 5 | `RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER` |
| Max orders per cycle | 2 | `RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE` |
| Max orders per hour | 10 | `RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR` |
| Allowed series | 15-min crypto | `RIVALCLAW_LIVE_SERIES` |
| Kill switch | off | `RIVALCLAW_LIVE_KILL_SWITCH` |
| Price deviation max | 10% | `RIVALCLAW_LIVE_MAX_PRICE_DEVIATION` |
| Write rate limit | 10/sec | `RIVALCLAW_KALSHI_WRITE_RATE` |

### Reconciliation

After each live fill:
- Compare paper execution price vs real fill price
- Compute slippage delta in bps
- Log to `live_reconciliation` table
- If real slippage exceeds paper slippage by >5%, send Telegram alert

### Database Changes

New tables in `rivalclaw.db`:

```sql
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY,
    intent_id TEXT NOT NULL,
    client_order_id TEXT UNIQUE NOT NULL,
    kalshi_order_id TEXT,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,          -- buy/sell
    side TEXT NOT NULL,            -- yes/no
    count INTEGER NOT NULL,
    yes_price INTEGER NOT NULL,   -- cents (1-99)
    order_type TEXT DEFAULT 'limit',
    status TEXT DEFAULT 'pending', -- pending/filled/partial/rejected/cancelled/shadow
    fill_price INTEGER,
    fill_count INTEGER,
    submitted_at TEXT,
    filled_at TEXT,
    mode TEXT NOT NULL,            -- shadow/live
    error_message TEXT,
    rejection_reason TEXT,
    cycle_id TEXT,
    strategy TEXT,
    market_question TEXT
);

CREATE TABLE IF NOT EXISTS live_reconciliation (
    id INTEGER PRIMARY KEY,
    live_order_id INTEGER REFERENCES live_orders(id),
    paper_entry_price REAL,
    live_fill_price REAL,
    slippage_delta_bps REAL,
    paper_amount_usd REAL,
    live_amount_usd REAL,
    reconciled_at TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY,
    balance_cents INTEGER,
    portfolio_value_cents INTEGER,
    open_positions INTEGER,
    fetched_at TEXT
);
```

### .env Additions

```bash
# Execution mode: paper (default) | shadow | live
RIVALCLAW_EXECUTION_MODE=paper

# Safety limits (for $10 wallet)
RIVALCLAW_LIVE_MAX_ORDER_USD=2
RIVALCLAW_LIVE_MAX_EXPOSURE_USD=10
RIVALCLAW_LIVE_MAX_CONTRACTS_PER_ORDER=5
RIVALCLAW_LIVE_MAX_ORDERS_PER_CYCLE=2
RIVALCLAW_LIVE_MAX_ORDERS_PER_HOUR=10
RIVALCLAW_LIVE_SERIES=KXDOGE15M,KXADA15M,KXBNB15M,KXBCH15M
RIVALCLAW_LIVE_KILL_SWITCH=0
RIVALCLAW_LIVE_MAX_PRICE_DEVIATION=0.10
RIVALCLAW_KALSHI_WRITE_RATE=10

# Production API key (replace demo key)
KALSHI_PRIVATE_KEY_PATH=/Users/nayslayer/.kalshi/private.pem
```

### CLAUDE.md Doctrine Updates

Remove from non-negotiable rules:
- "No live trading"
- "No private key handling"

Add to non-negotiable rules:
- Live trading requires explicit mode flag (`RIVALCLAW_EXECUTION_MODE=live`)
- All live orders must pass pre-flight safety checks (10-point checklist)
- Kill switch must always be functional and immediately halt all submissions
- No optimistic fills — live fills are real, paper fills stay simulated
- Max order size and exposure limits are hard-enforced, not advisory

---

## Sub-Project 2: ERS Dashboard (Frontend)

### Overview

A fully private Next.js dashboard at eternalrevenueservice.com. Auth-gated. Shows real-time trading data, Kalshi account state, strategy performance, and provides controls for all Kalshi API operations.

### Infrastructure

```
Your Mac (always running)
  ├── rivalclaw (cron, trading loop)
  ├── rivalclaw.db (SQLite, source of truth)
  ├── FastAPI bridge server (port 8400)
  │     ├── /api/db/*          → reads rivalclaw.db
  │     ├── /api/kalshi/*      → proxies to Kalshi API (RSA auth server-side)
  │     └── /api/control/*     → writes mode/config changes to .env + context table
  └── Cloudflare Tunnel → exposes bridge server to internet

Vercel
  └── eternalrevenueservice.com (Next.js)
        ├── Auth (NextAuth.js, credentials provider)
        ├── Pages (dashboard, trades, markets, strategies, controls, system)
        └── API routes → proxy to FastAPI bridge via Cloudflare Tunnel URL
```

**Why FastAPI bridge:** rivalclaw.db is SQLite on your Mac. Vercel can't access it directly. A small FastAPI service exposes it over HTTP. Cloudflare Tunnel (free) gives it a stable public URL without port forwarding.

### DNS Setup

GoDaddy domain → Cloudflare nameservers (for tunnel + CDN):
1. Add eternalrevenueservice.com to Cloudflare (free plan)
2. Update GoDaddy nameservers to Cloudflare's
3. In Cloudflare: CNAME `@` → Vercel's cname target
4. Cloudflare Tunnel for the FastAPI bridge (separate subdomain, e.g., `api.eternalrevenueservice.com`)

### Authentication

NextAuth.js with credentials provider (simple username/password). Fully private — every page and API route gated. No public access.

### Dashboard Pages

#### 1. Home (`/`)
- Kalshi wallet balance (real, synced)
- Paper wallet balance (from rivalclaw.db)
- Daily P&L chart (line chart, last 30 days)
- Current execution mode indicator (paper/shadow/live)
- Open positions count
- Last cycle timestamp + status
- Quick stats: win rate, total trades, edge capture rate

#### 2. Trades (`/trades`)
- Tabbed view: Open | Closed | Shadow | Live Orders
- Each trade row: market, direction, entry price, current price, P&L, strategy, venue, timestamps
- Reconciliation panel: paper vs live fill comparison (when in live mode)
- Filters: by strategy, by venue, by date range, by status
- Fill history from Kalshi API
- Settlement history from Kalshi API

#### 3. Markets (`/markets`)
- Active markets being tracked (all FAST_SERIES tickers)
- Per-market: last price, bid/ask spread, volume, OI, time to expiry
- Orderbook viewer (depth chart) for selected market
- Candlestick chart (1-min, 1-hr, 1-day from Kalshi API)
- Market status indicators (open, closed, settled)
- Exchange status banner (is Kalshi up?)

#### 4. Strategies (`/strategies`)
- Per-strategy cards: win rate, avg P&L, trade count, edge capture rate
- Strategy-level P&L chart over time
- Kelly sizing: recommended vs actual
- Best/worst performing strategy highlight
- Strategy enable/disable toggles

#### 5. Controls (`/controls`)
- **Start Trading** — big green button, sets mode to `live`
- **Kill Switch** — big red animated button, sets kill switch + batch cancels all resting orders
- **Shadow Mode** — amber button, sets mode to `shadow`
- **Paper Mode** — default button, sets mode to `paper`
- **Cancel All Orders** — calls Kalshi batch cancel endpoint
- **Cancel Order** — per-order cancel (from order list)
- **Amend Order** — modal to change price/size on resting order
- **Place Manual Order** — form: ticker, action, side, count, price. Submit to Kalshi.
- **View Order Queue** — check queue position for resting orders
- **Sync Balance** — force-refresh Kalshi balance
- **Sync Positions** — force-refresh Kalshi positions
- **View Fills** — pull latest fills from Kalshi
- **View Settlements** — pull settlement history

- **Safety config panel:** edit max order size, max exposure, allowed series, rate limits. Changes write to .env via the bridge API.

#### 6. System (`/system`)
- Cycle timing: avg, p95, max (from cycle_metrics table)
- API rate limit usage gauge (reads/sec, writes/sec vs tier limit)
- Last 50 cycle entries with timing breakdown
- Error log (last 100 errors from event_logger)
- Cron status indicator
- Telegram alert history
- Process count (detect cascade stacking)

### FastAPI Bridge Server

Runs on your Mac at port 8400. Endpoints:

**Database reads:**
- `GET /api/db/wallet` — paper wallet state
- `GET /api/db/trades?status=open|closed&limit=50&offset=0` — trade history
- `GET /api/db/live-orders?mode=shadow|live&limit=50` — live/shadow order log
- `GET /api/db/reconciliation` — paper vs live comparison
- `GET /api/db/strategies` — per-strategy performance aggregates
- `GET /api/db/cycles?limit=50` — cycle timing metrics
- `GET /api/db/daily-pnl` — daily P&L snapshots
- `GET /api/db/errors?limit=100` — event log errors
- `GET /api/db/account-snapshots` — Kalshi account history
- `GET /api/db/market-data?venue=kalshi&limit=50` — cached market data

**Kalshi API proxies (authenticated server-side):**
- `GET /api/kalshi/balance` — real balance
- `GET /api/kalshi/positions` — real positions
- `GET /api/kalshi/orders?status=resting` — resting orders
- `GET /api/kalshi/fills?limit=50` — fill history
- `GET /api/kalshi/settlements?limit=50` — settlement history
- `GET /api/kalshi/market/{ticker}` — single market data
- `GET /api/kalshi/market/{ticker}/orderbook` — orderbook
- `GET /api/kalshi/market/{ticker}/candlesticks?period=1m` — candlesticks
- `GET /api/kalshi/exchange/status` — exchange status
- `POST /api/kalshi/orders` — place order (requires auth token in bridge)
- `DELETE /api/kalshi/orders/{id}` — cancel order
- `PUT /api/kalshi/orders/{id}` — amend order
- `POST /api/kalshi/orders/batch-cancel` — batch cancel
- `GET /api/kalshi/orders/{id}/queue` — queue position

**Control endpoints:**
- `GET /api/control/mode` — current execution mode
- `POST /api/control/mode` — set execution mode (paper/shadow/live)
- `GET /api/control/kill-switch` — kill switch status
- `POST /api/control/kill-switch` — activate/deactivate kill switch
- `GET /api/control/config` — current safety config
- `POST /api/control/config` — update safety config values
- `POST /api/control/sync-balance` — trigger account sync
- `POST /api/control/sync-positions` — trigger position sync

**Auth:** The bridge uses a shared API key between itself and the Next.js app. Set via `ERS_BRIDGE_API_KEY` env var. Every request must include `Authorization: Bearer <key>` header.

### UI Design Direction

Clean, data-dense dashboard. Dark theme. Think Bloomberg terminal meets modern web UI.

- Cards with key metrics, sparkline charts
- Real-time indicators (green/amber/red dots)
- The kill switch should be visually prominent and satisfying to press — large red button with glow effect, confirmation modal
- Start trading button: large green with pulse animation when active
- Mode indicator always visible in the header: PAPER (gray) | SHADOW (amber) | LIVE (green, pulsing)
- Responsive but desktop-first (this is a trading dashboard)

### Tech Stack

- **Framework:** Next.js 14+ (App Router)
- **Styling:** Tailwind CSS
- **Charts:** Lightweight Charts (TradingView) for candlesticks, Recharts for P&L/metrics
- **Auth:** NextAuth.js (credentials provider)
- **State:** React Query (TanStack Query) for server state, polling intervals
- **Hosting:** Vercel
- **Bridge:** FastAPI (Python, runs on Mac)
- **Tunnel:** Cloudflare Tunnel (free, `cloudflared`)

---

## Implementation Order

1. **Trading bridge first** — `kalshi_executor.py`, `execution_router.py`, DB migrations, .env updates
2. **FastAPI bridge server** — expose rivalclaw.db + Kalshi API proxy
3. **Cloudflare Tunnel setup** — domain config, tunnel for bridge
4. **Dashboard frontend** — Next.js app, page by page
5. **Shadow mode validation** — run shadow for 24-48h, verify order construction
6. **Live mode activation** — flip the switch

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Overdraw $10 wallet | Hard exposure cap at $10, pre-flight balance check |
| Rate limit ban | Token bucket + exponential backoff + rate gauge on dashboard |
| Stale data = bad orders | 5-minute staleness check, reject if data too old |
| Mac goes offline | Dashboard shows "bridge offline" indicator, trading halts gracefully |
| Kill switch fails | Kill switch writes to both .env AND context table — double check in execution_router |
| API key compromise | Bridge API key only exposed to Vercel via env var, never client-side |
