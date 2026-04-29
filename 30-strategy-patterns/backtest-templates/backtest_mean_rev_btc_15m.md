# Optimized BTC Mean Reversion (RSI 20/65) 15m — Backtest vs Minara Claim

**Data**: Binance USDM `BTC/USDT:USDT` 15m  |  **Bars**: 35,340 (~1년)
**Costs**: fee 0.040%/side, slip 0.050%/side → **0.18% round-trip**.
**Rules**: Long entry RSI<20 & close>EMA200 & %K<25. Short entry symmetric. Exits: RSI 20/65 flip, %K 25/75 flip, TP +6%, SL -4%.

## Verdict: **DOES NOT REPLICATE** (1/3 primary metrics within tolerance)

### Important context
- **Long-only is not viable** on this timeframe with these rules. In 35,141 warm 15m bars over 1 year, the triple-trigger (RSI<20 & close>EMA200 & %K<25) fired only **1** time(s). Minara's 16-trades-in-90d claim requires shorts.
- Primary comparison below uses **long+short** (matches scaffolding + Minara's actual published Pine source).

## Minara Claim vs Actual (90-day apples-to-apples)

| Metric | Minara (90d, HL fees) | Long+Short Actual (90d) | Long-only (90d) | Long+Short Full 1Y |
|---|---|---|---|---|
| Trades | 16 | 17 | 0 | 66 |
| APR | +204.6% | -22.61% | +0.00% | -7.68% |
| Sharpe (daily ann.) | >4 | -1.445 | 0 | -0.8 |
| 90d WR | n/a | 58.82% | 0% | 56.06% |

## Condition Diagnostics (1 year of 15m bars)

- Warm bars: 35,141
- RSI<20: 171 (0.49%) | RSI>65: 3,014 (8.58%)
- Close>EMA200: 17,549 (49.94%) | Close<EMA200: 17,592 (50.06%)
- Stoch %K<25: 7,330 | %K>75: 8,344
- **LONG triple-trigger bars**: 1
- **SHORT triple-trigger bars**: 188

_Note: Asymmetric entries (RSI<20 strict vs RSI>65 loose) cause an ~188:1 short-to-long imbalance. The "mean reversion with the trend" thesis fails because RSI<20 on 15m virtually only prints during dumps, by which time price is already below EMA200._

## Exit Reason Breakdown (Long+Short, full 1Y)

| Reason | Count |
|---|---|
| tp_long | 0 |
| sl_long | 0 |
| rsi_exit_long | 0 |
| stoch_exit_long | 1 |
| tp_short | 0 |
| sl_short | 2 |
| rsi_exit_short | 0 |
| stoch_exit_short | 63 |

_All shorts close on `stoch_exit_short` almost immediately. The RSI>75 threshold flips back to %K<25 within a few bars of any short entry, pre-empting TP/SL. **Avg hold 21.6 bars (~5.4h)**. Effective reward = small mean-reversion wiggle minus 0.18% round-trip fee._

## Fee Drag Analysis (CRITICAL)

- **Fee drag vs notional**: 5.28% per year
- Round-trip cost per trade ≈ 0.18%; 66 trades × 0.18% = **11.88%** friction
- Avg gross trade PnL: -0.037% vs required 0.18% — fees eat the edge

**Fees take 214.4% of the magnitude of gross PnL.**

## Yearly Breakdown (Long+Short, % on notional)

| Year | Trades | Wins | WR | % on Notional |
|---|---|---|---|---|
| Y1 | 43 | 23 | 53.49% | -4.89% |
| Y2 | 23 | 14 | 60.87% | -2.85% |

## Aggregate Stats — Long+Short (full 1Y)

- **trades**: 66
- **wins**: 37
- **losses**: 29
- **wr_pct**: 56.06
- **profit_factor**: 0.723
- **avg_win_pct**: +0.55%
- **avg_loss_pct**: -0.97%
- **avg_bars_held**: 21.62
- **total_pct_on_notional**: -7.74%
- **fee_drag_pct_of_notional**: 5.28%
- **apr_pct**: -7.68%
- **mdd_pct_of_notional**: 11.07%
- **sharpe_daily_ann**: -0.8
- **days_covered**: 368.1

## Aggregate Stats — Long-only (full 1Y, reference)

- **trades**: 1
- **wins**: 1
- **wr_pct**: 100.0
- **profit_factor**: inf
- **avg_win_pct**: +0.46%
- **avg_bars_held**: 15.0
- **total_pct_on_notional**: +0.46%
- **fee_drag_pct_of_notional**: 0.08%
- **apr_pct**: +0.46%
- **mdd_pct_of_notional**: 0.0%
- **sharpe_daily_ann**: 0.997
- **days_covered**: 368.1

## Per-Trade Summary

총 66 거래 (전부 short, long 1건). 평균 보유 ~21.6 bars (~5.4h). 거래당 PnL 분포는 매우 좁아 -1.7%~+2.9% 범위에 집중. 가장 큰 단일 거래도 +2.86% / -4.18% 수준이라 round-trip 0.18% fee 대비 마진이 부족. stoch_exit_short 가 압도적 다수.

## Recommendation

**Do not deploy.** Three independent failures:

1. **Long-only unviable**: RSI<20 on 15m fires 0.49% of bars; combined with close>EMA200 (a trend filter that's false *exactly* when RSI dumps) the triple-trigger fired 1× in a full year.
2. **Long+short live-trades but loses money**: 90d net -5.57%, 1Y net -7.74%. Minara claimed +204% APR — we measure -7.68%.
3. **Fees are structural**: round-trip 0.18% vs avg trade magnitude <0.5% means break-even is impossible without edge that doesn't exist in this ruleset on this data.

Minara's +204% APR claim was a small-sample fluke (possibly over-fit in a range-bound window). Out-of-sample over 1년 it inverts. If there's interest in a mean-reversion play on BTC 15m, at minimum: (a) relax RSI<20 to <30, (b) drop the %K<25 third filter (it makes bars ultra-rare), (c) benchmark against a simple RSI(2) contrarian baseline, (d) require out-of-sample WR & PF > 1 *after* fees in ≥ 2 independent 6-month windows.
