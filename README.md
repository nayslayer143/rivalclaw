# RivalClaw

Architecture-faithful arb-only sibling of Mirofish. Part of a three-way comparison experiment testing whether Clawmpson's trading architecture introduces execution lag on arbitrage opportunities.

## The Experiment

Three systems run the same cross-outcome arb logic against the same Polymarket API on the same machine:

| System | Architecture | Cron | Purpose |
|--------|-------------|------|---------|
| **ArbClaw** | 4 files, no overhead | */5 | Speed baseline — what does zero architecture cost? |
| **RivalClaw** | Mirofish skeleton, arb-only | */5 | Architecture test — does the framework itself add lag? |
| **Clawmpson** | Full Mirofish, 5 strategies | */30 | Production baseline — does strategy contention matter? |

## Architecture

RivalClaw preserves Mirofish's control flow shape while stripping non-arb organs:

```
simulator.run_loop()
  -> polymarket_feed.fetch_markets()     # configurable cache/categories
  -> trading_brain.analyze()             # arb-only + integrity guards
  -> paper_wallet.execute_trade()        # frozen Mirofish execution sim
  -> paper_wallet.check_stops()          # same SL/TP + expiry logic
  -> graduation.maybe_snapshot()         # same 4 graduation gates
```

| File | Lines | Preserves from Mirofish |
|------|-------|------------------------|
| simulator.py | ~160 | run_loop orchestration, migration, cycle metrics |
| trading_brain.py | ~130 | TradeDecision, arb detection, Kelly, integrity guards |
| paper_wallet.py | ~280 | Execution sim, stops, mark-to-market, balance derivation |
| polymarket_feed.py | ~160 | Gamma API, SQLite cache, price parsing |
| graduation.py | ~120 | 4 graduation gates, daily snapshot |
| run.py | ~30 | CLI entry point |

## Arb Logic Parity

Arb math is identical to ArbClaw — same fee computation, same Kelly formula, same thresholds:
- Fee: 2% of min(price, 1-price) per leg
- Min edge: 0.5% after fees
- Kelly cap: 10% of balance

## What RivalClaw Adds Over ArbClaw

These are the "architectural weight" being measured:
- Execution simulation (50bps slippage + 0.2% latency penalty + 80-100% fill)
- Full daily_pnl accounting with ROI, Sharpe, max drawdown
- Graduation gates (7-day window, same thresholds as Clawmpson)
- Mark-to-market balance derivation
- Integrity guards (stale timestamps, impossible prices, sum sanity)
- Per-cycle timing instrumentation (fetch_ms, analyze_ms, wallet_ms)
- experiment_id / instance_id on all records

## Key Metrics

Granular latency decomposition per trade:
- `cycle_started_at_ms` — when the cron cycle began
- `decision_generated_at_ms` — when the brain produced the signal
- `trade_executed_at_ms` — when the wallet committed the trade
- `signal_to_trade_latency_ms` — decision to execution delta

Per-cycle overhead measurement:
- `fetch_ms` / `analyze_ms` / `wallet_ms` / `total_cycle_ms`

## Config Toggles

| Env Var | Default | Purpose |
|---------|---------|---------|
| RIVALCLAW_FETCH_MODE | fresh | "fresh" or "cache_ok" |
| RIVALCLAW_CATEGORIES | "" | "" = all, or "crypto,politics" |
| RIVALCLAW_EXECUTION_SIM | 1 | "1" = on, "0" = off |
| RIVALCLAW_EXPERIMENT_ID | arb-bakeoff-2026-03 | Experiment tracking |
| RIVALCLAW_INSTANCE_ID | rivalclaw | Instance tracking |

## Wallet Rules (frozen parity with Mirofish)

- Starting capital: $1,000
- Max position: 10%
- Stop-loss: -20%
- Take-profit: +50%
- Slippage: 50bps
- Latency penalty: 0.2%
- Fill rate: 80-100%

## Graduation Gates (frozen parity with Clawmpson)

- Min history: 7 days
- 7-day ROI > 0%
- Win rate > 55%
- Sharpe > 1.0
- Max drawdown < 25%

## Status

**Experiment start:** 2026-03-24
**Experiment end:** 2026-04-07
