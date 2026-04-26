# Optimized BTC Mean Reversion (RSI 20/65) 15m — Backtest vs Minara Claim

**Data**: Binance USDM `BTC/USDT:USDT` 15m  |  **Range**: 2025-04-21 → 2026-04-24  |  **Bars**: 35,340
**Costs**: fee 0.040%/side, slip 0.050%/side → **0.18% round-trip**. Notional $10,000.
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
| 90d Net PnL | n/a | $-557.48 | $0.00 | $-774.19 |
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

- **Gross PnL**: $-246.28
- **Total fees+slip**: $527.93
- **Net PnL**: $-774.19
- **Fee drag vs notional**: 5.28% per year
- Round-trip cost per trade ≈ 0.18%; 66 trades × 0.18% = **11.88%** friction
- Avg gross trade PnL: -0.037% vs required 0.18% — fees eat the edge

**Fee takes 214.4% of the magnitude of gross PnL.** 

## Yearly Breakdown (Long+Short)

| Year | Trades | Wins | WR | Net PnL | Fees | % on Notional |
|---|---|---|---|---|---|---|
| 2025 | 43 | 23 | 53.49% | $-489.43 | $343.97 | -4.89% |
| 2026 | 23 | 14 | 60.87% | $-284.76 | $183.96 | -2.85% |

## Aggregate Stats — Long+Short (full 1Y)

- **trades**: 66
- **wins**: 37
- **losses**: 29
- **wr_pct**: 56.06
- **gross_win**: 2024.47
- **gross_loss**: 2798.66
- **profit_factor**: 0.723
- **avg_win**: 54.72
- **avg_loss**: -96.51
- **best_trade_pnl**: 323.21
- **best_trade_pct**: 3.232
- **worst_trade_pnl**: -418.24
- **worst_trade_pct**: -4.182
- **avg_bars_held**: 21.62
- **total_net_pnl**: -774.19
- **total_pct_on_notional**: -7.74
- **total_fees**: 527.93
- **gross_pnl**: -246.28
- **fee_drag_pct_of_notional**: 5.28
- **apr_pct**: -7.68
- **mdd_dollar**: 1107.08
- **mdd_pct_of_notional**: 11.07
- **sharpe_daily_ann**: -0.8
- **days_covered**: 368.1

## Aggregate Stats — Long-only (full 1Y, reference)

- **trades**: 1
- **wins**: 1
- **losses**: 0
- **wr_pct**: 100.0
- **gross_win**: 46.12
- **gross_loss**: 0
- **profit_factor**: inf
- **avg_win**: 46.12
- **avg_loss**: 0.0
- **best_trade_pnl**: 46.12
- **best_trade_pct**: 0.461
- **worst_trade_pnl**: 46.12
- **worst_trade_pct**: 0.461
- **avg_bars_held**: 15.0
- **total_net_pnl**: 46.12
- **total_pct_on_notional**: 0.46
- **total_fees**: 8.02
- **gross_pnl**: 54.14
- **fee_drag_pct_of_notional**: 0.08
- **apr_pct**: 0.46
- **mdd_dollar**: 0.0
- **mdd_pct_of_notional**: 0.0
- **sharpe_daily_ann**: 0.997
- **days_covered**: 368.1

## Per-Trade Log — Long+Short (first 50)

| # | Side | Entry | Entry $ | Exit | Exit $ | Reason | Bars | Net PnL | % |
|---|---|---|---|---|---|---|---|---|---|
| 1 | short | 2025-05-05 18:30 | 94,752.60 | 2025-05-05 20:15 | 94,195.47 | stoch_exit_short | 7 | $50.77 | +0.51% |
| 2 | short | 2025-05-24 10:30 | 108,853.65 | 2025-05-24 14:15 | 108,705.43 | stoch_exit_short | 15 | $5.61 | +0.06% |
| 3 | short | 2025-05-31 13:00 | 104,017.97 | 2025-06-01 00:15 | 104,371.46 | stoch_exit_short | 45 | $-41.97 | -0.42% |
| 4 | short | 2025-06-06 04:30 | 102,627.46 | 2025-06-06 18:30 | 104,643.70 | stoch_exit_short | 56 | $-204.38 | -2.04% |
| 5 | short | 2025-06-12 17:30 | 108,179.98 | 2025-06-12 20:00 | 106,488.72 | stoch_exit_short | 10 | $148.28 | +1.48% |
| 6 | short | 2025-06-13 23:30 | 105,932.71 | 2025-06-14 01:15 | 105,530.84 | stoch_exit_short | 7 | $29.92 | +0.30% |
| 7 | short | 2025-06-18 04:45 | 105,346.90 | 2025-06-18 07:00 | 104,861.30 | stoch_exit_short | 9 | $38.08 | +0.38% |
| 8 | short | 2025-06-21 08:45 | 103,715.42 | 2025-06-21 13:45 | 103,580.16 | stoch_exit_short | 20 | $5.04 | +0.05% |
| 9 | short | 2025-06-23 05:45 | 101,660.04 | 2025-06-23 09:30 | 101,485.02 | stoch_exit_short | 15 | $9.21 | +0.09% |
| 10 | short | 2025-07-02 04:30 | 106,151.10 | 2025-07-02 12:15 | 107,324.54 | stoch_exit_short | 31 | $-118.50 | -1.19% |
| 11 | short | 2025-07-19 04:00 | 118,260.04 | 2025-07-19 06:30 | 118,130.24 | stoch_exit_short | 10 | $2.97 | +0.03% |
| 12 | short | 2025-07-25 19:30 | 116,894.72 | 2025-07-26 02:00 | 117,348.04 | stoch_exit_short | 26 | $-46.76 | -0.47% |
| 13 | short | 2025-08-03 03:30 | 113,601.97 | 2025-08-03 22:15 | 114,120.23 | stoch_exit_short | 75 | $-53.60 | -0.54% |
| 14 | short | 2025-08-15 04:15 | 119,063.84 | 2025-08-15 05:30 | 118,717.83 | stoch_exit_short | 5 | $21.05 | +0.21% |
| 15 | short | 2025-08-17 04:15 | 117,663.14 | 2025-08-17 12:45 | 118,168.35 | stoch_exit_short | 34 | $-50.92 | -0.51% |
| 16 | short | 2025-08-18 17:15 | 116,411.76 | 2025-08-19 00:00 | 116,282.81 | stoch_exit_short | 27 | $3.07 | +0.03% |
| 17 | short | 2025-08-22 02:15 | 113,398.37 | 2025-08-22 09:15 | 112,979.66 | stoch_exit_short | 28 | $28.91 | +0.29% |
| 18 | short | 2025-08-26 19:15 | 111,003.77 | 2025-08-27 01:00 | 111,408.28 | stoch_exit_short | 23 | $-44.43 | -0.44% |
| 19 | short | 2025-08-31 00:30 | 109,097.62 | 2025-08-31 04:15 | 108,845.50 | stoch_exit_short | 15 | $15.10 | +0.15% |
| 20 | short | 2025-08-31 17:00 | 108,828.96 | 2025-08-31 23:00 | 108,689.42 | stoch_exit_short | 24 | $4.82 | +0.05% |
| 21 | short | 2025-09-07 01:15 | 110,393.68 | 2025-09-07 06:45 | 110,513.73 | stoch_exit_short | 22 | $-18.87 | -0.19% |
| 22 | short | 2025-09-20 07:30 | 115,811.37 | 2025-09-20 09:45 | 115,663.50 | stoch_exit_short | 9 | $4.76 | +0.05% |
| 23 | short | 2025-09-20 15:00 | 115,941.90 | 2025-09-20 17:30 | 115,873.71 | stoch_exit_short | 10 | $-2.12 | -0.02% |
| 24 | short | 2025-09-23 08:45 | 113,031.36 | 2025-09-23 10:15 | 112,918.13 | stoch_exit_short | 6 | $2.01 | +0.02% |
| 25 | short | 2025-09-26 18:15 | 110,081.73 | 2025-09-26 19:30 | 109,086.52 | stoch_exit_short | 5 | $82.37 | +0.82% |
| 26 | short | 2025-09-27 22:00 | 109,512.82 | 2025-09-28 01:15 | 109,495.92 | stoch_exit_short | 13 | $-6.46 | -0.07% |
| 27 | long | 2025-09-30 09:45 | 112,799.47 | 2025-09-30 13:30 | 113,410.17 | stoch_exit_long | 15 | $46.12 | +0.46% |
| 28 | short | 2025-10-12 06:00 | 111,710.22 | 2025-10-12 09:00 | 111,191.87 | stoch_exit_short | 12 | $38.38 | +0.38% |
| 29 | short | 2025-10-12 15:15 | 113,043.45 | 2025-10-13 03:45 | 114,830.99 | stoch_exit_short | 50 | $-166.07 | -1.66% |
| 30 | short | 2025-10-14 15:45 | 112,766.59 | 2025-10-15 00:15 | 112,836.39 | stoch_exit_short | 34 | $-14.19 | -0.14% |
| 31 | short | 2025-10-17 21:00 | 107,281.33 | 2025-10-17 23:45 | 106,431.79 | stoch_exit_short | 11 | $71.16 | +0.71% |
| 32 | short | 2025-10-29 07:45 | 113,455.14 | 2025-10-29 09:00 | 112,870.11 | stoch_exit_short | 5 | $43.54 | +0.43% |
| 33 | short | 2025-10-31 00:00 | 108,563.89 | 2025-10-31 08:00 | 109,227.59 | stoch_exit_short | 32 | $-69.11 | -0.69% |
| 34 | short | 2025-11-05 12:30 | 102,612.77 | 2025-11-05 21:15 | 103,584.77 | stoch_exit_short | 35 | $-102.69 | -1.03% |
| 35 | short | 2025-11-15 03:15 | 96,570.79 | 2025-11-15 08:30 | 95,924.04 | stoch_exit_short | 21 | $58.95 | +0.59% |
| 36 | short | 2025-11-16 10:00 | 96,431.56 | 2025-11-16 11:30 | 95,586.07 | stoch_exit_short | 6 | $79.64 | +0.80% |
| 37 | short | 2025-11-18 15:30 | 92,647.25 | 2025-11-18 20:30 | 92,807.58 | stoch_exit_short | 20 | $-25.30 | -0.25% |
| 38 | short | 2025-11-19 23:00 | 91,247.95 | 2025-11-20 06:45 | 91,984.07 | stoch_exit_short | 31 | $-88.64 | -0.89% |
| 39 | short | 2025-11-22 22:45 | 84,978.09 | 2025-11-23 06:45 | 85,862.71 | stoch_exit_short | 32 | $-112.06 | -1.12% |
| 40 | short | 2025-12-14 00:30 | 90,378.79 | 2025-12-14 04:45 | 90,204.38 | stoch_exit_short | 17 | $11.29 | +0.11% |
| 41 | short | 2025-12-15 01:45 | 89,144.51 | 2025-12-15 11:45 | 89,650.80 | stoch_exit_short | 40 | $-64.77 | -0.65% |
| 42 | short | 2025-12-16 10:30 | 87,056.85 | 2025-12-17 00:30 | 87,446.50 | stoch_exit_short | 56 | $-52.74 | -0.53% |
| 43 | short | 2025-12-27 21:45 | 87,579.69 | 2025-12-27 22:45 | 87,570.06 | stoch_exit_short | 4 | $-6.90 | -0.07% |
| 44 | short | 2026-01-01 12:00 | 87,915.82 | 2026-01-02 02:15 | 88,433.49 | stoch_exit_short | 57 | $-66.86 | -0.67% |
| 45 | short | 2026-01-08 17:00 | 91,011.97 | 2026-01-08 20:15 | 90,595.48 | stoch_exit_short | 13 | $37.74 | +0.38% |
| 46 | short | 2026-01-10 10:45 | 90,750.00 | 2026-01-10 14:00 | 90,621.09 | stoch_exit_short | 13 | $6.20 | +0.06% |
| 47 | short | 2026-01-21 05:30 | 89,848.75 | 2026-01-21 07:30 | 89,225.89 | stoch_exit_short | 8 | $61.30 | +0.61% |
| 48 | short | 2026-01-21 15:00 | 90,259.35 | 2026-01-21 16:45 | 87,606.08 | stoch_exit_short | 7 | $285.84 | +2.86% |
| 49 | short | 2026-01-23 14:15 | 89,593.38 | 2026-01-23 19:45 | 89,983.27 | stoch_exit_short | 22 | $-51.50 | -0.52% |
| 50 | short | 2026-02-02 11:30 | 77,757.90 | 2026-02-02 19:00 | 78,519.54 | stoch_exit_short | 30 | $-105.91 | -1.06% |

_... 16 more trades in JSON_

## Recommendation

**Do not deploy.** Three independent failures:

1. **Long-only unviable**: RSI<20 on 15m fires 0.49% of bars; combined with close>EMA200 (a trend filter that's false *exactly* when RSI dumps) the triple-trigger fired 1× in a full year.
2. **Long+short live-trades but loses money**: 90d net -5.57%, 1Y net -7.74%. Minara claimed +204% APR — we measure -7.68%.
3. **Fees are structural**: round-trip 0.18% vs avg trade magnitude <0.5% means break-even is impossible without edge that doesn't exist in this ruleset on this data.

Minara's +204% APR on 16 trades in 90d is a small-sample fluke (possibly over-fit in a 2024 range-bound window). Out-of-sample (apr 2025 - apr 2026) it inverts. If there's interest in a mean-reversion play on BTC 15m, at minimum: (a) relax RSI<20 to <30, (b) drop the %K<25 third filter (it makes bars ultra-rare), (c) benchmark against a simple RSI(2) contrarian baseline, (d) require out-of-sample WR & PF > 1 *after* fees in ≥ 2 independent 6-month windows.
