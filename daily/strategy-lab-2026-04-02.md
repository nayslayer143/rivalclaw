# Strategy Lab Report — 2026-04-02

## 1. System Health

| Status | Count |
|--------|-------|
| production | 3 |
| degraded | 5 |
| **Total** | **8** |

## 2. Key Findings

- **Top performer:** polymarket_convergence — $+99306.70 PnL, 14% WR
- **Worst performer:** forecast_delta — $-2201.81 PnL, 28% WR
- **Drift detected:** cross_strike_arb — degraded (recent avg $-14.87 vs older avg $9.11)
- **Drift detected:** expiry_acceleration — degraded (recent avg $-41.43 vs older avg $0.18)
- **Drift detected:** liquidity_fade — degraded (recent avg $-36.58 vs older avg $19.91)
- **Drift detected:** forecast_delta — degraded (recent avg $-17.56 vs older avg $10.09)
- **Drift detected:** election_field_arb — degraded (recent avg $-0.09 vs older avg $1.84)
- **Regime calm:** 31651 trades, $+204105.17 PnL
- **Signal calibration:** overconfident (confidence 0.86 vs actual 0.13)

## 3. Candidate Hypotheses

- No hypotheses generated this cycle

## 4. Evaluation Results

- No backtests completed

## 5. Promotions / Demotions

- No state changes today

## 6. Memory Updates

- No lessons recorded yet

## 7. Next Actions

- Investigate 5 drifting strategy(ies)
- Missed: Will the temp in NYC be above 41.99° on Mar 28, 20 (Δ100%)
- Missed: Ethereum price at Mar 25, 2026 at 8pm EDT? (Δ99%)
- Missed: Ethereum price at Mar 25, 2026 at 8pm EDT? (Δ99%)
