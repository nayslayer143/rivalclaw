---
project: rivalclaw
type: trading-agent
stack: [python, sqlite]
status: active
github: https://github.com/nayslayer143/rivalclaw
gitlab: https://gitlab.com/jordan291/rivalclaw
instance: RivalClaw
parent: openclaw
children: []
---

# RivalClaw

Lightweight, laser-focused arbitrage trading agent. Speed and execution reliability over complexity.

## What This Is

RivalClaw is a standalone arbitrage execution engine within the OpenClaw ecosystem. It runs independently but exports daily metrics compatible with OpenClaw's comparison framework. Optimizes for reliable, repeatable, execution-realistic arbitrage — not narrative intelligence or complex reasoning.

## Architecture

- **Cycle-based execution** — runs every 10-15 minutes scanning for arb opportunities
- **Single-strategy focus** — pure arbitrage, no multi-strategy complexity
- **Metrics export** — daily JSON contract compatible with OpenClaw and ArbClaw comparison
- **Risk management** — built-in drawdown limits, slippage tracking, false positive monitoring

## Key Files

| File/Dir | Purpose |
|----------|---------|
| `CLAUDE.md` | Agent instructions and architecture rules |
| `src/` | Core trading logic |
| `daily-update.sh` | Daily report generation + git push |

## Quick Start

```bash
git clone https://github.com/nayslayer143/rivalclaw.git
cd rivalclaw
cat CLAUDE.md  # Full architecture and rules
```

## Related Projects

| Project | Relationship | Repo |
|---------|-------------|------|
| OpenClaw | Parent — orchestrator | [GitHub](https://github.com/nayslayer143/openclaw) |
| ArbClaw | Sibling — minimal arb baseline | [GitHub](https://github.com/nayslayer143/arbclaw) |
| QuantumentalClaw | Sibling — signal fusion | [GitHub](https://github.com/nayslayer143/quantumentalclaw) |

## License

Private project.
