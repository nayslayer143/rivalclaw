# Kalshi + Fast-Resolution Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make RivalClaw actively trade by adding Kalshi fast-resolution markets (15-min crypto, hourly indices) with a fair-value directional strategy, plus near-expiry directional trades on Polymarket.

**Architecture:** Add kalshi_feed.py (ported from OpenClaw) and spot_feed.py (CoinGecko) as new data sources. Expand trading_brain.py with two new strategies: (1) fair-value directional using spot-vs-contract mispricing on Kalshi fast-resolution contracts, and (2) near-expiry momentum on Polymarket for markets resolving within 48 hours. Both venues feed through the existing simulator → brain → wallet → metrics flow. Cron interval drops to 2 minutes.

**Tech Stack:** Python 3.9+, SQLite, requests, cryptography (RSA auth for Kalshi), CoinGecko API (free, no auth)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `.env` | Modify | Add Kalshi credentials (already present, clean up dupes) |
| `simulator.py` | Modify | Add Kalshi fetch step, combined market list, multi-venue price tracking |
| `trading_brain.py` | Modify | Add fair_value_directional + near_expiry_momentum strategies |
| `kalshi_feed.py` | Create | Kalshi API client with RSA auth, focused on fast-resolution series |
| `spot_feed.py` | Create | CoinGecko crypto spot prices for fair-value computation |
| `polymarket_feed.py` | Modify | Add venue field, add near-expiry filtering function |
| `paper_wallet.py` | Modify | Combined price lookup for mark-to-market across venues |
| `run.py` | No change | Already loads .env |

---

### Task 1: Install cryptography package and clean .env

**Files:**
- Modify: `.env`

- [ ] **Step 1: Install cryptography**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && pip install cryptography
```

- [ ] **Step 2: Clean up duplicate .env entries**

`.env` should contain exactly:
```
KALSHI_API_KEY_ID=517bb82c-62d4-444d-9a94-8d1b52233ea4
KALSHI_API_ENV=prod
KALSHI_PRIVATE_KEY_PATH=/Users/nayslayer/.kalshi/demo-private.pem
```

- [ ] **Step 3: Verify key loads**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
from cryptography.hazmat.primitives.serialization import load_pem_private_key
with open('/Users/nayslayer/.kalshi/demo-private.pem', 'rb') as f:
    key = load_pem_private_key(f.read(), password=None)
print(f'Key loaded: {key.key_size}-bit RSA')
"
```

Expected: `Key loaded: 2048-bit RSA` (or 4096)

---

### Task 2: Add DB migration for venue tracking

**Files:**
- Modify: `simulator.py:28-105` (MIGRATION_SQL)

- [ ] **Step 1: Add venue column and kalshi_extra table to migration**

Add to MIGRATION_SQL after existing CREATE statements:

```sql
-- Venue tracking for multi-source markets
ALTER TABLE market_data ADD COLUMN venue TEXT DEFAULT 'polymarket';
-- (ALTER TABLE is idempotent with IF NOT EXISTS not supported, so wrap in try/except in Python)

-- Kalshi-specific fields needed for fair value computation
CREATE TABLE IF NOT EXISTS kalshi_extra (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    event_ticker TEXT,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume_24h REAL,
    open_interest REAL,
    close_time TEXT,
    strike_type TEXT,
    cap_strike REAL,
    floor_strike REAL,
    rules_primary TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kalshi_extra_market_time ON kalshi_extra(market_id, fetched_at);
```

Since SQLite doesn't support `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, handle it in Python:

```python
# Add after conn.executescript(MIGRATION_SQL):
try:
    conn.execute("ALTER TABLE market_data ADD COLUMN venue TEXT DEFAULT 'polymarket'")
except sqlite3.OperationalError:
    pass  # Column already exists
try:
    conn.execute("ALTER TABLE paper_trades ADD COLUMN venue TEXT DEFAULT 'polymarket'")
except sqlite3.OperationalError:
    pass
```

- [ ] **Step 2: Run migration**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 run.py --migrate
```

Expected: `[rivalclaw] Migration complete.`

- [ ] **Step 3: Verify columns exist**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import sqlite3
conn = sqlite3.connect('rivalclaw.db')
cols = [r[1] for r in conn.execute('PRAGMA table_info(market_data)').fetchall()]
print('market_data columns:', cols)
assert 'venue' in cols, 'venue column missing'
cols2 = [r[1] for r in conn.execute('PRAGMA table_info(kalshi_extra)').fetchall()]
print('kalshi_extra columns:', cols2)
print('OK')
"
```

- [ ] **Step 4: Commit**

```bash
git add simulator.py && git commit -m "feat: add venue column and kalshi_extra table to migration"
```

---

### Task 3: Create kalshi_feed.py

**Files:**
- Create: `kalshi_feed.py`

This is a focused port of `/Users/nayslayer/openclaw/scripts/mirofish/kalshi_feed.py` adapted for RivalClaw:
- Uses `RIVALCLAW_DB_PATH` instead of `CLAWMSON_DB_PATH`
- Stores to both `market_data` (for dashboard compat) and `kalshi_extra` (for fair value)
- Focused on fast-resolution series only (15-min crypto, hourly)
- Same RSA auth pattern

- [ ] **Step 1: Create kalshi_feed.py**

Core structure:
```python
#!/usr/bin/env python3
"""
RivalClaw Kalshi feed — RSA-authenticated API client focused on fast-resolution markets.
Ported from OpenClaw kalshi_feed.py, adapted for rivalclaw.db schema.
"""
import base64, datetime, os, sqlite3, time, json, requests
from pathlib import Path

DB_PATH = Path(os.environ.get("RIVALCLAW_DB_PATH", Path(__file__).parent / "rivalclaw.db"))

# Focus on fastest-resolving series
FAST_SERIES = [
    "KXDOGE15M", "KXADA15M", "KXBNB15M", "KXBCH15M",  # 15-min crypto
    "INXI", "NASDAQ100I",  # hourly indices
    "KXBTC", "KXETH",  # daily crypto (bracket contracts)
]

PROD_API = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"
PAGE_LIMIT = 200
CACHE_MAX_AGE_HOURS = 1  # Shorter cache for fast markets

# Map series to underlying crypto ticker for spot feed
SERIES_TO_UNDERLYING = {
    "KXDOGE15M": "dogecoin",
    "KXADA15M": "cardano",
    "KXBNB15M": "binancecoin",
    "KXBCH15M": "bitcoin-cash",
    "KXBTC": "bitcoin",
    "KXBTCMAXD": "bitcoin",
    "KXETH": "ethereum",
}
```

Include:
- `_get_conn()` — uses RIVALCLAW_DB_PATH
- `_load_private_key()` — same as OpenClaw
- `_sign_request()` — same as OpenClaw
- `_auth_headers()` — same as OpenClaw
- `_call_kalshi()` — same as OpenClaw
- `_adapt_market_fields()` — same as OpenClaw
- `_fetch_event_markets(series)` — same as OpenClaw
- `fetch_markets()` — iterates FAST_SERIES, stores to market_data + kalshi_extra, returns normalized dicts
- `get_latest_prices()` — returns {market_id: {yes_price, no_price}} from market_data where venue='kalshi'

Key differences from OpenClaw:
- DB path → rivalclaw.db
- Writes to `market_data` table (yes_price=last_price, no_price=1-last_price, venue='kalshi', end_date=close_time)
- Also writes to `kalshi_extra` table (bid/ask, strike data)
- Returns market dicts with extra fields: `venue`, `close_time`, `strike_type`, `cap_strike`, `floor_strike`, `yes_bid`, `yes_ask`
- CACHE_MAX_AGE = 1 hour (not 6)

- [ ] **Step 2: Verify Kalshi API auth works**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import kalshi_feed
markets = kalshi_feed.fetch_markets()
print(f'Fetched {len(markets)} Kalshi markets')
if markets:
    m = markets[0]
    print(f'  Sample: {m[\"market_id\"]} — {m[\"question\"][:60]}')
    print(f'  yes_price={m.get(\"yes_price\")}, close_time={m.get(\"close_time\")}')
"
```

Expected: Some markets fetched (if API auth works). If 0, check auth error messages.

- [ ] **Step 3: Commit**

```bash
git add kalshi_feed.py && git commit -m "feat: add Kalshi feed for fast-resolution markets"
```

---

### Task 4: Create spot_feed.py

**Files:**
- Create: `spot_feed.py`

Simple CoinGecko price fetcher. No auth needed. Used by the brain for fair-value computation.

- [ ] **Step 1: Create spot_feed.py**

```python
#!/usr/bin/env python3
"""
RivalClaw spot price feed — CoinGecko free API for crypto spot prices.
Used to compute fair value of Kalshi binary contracts.
"""
import time, requests

COINGECKO_API = "https://api.coingecko.com/api/v3/simple/price"

# CoinGecko IDs for the cryptos we trade on Kalshi
CRYPTO_IDS = "bitcoin,ethereum,dogecoin,cardano,binancecoin,bitcoin-cash"

_cache = {}
_cache_ts = 0
CACHE_TTL_SECONDS = 30  # Refresh every 30s max

def get_spot_prices() -> dict:
    """
    Returns {coingecko_id: price_usd} for all tracked cryptos.
    Cached for 30 seconds to avoid rate limits (CoinGecko free = 30 calls/min).
    """
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < CACHE_TTL_SECONDS:
        return _cache

    try:
        resp = requests.get(COINGECKO_API, params={
            "ids": CRYPTO_IDS,
            "vs_currencies": "usd",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _cache = {k: v["usd"] for k, v in data.items() if "usd" in v}
        _cache_ts = time.time()
        return _cache
    except Exception as e:
        print(f"[rivalclaw/spot] CoinGecko error: {e}")
        return _cache  # Return stale cache on error

def get_crypto_price(coingecko_id: str) -> float | None:
    """Get spot price for a single crypto. Returns None if unavailable."""
    prices = get_spot_prices()
    return prices.get(coingecko_id)
```

- [ ] **Step 2: Test spot feed**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import spot_feed
prices = spot_feed.get_spot_prices()
for k, v in prices.items():
    print(f'  {k}: \${v:,.2f}')
"
```

Expected: Current crypto prices.

- [ ] **Step 3: Commit**

```bash
git add spot_feed.py && git commit -m "feat: add CoinGecko spot price feed for fair value computation"
```

---

### Task 5: Expand trading_brain.py with new strategies

**Files:**
- Modify: `trading_brain.py`

This is the most critical task. We're adding two new strategies alongside the existing cross-outcome arb:

**Strategy: `fair_value_directional`**
- For Kalshi contracts with known underlying (crypto)
- Compute fair value from current spot price, strike, time to expiry, and estimated volatility
- Trade when market price diverges from fair value by > threshold
- Uses simplified Black-Scholes for binary options

**Strategy: `near_expiry_momentum`**
- For both Polymarket and Kalshi markets expiring within 48 hours
- When a binary market is near expiry and price is strongly directional (>0.75 or <0.25), bet on continuation
- The closer to expiry + the more extreme the price, the higher the confidence

- [ ] **Step 1: Add fair value computation**

Add to trading_brain.py:

```python
import math

# Annualized volatility estimates for crypto (conservative)
CRYPTO_VOL = {
    "dogecoin": 0.90,    # DOGE is volatile
    "cardano": 0.80,
    "binancecoin": 0.65,
    "bitcoin-cash": 0.75,
    "bitcoin": 0.60,
    "ethereum": 0.65,
}
MIN_FAIR_VALUE_EDGE = float(os.environ.get("RIVALCLAW_MIN_FV_EDGE", "0.08"))  # 8% mispricing
KALSHI_TAKER_FEE = 0.07  # ~7% of min(price, 1-price)

def _kalshi_fee(price: float) -> float:
    """Kalshi taker fee: ~7% of min(price, 1-price)."""
    return KALSHI_TAKER_FEE * min(price, 1.0 - price)

def _compute_fair_value(spot: float, strike: float, minutes_to_expiry: float,
                        vol: float, strike_type: str = "threshold") -> float | None:
    """
    Compute fair value of a binary option using simplified Black-Scholes.
    Returns P(spot > strike at expiry) for threshold contracts.
    """
    if spot <= 0 or strike <= 0 or minutes_to_expiry <= 0 or vol <= 0:
        return None

    # Convert vol to the timeframe
    hours = minutes_to_expiry / 60.0
    years = hours / (365.25 * 24)
    sigma_t = vol * math.sqrt(years)

    if sigma_t < 0.0001:
        # Basically no time left — binary outcome
        return 1.0 if spot > strike else 0.0

    # d2 from Black-Scholes (simplified, assuming no drift for short durations)
    d2 = math.log(spot / strike) / sigma_t

    # Normal CDF approximation
    fair = _norm_cdf(d2)
    return max(0.01, min(0.99, fair))

def _norm_cdf(x: float) -> float:
    """Fast normal CDF approximation (Abramowitz & Stegun)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
```

- [ ] **Step 2: Add _check_fair_value_directional function**

```python
def _check_fair_value_directional(market: dict, balance: float, spot_prices: dict) -> TradeDecision | None:
    """
    Fair value directional strategy for Kalshi fast-resolution contracts.
    Compares contract market price to fair value computed from spot.
    """
    from kalshi_feed import SERIES_TO_UNDERLYING

    venue = market.get("venue", "")
    if venue != "kalshi":
        return None

    # Need close_time and strike data
    close_time_str = market.get("close_time") or market.get("end_date")
    if not close_time_str:
        return None

    # Parse close time and compute minutes to expiry
    try:
        close_time = datetime.datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        minutes_to_expiry = (close_time - now).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None

    if minutes_to_expiry <= 2:  # Too close to expiry, risk of not filling
        return None
    if minutes_to_expiry > 24 * 60:  # Only trade fast-resolving (< 24h)
        return None

    # Find the underlying crypto
    event_ticker = market.get("event_ticker", "")
    market_id = market.get("market_id", "")
    underlying_id = None
    for series, crypto_id in SERIES_TO_UNDERLYING.items():
        if series in event_ticker or series in market_id:
            underlying_id = crypto_id
            break

    if not underlying_id:
        return None

    spot = spot_prices.get(underlying_id)
    if spot is None or spot <= 0:
        return None

    # Get strike from market data
    strike = market.get("cap_strike") or market.get("floor_strike")
    if strike is None or strike <= 0:
        return None

    vol = CRYPTO_VOL.get(underlying_id, 0.70)
    fair = _compute_fair_value(spot, strike, minutes_to_expiry, vol)
    if fair is None:
        return None

    # Get market prices
    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    fee = _kalshi_fee(yes_price)
    edge_yes = fair - (yes_price + fee)   # Edge from buying YES
    edge_no = (1.0 - fair) - ((1.0 - yes_price) + _kalshi_fee(1.0 - yes_price))  # Edge from buying NO

    if edge_yes > MIN_FAIR_VALUE_EDGE:
        direction = "YES"
        entry_price = yes_price
        confidence = min(fair, 0.95)
        edge = edge_yes
    elif edge_no > MIN_FAIR_VALUE_EDGE:
        direction = "NO"
        entry_price = 1.0 - yes_price
        confidence = min(1.0 - fair, 0.95)
        edge = edge_no
    else:
        return None

    amount = _kelly_size(confidence, entry_price, balance)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"],
        question=market.get("question", ""),
        direction=direction,
        confidence=confidence,
        reasoning=(f"FairVal: spot=${spot:,.0f} strike=${strike:,.0f} "
                   f"exp={minutes_to_expiry:.0f}m fair={fair:.3f} "
                   f"mkt={yes_price:.3f} edge={edge:.3f}"),
        strategy="fair_value_directional",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "fair_value": fair, "spot": spot,
                  "strike": strike, "minutes_to_expiry": minutes_to_expiry,
                  "vol": vol, "venue": "kalshi"},
    )
```

- [ ] **Step 3: Add _check_near_expiry_momentum function**

```python
NEAR_EXPIRY_HOURS = float(os.environ.get("RIVALCLAW_NEAR_EXPIRY_HOURS", "48"))
MIN_MOMENTUM_PRICE = float(os.environ.get("RIVALCLAW_MIN_MOMENTUM_PRICE", "0.78"))

def _check_near_expiry_momentum(market: dict, balance: float) -> TradeDecision | None:
    """
    Near-expiry momentum: bet on continuation when price is strongly directional
    and market is close to resolution. Works for both venues.
    """
    end_date_str = market.get("end_date") or market.get("close_time")
    if not end_date_str:
        return None

    try:
        end_date = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        hours_to_expiry = (end_date - now).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None

    if hours_to_expiry <= 0 or hours_to_expiry > NEAR_EXPIRY_HOURS:
        return None

    yes_price = market.get("yes_price", 0) or 0
    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    venue = market.get("venue", "polymarket")
    fee_fn = _kalshi_fee if venue == "kalshi" else _fee

    # Strong YES signal
    if yes_price >= MIN_MOMENTUM_PRICE:
        direction = "YES"
        entry_price = yes_price
        # Confidence scales with price and proximity to expiry
        time_boost = max(0, 1.0 - hours_to_expiry / NEAR_EXPIRY_HOURS) * 0.05
        confidence = min(yes_price + time_boost, 0.95)
        fee = fee_fn(entry_price)
        edge = confidence - (entry_price + fee)
    # Strong NO signal
    elif yes_price <= (1.0 - MIN_MOMENTUM_PRICE):
        direction = "NO"
        entry_price = 1.0 - yes_price
        time_boost = max(0, 1.0 - hours_to_expiry / NEAR_EXPIRY_HOURS) * 0.05
        confidence = min(entry_price + time_boost, 0.95)
        fee = fee_fn(entry_price)
        edge = confidence - (entry_price + fee)
    else:
        return None

    if edge <= MIN_EDGE:
        return None

    amount = _kelly_size(confidence, entry_price, balance)
    if amount is None:
        return None

    return TradeDecision(
        market_id=market["market_id"],
        question=market.get("question", ""),
        direction=direction,
        confidence=confidence,
        reasoning=(f"NearExpiry: {hours_to_expiry:.1f}h left, "
                   f"yes={yes_price:.3f}, edge={edge:.3f}"),
        strategy="near_expiry_momentum",
        amount_usd=amount,
        entry_price=entry_price,
        shares=amount / entry_price if entry_price > 0 else 0,
        decision_generated_at_ms=time.time() * 1000,
        metadata={"edge": edge, "hours_to_expiry": hours_to_expiry,
                  "yes_price": yes_price, "venue": venue},
    )
```

- [ ] **Step 4: Update analyze() to run all three strategies**

```python
import datetime

def analyze(markets: list[dict], wallet: dict, spot_prices: dict | None = None) -> list[TradeDecision]:
    """
    Main entry point. Runs all strategies on all markets.
    spot_prices: {coingecko_id: price_usd} from spot_feed, needed for fair_value strategy.
    """
    balance = wallet.get("balance", 1000.0)
    spot = spot_prices or {}
    decisions = []
    stats = {"integrity": 0, "arb": 0, "fair_value": 0, "near_expiry": 0}

    for market in markets:
        reason = _validate_market(market)
        if reason:
            stats["integrity"] += 1
            continue

        # Strategy 1: Cross-outcome arb (existing)
        d = _check_arbitrage(market, balance)
        if d:
            decisions.append(d)
            stats["arb"] += 1
            continue  # One signal per market

        # Strategy 2: Fair value directional (Kalshi fast-resolution)
        d = _check_fair_value_directional(market, balance, spot)
        if d:
            decisions.append(d)
            stats["fair_value"] += 1
            continue

        # Strategy 3: Near-expiry momentum (both venues)
        d = _check_near_expiry_momentum(market, balance)
        if d:
            decisions.append(d)
            stats["near_expiry"] += 1

    if stats["integrity"]:
        print(f"[rivalclaw/brain] Integrity rejected: {stats['integrity']}")
    print(f"[rivalclaw/brain] Signals: arb={stats['arb']} fv={stats['fair_value']} "
          f"expiry={stats['near_expiry']} (total={len(decisions)})")

    return sorted(decisions, key=lambda d: d.confidence, reverse=True)
```

- [ ] **Step 5: Commit**

```bash
git add trading_brain.py && git commit -m "feat: add fair_value_directional and near_expiry_momentum strategies"
```

---

### Task 6: Update polymarket_feed.py with venue field and expiry filtering

**Files:**
- Modify: `polymarket_feed.py`

- [ ] **Step 1: Add venue field to market dicts and DB inserts**

In `fetch_markets()`, add `"venue": "polymarket"` to each market dict (line ~130).
In the INSERT statement, add venue column.

- [ ] **Step 2: Add function to get near-expiry markets**

```python
def get_near_expiry_markets(hours=48):
    """Return markets expiring within the next N hours."""
    cutoff = (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).isoformat()
    now = datetime.datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT md.* FROM market_data md
            INNER JOIN (
                SELECT market_id, MAX(fetched_at) AS latest
                FROM market_data GROUP BY market_id
            ) l ON md.market_id = l.market_id AND md.fetched_at = l.latest
            WHERE md.end_date IS NOT NULL AND md.end_date > ? AND md.end_date < ?
        """, (now, cutoff)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
```

- [ ] **Step 3: Commit**

```bash
git add polymarket_feed.py && git commit -m "feat: add venue field and near-expiry filter to polymarket feed"
```

---

### Task 7: Update paper_wallet.py for multi-venue price tracking

**Files:**
- Modify: `paper_wallet.py:80-90` (get_state), `paper_wallet.py:158-216` (execute_trade)

- [ ] **Step 1: Update get_state() to use combined prices**

Replace the price fetching in `get_state()` (lines 83-88):

```python
def _get_all_latest_prices():
    """Combined latest prices from all venues."""
    try:
        import polymarket_feed
        prices = polymarket_feed.get_latest_prices()
    except Exception:
        prices = {}
    try:
        import kalshi_feed
        kalshi_prices = kalshi_feed.get_latest_prices()
        prices.update(kalshi_prices)
    except Exception:
        pass
    return prices
```

Update `get_state()` to call `_get_all_latest_prices()` instead of `polymarket_feed.get_latest_prices()`.

- [ ] **Step 2: Update execute_trade to record venue**

In `execute_trade()`, after the INSERT, add venue from the decision's metadata:

```python
# In the INSERT statement, add venue parameter
venue = (decision.metadata or {}).get("venue", "polymarket") if decision.metadata else "polymarket"
```

Add venue to the INSERT columns and values.

- [ ] **Step 3: Update check_stops to use combined prices**

In `check_stops()`, change to accept prices from all venues or fetch them internally using `_get_all_latest_prices()`.

- [ ] **Step 4: Commit**

```bash
git add paper_wallet.py && git commit -m "feat: multi-venue price tracking in paper wallet"
```

---

### Task 8: Update simulator.py for multi-feed orchestration

**Files:**
- Modify: `simulator.py:114-193` (run_loop)

- [ ] **Step 1: Update run_loop to fetch from both venues**

```python
def run_loop():
    sys.path.insert(0, str(Path(__file__).parent))
    import polymarket_feed as poly_feed
    import kalshi_feed
    import spot_feed
    import paper_wallet as wallet
    import trading_brain as brain
    import graduation as grad

    cycle_started_at_ms = time.time() * 1000
    cycle_started_iso = datetime.datetime.utcnow().isoformat()
    print(f"[rivalclaw] Run loop starting — {cycle_started_iso}")

    # 1. Fetch market data from both venues (timed)
    t0 = time.time()
    poly_markets = poly_feed.fetch_markets()
    kalshi_markets = kalshi_feed.fetch_markets()
    markets = poly_markets + kalshi_markets
    fetch_ms = (time.time() - t0) * 1000
    print(f"[rivalclaw] Fetched: {len(poly_markets)} Polymarket + {len(kalshi_markets)} Kalshi = {len(markets)} total")

    if not markets:
        print("[rivalclaw] No markets available. Skipping.")
        _log_cycle_metrics(cycle_started_iso, 0, 0, 0, 0, 0, fetch_ms, 0, 0,
                           (time.time() * 1000 - cycle_started_at_ms))
        return

    # 2. Get wallet state + spot prices
    state = wallet.get_state()
    spot_prices = spot_feed.get_spot_prices()
    print(f"[rivalclaw] Wallet: ${state['balance']:.2f} | Spots: {len(spot_prices)} cryptos")

    # 3. Analyze all markets (timed)
    t0 = time.time()
    decisions = brain.analyze(markets, state, spot_prices=spot_prices)
    analyze_ms = (time.time() - t0) * 1000
    print(f"[rivalclaw] Brain returned {len(decisions)} signals")

    # 4-7: Execute, stops, snapshot, metrics (same as before)
    ...
```

- [ ] **Step 2: Commit**

```bash
git add simulator.py && git commit -m "feat: multi-feed orchestration with Kalshi + spot prices"
```

---

### Task 9: Update cron to 2-minute interval

**Files:**
- System crontab

- [ ] **Step 1: Update crontab**

Change from `*/5` to `*/2`:

```bash
# Current: */5 * * * * cd /Users/nayslayer/rivalclaw && ...
# New:     */2 * * * * cd /Users/nayslayer/rivalclaw && ...
```

Run: `crontab -e` or update programmatically.

- [ ] **Step 2: Verify cron**

```bash
crontab -l | grep rivalclaw
```

Expected: `*/2 * * * *` interval.

- [ ] **Step 3: Commit updated docs**

Update CLAUDE.md cron section to reflect new interval.

---

### Task 10: End-to-end integration test

- [ ] **Step 1: Run one full cycle manually**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 run.py --run
```

Expected output should show:
- Polymarket markets fetched
- Kalshi markets fetched
- Spot prices loaded
- Brain signals from new strategies
- Any trades executed

- [ ] **Step 2: Verify data appears in DB**

```bash
cd /Users/nayslayer/rivalclaw && source venv/bin/activate && python3 -c "
import sqlite3
conn = sqlite3.connect('rivalclaw.db')
print('Kalshi markets:', conn.execute('SELECT COUNT(*) FROM market_data WHERE venue=\"kalshi\"').fetchone()[0])
print('Total trades:', conn.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0])
print('Kalshi trades:', conn.execute('SELECT COUNT(*) FROM paper_trades WHERE venue=\"kalshi\"').fetchone()[0])
rows = conn.execute('SELECT strategy, COUNT(*) FROM paper_trades GROUP BY strategy').fetchall()
for r in rows: print(f'  {r[0]}: {r[1]}')
"
```

- [ ] **Step 3: Verify Gonzoclaw dashboard renders the data**

The dashboard at `/Users/nayslayer/openclaw/dashboard/server.py` reads from `rivalclaw.db` using `_rivalclaw_state()`. It queries:
- `paper_trades` — our trades will show up automatically since they use the same schema
- `market_data` — our Kalshi data will show up (the dashboard doesn't filter by venue)
- `cycle_metrics` — cycle timing will show up
- `daily_pnl` — daily snapshots will show up

No dashboard changes needed — the existing schema is compatible. The new `strategy` values ("fair_value_directional", "near_expiry_momentum") will appear in the trades table alongside "arbitrage".

- [ ] **Step 4: Commit any final fixes**

```bash
git add -A && git commit -m "feat: complete multi-venue fast-resolution trading integration"
```

---

## Risk Notes

- **Kalshi API auth**: Using prod API with demo key — may get 401. If so, check if key is actually for prod.
- **CoinGecko rate limits**: Free tier = 30 calls/min. With 30s cache, we use 2/min. Safe.
- **Fair value edge**: 8% threshold is conservative. May need tuning down to 5% if not finding trades.
- **Near-expiry momentum**: 78% price threshold may be too high. Can lower to 70% if needed.
- **Execution sim**: Kalshi slippage may differ from Polymarket. Same assumptions for now.

## Tuning Parameters (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| RIVALCLAW_MIN_FV_EDGE | 0.08 | Min fair value mispricing to trade |
| RIVALCLAW_NEAR_EXPIRY_HOURS | 48 | Max hours to expiry for momentum |
| RIVALCLAW_MIN_MOMENTUM_PRICE | 0.78 | Min directional price for momentum |
| KALSHI_TAKER_FEE | 0.07 | Kalshi taker fee estimate |
