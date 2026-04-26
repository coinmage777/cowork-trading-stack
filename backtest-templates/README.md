# backtest-templates

ccxt OHLCV 기반 백테스트 프레임워크 + 검증된 전략 4종 (RSI70 / SuperTrend / Mean-Rev / BB-Upper Short). Pine 원본 → Python 재구성. Minara 베이스라인 비교용.

## 검증 완료 전략 (Minara DB 기준)

| 전략 | TF | WR | Sharpe | MDD | n trades | 신뢰도 |
|------|----|----|--------|-----|----------|--------|
| **SuperTrend BTC 1D** | 1D | (extreme low freq) | — | — | 4 (4y) | HIGH (PF 8.98) |
| **RSI>70 BTC 4H** | 4H | (1.85 Sharpe) | 1.85 | 14.8% | 142 | HIGH |
| **RSI>70 ETH 4H** | 4H | 34% | 0.70 | 20% | 233 | MED |
| **BB Upper Short SOL 1H** | 1H | — | — | — | — | MED (10% hard stop overlay 추가) |
| **Mean Rev BTC 15m** | 15m | — | (APR 204%) | — | — | LOW (fee drag 검증 필요) |

## 파일 구조

```
backtest-templates/
├── backtester.py                        — 메인 BacktestEngine
├── backtest_supertrend_btc_1d.py        — SuperTrend 4 trades/4y 실행
├── backtest_supertrend_btc_1d.md        — 결과 (markdown)
├── backtest_rsi70_btc_4h.py             — RSI>70 BTC 4H
├── backtest_rsi70_eth_4h.py             — RSI>70 ETH 4H
├── backtest_bb_upper_sol_1h.py          — BB Upper Short SOL 1H
├── backtest_mean_rev_btc_15m.py         — Mean Reversion BTC 15m
├── paper_trader_rsi70_btc_4h.py         — RSI70 paper trader (live shadow)
├── paper_trader_rsi70_eth_4h.py         — RSI70 paper trader ETH
└── data/
    └── (OHLCV cache)
```

## 사용 예시

```bash
# OHLCV 다운로드 + 백테스트 한 번에
python backtest_rsi70_btc_4h.py

# 결과 (예시)
# Total trades: 142
# Win rate: 49.3%
# Profit factor: 1.68
# Sharpe: 1.85
# Max DD: 14.8%
# Net P&L: +$xxx (in $1 size)
```

## Paper trader (live shadow)

실시간 가격 폴링 → 시그널 발생 시 가상 진입/청산:
```bash
python paper_trader_rsi70_btc_4h.py
# data/paper_rsi70_journal.jsonl 에 진입/청산 기록
```

진입 0건이면 RSI 70+ 시그널이 발생 안 한 것 (4H 캔들 RSI 70+ 빈도는 월 4-8회 추정).

## 의존성

- `ccxt` (OHLCV fetch)
- `pandas`, `numpy`
- `matplotlib` (차트)

## 환경변수

```env
# (옵션) ccxt 거래소별 API key
BINANCE_API_KEY=<...>
BINANCE_SECRET=<...>
```

API key 없이도 OHLCV public endpoint로 작동.

## 추가 전략 후보 (백로그)

- Stoikov-Avellaneda MM (Polymarket bot에 통합)
- Bayesian directional prior (Polymarket bot에 통합)
- Pair trading z-score (`strategy-templates/pair_trader.py`)
- Funding rate arbitrage (`cross-venue-arb-scanner/funding_arb.py`)
