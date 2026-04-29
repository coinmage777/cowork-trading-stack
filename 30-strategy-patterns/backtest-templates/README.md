# backtest-templates

> 한 줄 요약: ccxt OHLCV 기반 백테스트 프레임워크 + 검증된 4종 전략 (RSI70 / SuperTrend / Mean-Rev / BB-Upper Short). Pine 원본 → Python 재구성, Minara DB 베이스라인 비교용입니다.

## 의존성 (Dependencies)

- `ccxt` (OHLCV fetch)
- `pandas`, `numpy`
- `matplotlib` (차트)

API key 없이도 OHLCV public endpoint 로 작동합니다.

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "TradingView Pine 으로 짜인 RSI>70 short, SuperTrend 1D, BB upper short, mean reversion 전략 4개를 Python 으로 재구성해 줘. ccxt 로 OHLCV 받고, 벡터라이즈드 백테스트 + Minara baseline DB 와 같은 메트릭 (WR, PF, Sharpe, MDD, n_trades) 출력. paper_trader 도 같이 만들어서 실시간 가격 폴링 → 시그널 발생 시 가상 진입/청산 jsonl 로 기록."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **Lookahead bias**: AI 가 자주 `df['close'].rolling(N).mean()` 으로 시그널 만들고 같은 candle 의 close 가격으로 진입 처리하는 코드를 작성. 실제로는 다음 candle open 이 진입 가능 가격.
- **Survivorship bias 누락**: ccxt 에서 받은 자산 풀이 현재 시점에 살아있는 자산만 포함. 백테스트 결과 Sharpe 가 과대평가되는데 AI 는 디스클레이머 안 적어줌.
- **Fee/slippage 누락**: AI 의 백테스트는 통상 fee 0% 가정. 실제 maker/taker fee + 슬리피지 0.1~0.3% 를 빼야 fair.

힌트: 이 모듈은 4개 전략을 Pine → Python 으로 옮기면서 6번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **lookahead bias, fee drag, BB-Upper short 의 hard stop overlay (10%) 추가** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `backtester.py` | 메인 BacktestEngine (벡터라이즈드, fee/slippage 인자) |
| `backtest_supertrend_btc_1d.py` | SuperTrend 1D (4 trades / 4y, PF 8.98) |
| `backtest_supertrend_btc_1d.md` | SuperTrend 결과 정리 (markdown) |
| `backtest_rsi70_btc_4h.py` | RSI>70 BTC 4H short (Sharpe 1.85, MDD 14.8%, 142 trades) |
| `backtest_rsi70_eth_4h.py` | RSI>70 ETH 4H short (WR 34%, Sharpe 0.70, MDD 20%, 233 trades) |
| `backtest_bb_upper_sol_1h.py` | BB Upper short SOL 1H (10% hard stop overlay 적용) |
| `backtest_mean_rev_btc_15m.py` | Mean Reversion BTC 15m (APR 204% naive, fee drag 검증 필요) |
| `paper_trader_rsi70_btc_4h.py` | RSI70 paper trader (live shadow, jsonl journal) |
| `paper_trader_rsi70_eth_4h.py` | RSI70 paper trader ETH |
| `data/` | OHLCV cache |

## 검증 완료 전략 (Minara DB 기준)

| 전략 | TF | WR | Sharpe | MDD | n trades | 신뢰도 |
|------|----|----|--------|-----|----------|--------|
| **SuperTrend BTC 1D** | 1D | (extreme low freq) | — | — | 4 (4y) | HIGH (PF 8.98) |
| **RSI>70 BTC 4H** | 4H | (1.85 Sharpe) | 1.85 | 14.8% | 142 | HIGH |
| **RSI>70 ETH 4H** | 4H | 34% | 0.70 | 20% | 233 | MED |
| **BB Upper Short SOL 1H** | 1H | — | — | — | — | MED (10% hard stop overlay 추가) |
| **Mean Rev BTC 15m** | 15m | — | (APR 204%) | — | — | LOW (fee drag 검증 필요) |

## 사용 예시 (Usage)

```bash
# OHLCV 다운로드 + 백테스트 한 번에
python backtest_rsi70_btc_4h.py

# 결과 (예시)
# Total trades: 142
# Win rate: 49.3%
# Profit factor: 1.68
# Sharpe: 1.85
# Max DD: 14.8%
# Total return: +XX% (% on notional)
```

### Paper trader (live shadow)

실시간 가격 폴링 → 시그널 발생 시 가상 진입/청산:

```bash
python paper_trader_rsi70_btc_4h.py
# data/paper_rsi70_journal.jsonl 에 진입/청산 기록
```

진입 0건이면 RSI 70+ 시그널이 발생 안 한 것 (4H 캔들 RSI 70+ 빈도는 월 4-8회 추정).

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **Mean Rev BTC 15m 의 fee drag**: 백테스트 APR 204% 였지만 fee 0.1%/leg 적용 시 트레이드 빈도 (15m 마다 진입 후보) × 양쪽 fee 고려하면 net 이 -fee 로 떨어짐. naive 결과를 그대로 신뢰하면 망함.
- **BB Upper Short SOL 1H 의 long tail loss**: 큰 폭등 케이스에서 short 누적 risk. 10% hard stop overlay 추가로 MDD 통제.
- **lookahead bias**: 초기 backtester 가 같은 candle 의 close 시점에 진입 처리 → live 와 backtest 결과 괴리. 다음 candle open 진입으로 수정 후 결과 일관됨.
- **OHLCV 데이터 cache 갱신 안 됨**: `data/` 디렉터리에 caching 했는데 새 데이터로 갱신 안 한 채로 백테스트 돌려서 며칠 분석 결과가 의미 없었음. 캐시 stale 체크 추가.
- **Paper trader 진입 0건**: 4H 캔들에서 RSI 70+ 시그널은 월 4-8회 정도 빈도라 paper trader 가 며칠 동안 진입 0건일 수 있어 "버그인 줄 알고" 디버깅한 케이스. 진입 없는 게 정상.

## 응용 (How this fits with other modules)

- `30-strategy-patterns/_combined/signals/*` 빌딩블록 시그널을 import 해 백테스트 가능
- `40-realtime-infra/cross-venue-arb-scanner/funding_arb.py` 의 fee 가정값 검증에도 사용
- 백테스트에서 검증된 전략은 `30-strategy-patterns/_combined` 에 paper_trader 추가 후 live 로 승격

결합 사용 시: 백테스트 → paper trader 1주일 → live $50 size 점진 ramp-up 의 3단계 검증 흐름이 표준.

## 환경변수

```env
# (옵션) ccxt 거래소별 API key — public OHLCV 는 key 없이 작동
BINANCE_API_KEY=<...>
BINANCE_SECRET=<...>
```

## 추가 전략 후보 (백로그)

- Pair trading z-score (`30-strategy-patterns/_combined/pair_trader.py`)
- Funding rate arbitrage (`40-realtime-infra/cross-venue-arb-scanner/funding_arb.py`)
