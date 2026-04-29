# RSI>70 Continuation BTC 4H — Backtest vs Minara Claim

**Data**: Binance USDM `BTC/USDT:USDT` 4H  |  **Bars**: 13240 (~6년)
**Strategy**: enter LONG on RSI(14) cross above 70, exit on cross back below 70. Long-only, no SL.
**Costs**: fee 0.040%/side, slippage 0.050%/side → 0.18% round-trip drag

## Verdict: **REPLICATES** (3/4 metrics within tolerance)

## Minara Claim vs Actual (1460-day window — apples-to-apples)

| Metric | Minara | Actual (4Y) | Match | Full (6Y) |
|---|---|---|---|---|
| Trades | 142 | 148 | OK | 240 |
| Win Rate | 35.2% | 33.11% | OK | 31.25% |
| Sharpe (daily ann.) | 1.85 | 2.751 | DIFF | 0.719 |
| MDD | 14.8% | 15.16% | OK | 15.4% |
| Total Return | 99.7% | 58.82% | n/a | 81.07% |
| Profit Factor | n/a | 1.627 | n/a | 1.502 |

_Per-trade Sharpe (full, alt)_: 0.111

## Yearly Breakdown (% on notional)

| Year | Trades | Wins | WR | % on Notional |
|---|---|---|---|---|
| Y1 | 43 | 12 | 27.91% | +18.59% |
| Y2 | 43 | 11 | 25.58% | -3.74% |
| Y3 | 25 | 8 | 32.0% | -3.67% |
| Y4 | 34 | 16 | 47.06% | +50.45% |
| Y5 | 52 | 15 | 28.85% | +18.15% |
| Y6 | 27 | 10 | 37.04% | +10.73% |
| Y7 | 16 | 3 | 18.75% | -9.43% |

## Aggregate Stats

- **trades**: 240
- **wins**: 75
- **losses**: 165
- **wr_pct**: 31.25
- **profit_factor**: 1.502
- **avg_win_pct**: +3.23%
- **avg_loss_pct**: -0.98%
- **avg_bars_held**: 4.36
- **total_pct_on_notional**: +81.07%
- **max_win_streak**: 4
- **max_loss_streak**: 12
- **mdd_pct_of_notional**: 15.4%
- **sharpe_daily_ann**: 0.719
- **sharpe_per_trade**: 0.111

## Per-Trade Summary

총 240 거래. 승률 31.25%, profit factor 1.5 (loss는 작고 frequent, win은 큰 trend continuation 캡처). 평균 보유 ~4.36 bars (~17h). 최장 연속 패배 12회. Bull market 구간에서 큰 win 확보, 횡보/약세에서 잦은 작은 loss 누적되는 전형적 trend follower 곡선.
