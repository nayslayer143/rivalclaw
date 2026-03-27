# RivalClaw

Testing a hunch: does my main trading system's architecture actually slow down arbitrage execution?

[OpenClaw](https://gitlab.com/jordan291/openclaw) (Clawmpson) runs 5 strategies, LLM analysis, a graduation engine, and a bunch of other stuff on a 30-minute cycle. That's great for complex trades, but for cross-outcome arb where mispricing windows close in minutes — all that machinery might be costing me alpha.

RivalClaw is the middle child in a three-way experiment:

| System | What it is | Cycle | Question it answers |
|--------|-----------|-------|-------------------|
| **ArbClaw** | 4 files, zero overhead | 5 min | What's the speed ceiling? |
| **RivalClaw** | Same architecture as Clawmpson, arb only | 5 min | Does the framework itself add lag? |
| **Clawmpson** | Full system, 5 strategies | 30 min | Does strategy contention matter? |

## How it works

RivalClaw keeps Clawmpson's exact control flow but strips everything that isn't arb:

```
fetch markets → analyze (arb only) → paper trade → check stops → maybe graduate
```

Same architecture shape. Same execution simulation (slippage, latency penalty, partial fills). Same graduation gates. Just fewer strategies competing for attention.

## The arb math

Identical to ArbClaw — same fee computation, same Kelly formula, same thresholds:
- Fee: 2% of min(price, 1-price) per leg
- Min edge: 0.5% after fees
- Kelly cap: 10% of balance

## What RivalClaw adds over ArbClaw

This is the "architectural weight" being measured:
- Execution simulation (50bps slippage, 0.2% latency penalty, 80-100% fill rate)
- Full daily PnL accounting with ROI, Sharpe, max drawdown
- Graduation gates (7-day window, same thresholds as Clawmpson)
- Mark-to-market balance derivation
- Integrity guards (stale timestamps, impossible prices, sum sanity checks)
- Per-cycle timing instrumentation

## Key metric

`signal_to_trade_latency_ms` — how fast does each system go from seeing an opportunity to placing a trade? That's the whole point.

## Stack

Python, SQLite, Polymarket Gamma API. ~880 lines across 6 files.

## Status

Paper trading experiment running March 24 – April 7, 2026. Part of the [OpenClaw](https://gitlab.com/jordan291/openclaw) ecosystem.
