# 08. Minara 백테스팅

전략 라이브에 박기 전에 백테스트를 하지 않으면 그냥 도박꾼이다. 쓰는 백테스팅 워크플로우와 그 과정에서 배운 것들.

## 왜 백테스트가 중요한가

한 번 백테스트 안 하고 라이브 박았다가 장기간 누적 손실 본 경험이 있다. 원인: TP/SL 비율이 본전 승률 94% 요구하는 구조였는데, 그걸 라이브 데이터로 알아챘다. 백테스트 한 시간만 했어도 그 자리에서 발견됐을 문제.

백테스트가 답하는 질문:
- 이 전략의 기대 승률은?
- 평균 익절 / 평균 손절 비율은?
- 손익분기 승률은?
- 최대 낙폭 (Max Drawdown)은?
- 특정 시장 상황 (변동성 큰 / 작은 / 추세 / 횡보)에서 어떻게 작동하나?

## 백테스트의 함정

### 1) 데이터 품질
- **흔한 실수**: 1분봉 OHLCV로 백테스트 → 실제로는 진입 가격이 high/low 사이 어디였을 수도 있음
- **개선**: tick 데이터 또는 1초봉. 그게 안 되면 1분봉의 high/low를 양쪽 다 시뮬

### 2) 슬리피지 / 수수료 무시
- **흔한 실수**: 마켓 주문이 다 mid price에 체결됐다고 가정
- **개선**: 매번 수수료 차감 + 사이즈에 비례한 슬리피지 적용

### 3) 미래 정보 누설 (Look-ahead Bias)
- **흔한 실수**: 시그널 계산할 때 종가 사용 → 실제로는 그 시점에 종가 모름
- **개선**: 시그널은 항상 직전 봉까지의 데이터로 계산

### 4) 과적합
- **흔한 실수**: 1년 데이터에 파라미터 그리드 서치 → "최적 파라미터"가 그 1년에만 좋고 미래엔 망함
- **개선**: 
  - Train / Validation / Test 분할 (예: 70/15/15)
  - Walk-forward analysis (시간 순으로 train → test → 다음 train → 다음 test)
  - 파라미터 견고성 체크 (작은 변경에 PnL 크게 안 변하는지)

### 5) 생존자 편향
- **흔한 실수**: 현재 거래되는 종목으로만 백테스트
- **개선**: 상장폐지된 토큰 / 사라진 거래소 데이터도 포함

## 쓰는 백테스트 워크플로우

### 1단계: 데이터 수집

거래소 API에서 OHLCV 풀링:

```python
import ccxt
import pandas as pd

ex = ccxt.binance()
since = ex.parse8601("2024-01-01T00:00:00Z")
all_candles = []
while True:
    candles = ex.fetch_ohlcv("BTC/USDT", timeframe="1m", since=since, limit=1000)
    if not candles:
        break
    all_candles += candles
    since = candles[-1][0] + 60_000  # 다음 1분
df = pd.DataFrame(all_candles, columns=["ts", "o", "h", "l", "c", "v"])
df.to_parquet("btc_1m.parquet")
```

거래소 API에 rate limit 있으니 sleep 필요.

### 2단계: 시그널 백테스트

기본 프레임:

```python
def backtest(df, signal_fn, entry_threshold, exit_threshold, fee=0.0005, slip=0.0002):
    position = 0  # +1 = long, -1 = short, 0 = flat
    entry_price = 0
    pnl_log = []
    
    for i in range(len(df)):
        signal = signal_fn(df.iloc[:i+1])  # 직전까지 데이터로만
        price = df.iloc[i]["c"]
        
        if position == 0:
            if signal > entry_threshold:
                position = 1
                entry_price = price * (1 + slip)
            elif signal < -entry_threshold:
                position = -1
                entry_price = price * (1 - slip)
        else:
            pnl_pct = (price - entry_price) / entry_price * position
            if abs(signal) < exit_threshold or pnl_pct < -0.025:  # SL -2.5%
                exit_price = price * (1 - slip * position)
                trade_pnl = (exit_price - entry_price) / entry_price * position - 2 * fee
                pnl_log.append(trade_pnl)
                position = 0
    
    return pnl_log
```

### 3단계: 메트릭

```python
def metrics(pnl_log):
    pnl_arr = np.array(pnl_log)
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr < 0]
    
    return {
        "n_trades": len(pnl_arr),
        "win_rate": len(wins) / len(pnl_arr) if len(pnl_arr) else 0,
        "avg_win": wins.mean() if len(wins) else 0,
        "avg_loss": losses.mean() if len(losses) else 0,
        "profit_factor": -wins.sum() / losses.sum() if losses.sum() < 0 else float("inf"),
        "total_return": pnl_arr.sum(),
        "max_drawdown": (np.maximum.accumulate(pnl_arr.cumsum()) - pnl_arr.cumsum()).max(),
        "sharpe_approx": pnl_arr.mean() / pnl_arr.std() * np.sqrt(365 * 24 * 60) if pnl_arr.std() > 0 else 0,
    }
```

핵심:
- **Profit Factor > 1.5**: 의미 있는 알파 (수수료 + 슬리피지 감안 후)
- **Max Drawdown < 30%**: 자본 관점에서 견딜 만함
- **Sharpe > 1.5**: 변동성 대비 수익 양호 (크립토는 더 높아도 됨)

### 4단계: Walk-forward

전체 기간을 N등분 → 각각 train / test로 분할 → out-of-sample 성과 측정.

```python
def walk_forward(df, signal_fn, n_splits=5):
    chunk = len(df) // n_splits
    oos_returns = []
    for i in range(n_splits - 1):
        train = df.iloc[i*chunk:(i+1)*chunk]
        test = df.iloc[(i+1)*chunk:(i+2)*chunk]
        # train으로 파라미터 튜닝
        best_params = optimize(train, signal_fn)
        # test로 OOS 평가
        oos_returns.append(backtest(test, signal_fn, **best_params))
    return oos_returns
```

OOS PnL이 IS PnL보다 명백히 떨어지면 → 과적합. 다시 디자인.

### 5단계: 시뮬레이션 → 페이퍼 트레이드 → 라이브

백테스트 통과 → 페이퍼 트레이드 (실시간 가격, 가짜 잔고) 1~2주 → 소액 라이브 → 확인 → 스케일.

페이퍼와 백테스트 결과가 크게 다르면 데이터 / 시그널 / 슬리피지 가정 중 하나가 틀린 것.

## Minara — 쓰는 백테스팅 도구

멀티 거래소 페어 트레이딩 봇용으로 빌드한 백테스팅 모듈을 Minara로 부른다. 기본 기능:

- **다거래소 동기화 데이터** — 같은 시점의 N개 거래소 가격을 동기화
- **페어 시그널 백테스트** — momentum_diff / spread_zscore / volatility_ratio 등
- **레짐 필터 시뮬** — correlation 기반 진입 차단 효과 측정
- **DCA 시뮬** — 추가 진입 / max_entries 변화 효과
- **트레일링 스탑 시뮬** — activation / callback 변화 효과
- **수수료 / 펀딩비 모델링** — 거래소별 정확한 수수료 + 펀딩비 시계열 적용
- **거래소 그룹 분산 시뮬** — 시차 진입 / 사이즈 배수의 효과

### 입력 / 출력 구조

입력:
```yaml
data:
  exchanges: [hyperliquid, binance]
  symbols: [BTC, ETH]
  timeframe: 1m
  start: 2024-01-01
  end: 2024-12-31

strategy:
  signal: spread_zscore
  entry_threshold: 1.5
  exit_threshold: 0.3
  stop_loss: -0.025
  trailing:
    activation: 0.015
    callback: 0.010
  dca:
    max_entries: 3
    additional_threshold: 0.003

filters:
  regime:
    enabled: true
    correlation_window: 24
    min_correlation: 0.7

execution:
  fee_maker: 0.0001
  fee_taker: 0.0005
  slippage_pct: 0.0002
```

출력:
- 트레이드 단위 로그 (CSV / parquet)
- 메트릭 요약 (JSON)
- equity curve 차트
- 분포 통계 (PnL 히스토그램, 보유 시간 분포 등)
- 파라미터 sensitivity 차트

### 사용 예시

```bash
python -m minara.backtest --config strategy.yaml --output results/
```

결과:
```
N trades:       1,247
Win rate:       62%
Avg win:        +1.32%
Avg loss:       -1.05%
Profit factor:  1.87
Total return:   +47.3%
Max drawdown:   -18.2%
Sharpe:         2.1
```

이 결과를 보고 라이브 박을지 결정.

## 백테스트 체크리스트

라이브에 박기 전 보는 것:

- [ ] Profit Factor > 1.5 (수수료 + 슬리피지 포함 후)
- [ ] Max Drawdown < 자본의 30%
- [ ] OOS 성과가 IS와 크게 안 다름 (과적합 아님)
- [ ] 파라미터 ±20% 변경해도 PF > 1.2 (견고함)
- [ ] 최소 100 트레이드 (통계적 유의성)
- [ ] 변동성 큰 / 작은 시기 모두 작동 확인
- [ ] 손익분기 승률 < 실제 승률 - 5%p (마진 있음)

이 체크리스트 통과 못하면 라이브 안 박는다.

## 백테스트로 발견한 것

운영하면서 백테스트로 검증해서 좋아진 것들:

1. **레짐 필터 추가**: 상관관계 < 0.7이면 진입 차단 → 일일 손실 빈도 명확히 감소
2. **방향 비대칭**: ETH long이 BTC long보다 승률 높음 → 진입 threshold 차등화
3. **DCA 4회 제한**: 5회 이상 승률 급락 → max_entries 5 → 4 → 3로 줄임
4. **TP/SL 재설계**: 마진 기준이 아니라 R:R 기준으로 → 본전 승률 94% → 55%
5. **no_entry_hours**: 특정 시간대 (UTC 1, 7, 9시) 승률 저조 → 진입 차단

이 모두 백테스트에서 먼저 확인하고 라이브에 적용한 거.

## 다음 장

다음은 [Polymarket](https://polymarket.com/?ref=coinmage) 봇 — 예측 시장 자동매매. Perp 봇과 완전히 다른 시장 구조.
