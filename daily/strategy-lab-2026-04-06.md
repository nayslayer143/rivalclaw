# Strategy Lab Report — 2026-04-06

## 1. System Health

| Status | Count |
|--------|-------|
| production | 3 |
| degraded | 5 |
| **Total** | **8** |

## 2. Key Findings

- **Top performer:** polymarket_convergence — $+99306.70 PnL, 14% WR
- **Worst performer:** forecast_delta — $-2201.81 PnL, 28% WR
- **Regime calm:** 31651 trades, $+204105.17 PnL
- **Signal calibration:** overconfident (confidence 0.86 vs actual 0.13)

## 3. Candidate Hypotheses

- No hypotheses generated this cycle

## 4. Evaluation Results

- No backtests completed

## 5. Promotions / Demotions

- No state changes today

## 6. Memory Updates

- [?] 
- [?] 
- [?] 
- [?] 
- [?] 

## 7. Next Actions

- Missed: Will the temp in NYC be above 41.99° on Mar 28, 20 (Δ100%)
- Missed: Ethereum price at Mar 25, 2026 at 8pm EDT? (Δ99%)
- Missed: Ethereum price at Mar 25, 2026 at 8pm EDT? (Δ99%)
- Continue data collection for next research cycle

## 8. Weekly Cemetery Review

### Degraded / Retired Strategies

- **fair_value_directional_v1.0** (degraded) — initial production version — proven 50% WR, 2.7x ratio
- **spot_momentum_v1.0** (degraded) — initial production version — ride crypto trends into lagging contracts
- **cross_strike_arb_v1.0** (degraded) — initial production version — bracket sum deviation from 1.0
- **mean_reversion_v1.0** (degraded) — initial production version — bet against crowd near 0.50, 15-min crypto
- **time_decay_v1.0** (degraded) — initial production version — proven 5.1x ratio, sell overpriced near-expiry

### Recurring Failure Patterns

- **unknown**: 26 failures — 


### Complexity Check

- Production: 3 strategies
- Shadow: 0 candidates
- Probation: 0 candidates
- Cemetery: 5 strategies
- **Total variants: 8** (watch for >20 = complexity creep)
