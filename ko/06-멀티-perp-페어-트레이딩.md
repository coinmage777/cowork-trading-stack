# 06. 멀티 Perp 페어 트레이딩

메인 봇이 굴리는 핵심 전략. 여러 Perp DEX에서 BTC/ETH 같은 페어를 동시에 long-short으로 들어가서, 두 자산의 상대적 움직임에서 알파를 짜내는 방식.

## 페어 트레이딩이란

기본 아이디어:
- 자산 A와 B가 평소 함께 움직임 (correlation 높음)
- 둘의 가격 비율 / 스프레드가 평균에서 벗어남 (z-score 1.5 이상)
- → A가 B보다 비싸졌으면: A short + B long
- → 평균 회귀하면 양쪽에서 수익

크립토에서 가장 흔한 페어: **BTC vs ETH**. 둘 다 시장 베타가 비슷하고, 상관도 0.8 이상이 평소.

### 왜 단방향 매매보다 유리한가

- **시장 노출 헷지** — 비트코인 갑자기 10% 빠져도 양쪽이 같이 빠지니까 PnL 영향 작음
- **레버리지 안정적으로 사용 가능** — 단방향 10x는 청산 위험 크지만, 헷지된 페어 10x는 더 안전
- **백테스트 가능** — 두 자산의 가격 비율 시계열만 있으면 시그널 디자인 가능

### 단점

- 복잡함 — 한쪽만 들어가는 것보다 변수 두 배
- 교차 거래소 운영하면 자본 / API / 모니터링이 두 배
- 갑자기 한 자산만 movement (ETH alpha unwind 같은) 발생하면 큰 손실

## 운영 봇 구조 — 개요

운영해온 멀티 Perp 페어 트레이딩 봇 (`mpdex` 프레임워크 위) 구조:

```
config.yaml          → 거래소 / 전략 / 사이즈 설정
multi_runner.py      → 메인 루프, 거래소별 페어 트레이더 인스턴스 관리
strategies/
  pair_trader.py     → 단일 거래소 페어 트레이딩 로직
  nado_pair_scalper.py → 빠른 모멘텀 + 스프레드 변형
  strategy_evolver.py  → 시그널 가중치 자동 튜닝
mpdex/               → 거래소 어댑터들 (Hyperliquid, GRVT, Lighter, ...)
state_manager.py     → 봇 상태 영속화 (DCA / trailing 복원용)
db.py                → SQLite 트레이드 / equity 로깅
```

### Factory 패턴

새 거래소 추가가 쉬워야 한다. Factory 패턴 + lazy loading:

```python
# factory.py
def create_exchange(name: str, key_params: dict):
    if name == "hyperliquid":
        from mpdex.hyperliquid import HyperliquidExchange
        return HyperliquidExchange(**key_params)
    elif name == "grvt":
        from mpdex.grvt import GrvtExchange
        return GrvtExchange(**key_params)
    # ...
```

이러면 `config.yaml`에 거래소 이름만 적으면 자동 로드.

### 통합 인터페이스

모든 거래소가 같은 메소드를 노출:

```python
class BaseExchange:
    async def get_mark_price(self, symbol: str) -> Decimal: ...
    async def get_position(self, symbol: str) -> Position: ...
    async def create_order(self, symbol: str, side: str, qty: Decimal, 
                           order_type: str = "limit", price: Decimal = None) -> Order: ...
    async def cancel_orders(self, symbol: str): ...
    async def close_position(self, symbol: str): ...
    async def get_collateral(self) -> Decimal: ...
```

봇 본체는 거래소가 무엇이든 같은 코드로 동작.

## 시그널 — 진입 / 청산 규칙

### 1) 모멘텀 차이 (momentum_diff)

기본:
- 코인1 (BTC) 14분 가격 변화율 vs 코인2 (ETH) 14분 가격 변화율
- 차이가 임계값 (예: 2%) 이상이면 진입
  - BTC > ETH: BTC short + ETH long (역모멘텀 베팅)

근거: 단기 모멘텀은 평균 회귀하는 경향이 강함. 페어에서 이 효과가 단방향보다 안정적.

### 2) 스프레드 z-score

```python
def calc_spread_zscore(prices_btc, prices_eth, lookback=60):
    spread = np.log(prices_btc) - np.log(prices_eth)
    mean = spread[-lookback:].mean()
    std = spread[-lookback:].std()
    z = (spread[-1] - mean) / std
    return z
```

- |z| ≥ 1.5: 진입 (방향은 z의 부호 따라)
- |z| < 0.3: 청산 (수렴)

### 3) 볼린저 밴드 돌파 / RSI 발산 (보조)

메인 시그널은 위 두 개. 그 외:
- 볼린저 밴드: 스프레드가 2σ 밖
- RSI 발산: BTC RSI 70+ + ETH RSI 50- 같은 비대칭

이 보조 시그널들은 가중치 낮게 (0.10~0.20).

### 4) 레짐 필터 — 매우 중요

상관관계 낮은 시점에는 페어 트레이딩이 안 통한다 (둘이 따로 움직이면 평균 회귀 가설 깨짐).

```python
def regime_ok(prices_btc, prices_eth, window=24):
    # 24개 캔들 (6시간) Pearson correlation
    corr = np.corrcoef(prices_btc[-window:], prices_eth[-window:])[0, 1]
    return corr >= 0.7  # 0.7 미만이면 진입 차단
```

이 필터 추가 후 일일 손실 빈도가 명확히 줄어들었다.

### 5) 방향 비대칭 — 데이터 기반

데이터: ETH long (coin2_long) 승률 80% vs BTC long (coin1_long) 승률 70%.

이유 추측: 알트 시즌 / 베타 차이. 어쨌든 데이터가 그러면 따라야 한다.

```python
COIN2_LONG_ENTRY_BONUS = 0.15  # ETH long 진입 threshold 15% 할인
```

## 청산 우선순위

운영 봇이 청산 결정할 때 체크하는 순서 (위에서 아래로):

1. **하드 스톱** (-2.5% PnL): 무조건 즉시 청산
2. **트레일링 스탑**: 활성화 후 callback 도달
3. **고정 익절** (+2% PnL): 이전엔 0.4%였는데 R:R 안 맞아서 5x 늘림
4. **모멘텀 손실 캡**: 비활성화 (hard_stop이 안전망 역할)
5. **모멘텀 이탈 / 스프레드 수렴**: 시그널 청산
6. **DCA**: 추가 진입 조건 만족 시 사이즈 확대

### R:R 수학

이 부분이 가장 비싸게 배운 것. **TP / SL 비율이 손익분기 승률을 결정**.

예: TP 0.4% + SL 3% → 본전 승률 = 3 / (0.4 + 3) = 88%. 이거 못 맞춤.

했던 실수: TP 0.4% / SL 3%로 운영 → 장기간 누적 손실.

수정 후: TP 2% / SL 2.5% → 본전 승률 55%. 실제 승률 67~84% → 수익성.

**TP / SL은 마진 기준이 아니라 R:R 기준으로 디자인**. 이걸 수학적으로 검증한 다음에야 라이브.

### 트레일링 스탑

기본:
- 활성화: PnL +1.5%
- 콜백: 1.0%
- 더 좋아지면 (PnL +3% 이상) → 콜백 0.5%로 좁힘 (수익 보호)

```python
class TrailingStop:
    def __init__(self, activation_pct, callback_pct, tighten_above, tighten_callback):
        self.activated = False
        self.peak = 0
        # ...

    def update(self, current_pnl_pct):
        if not self.activated and current_pnl_pct >= self.activation_pct:
            self.activated = True
            self.peak = current_pnl_pct
        if self.activated:
            self.peak = max(self.peak, current_pnl_pct)
            cb = self.tighten_callback if self.peak >= self.tighten_above else self.callback_pct
            if self.peak - current_pnl_pct >= cb:
                return True  # close
        return False
```

## DCA (Dollar Cost Averaging) — 추가 진입

진입 가격 빗나갔을 때 추가 진입으로 평단을 낮추는 패턴. **위험하지만 잘 쓰면 강력**.

룰:
- 최대 진입 회수: 3회
- 추가 진입 조건: 이전 진입 대비 가격이 N% 더 나빠졌을 때 (스프레드 기준)
- 각 진입 사이즈: 동일 (마틴게일 X)
- DCA 깊이별 승률 모니터링: 4회 이상 승률 급락하면 max_entries 줄임

데이터:
- 1~3회: 양호한 승률 (70%+)
- 4회 이상: 급락 (48%)
- 9~10회: 손실 집중 (-$XXX 누적)

→ max_entries = 3 또는 5로 제한.

## 거래소 그룹 분산

같은 시점에 여러 거래소 동시 진입하면 시장 충격이 운영자에게 돌아온다. 그래서:

| 그룹 | delay | momentum 배수 | 실효 threshold | 거래소 |
|------|-------|---------------|----------------|--------|
| GA | 0s | 0.8x | 1.6% | 거래소 6개 |
| GB | 30s | 1.0x | 2.0% | 거래소 6개 |
| GC | 60s | 1.5x | 3.0% | 거래소 6개 |
| GD | 90s | 2.0x | 4.0% | 거래소 4개 |

뒤 그룹일수록 강한 시그널만 진입 → 운영 봇이 동시 청산되는 경우 줄임.

## Strategy Evolver

시그널 가중치를 자동으로 튜닝하는 컴포넌트. 매 6시간마다:
1. 최근 트레이드 성과 분석
2. 시그널별 기여도 계산
3. 가중치 조정 (성과 좋은 시그널 ↑)
4. 새 가중치로 다음 사이클 시작

**과적합 방지를 위한 하한선**:
```yaml
min_signal_weights:
  momentum_diff: 0.15
  spread_zscore: 0.10
```

이거 안 두면 evolver가 핵심 시그널을 0으로 만들어서 봇이 이상해진다.

## 운영 팁

### 1) 처음에는 작게
페어 트레이딩 봇은 진입 시그널 / 청산 시그널 / 사이즈 / 레버리지 / 거래소 / 페어 / 시장 변동성 등 변수가 많다. 처음에는 작은 자본 + 한 거래소 + 한 페어로 시작. 안정 운영 확인 후 확장.

### 2) 데이터 기반 결정
"감으로 SL 좀 늘려볼까"는 금지. DB에서 실제 손익 분포 보고 결정. 매번 변경 전에 N개 트레이드의 분포 분석.

### 3) 한 번에 하나만 변경
SL / TP / 사이즈 / 레버리지 동시에 바꾸면 어느 게 효과 있는지 모른다. A/B 테스트 식으로 하나씩.

### 4) 서킷브레이커 필수
일일 -X% 손실 시 자동 정지. 기본값 -$30~-$150 (자본 규모에 따라).

### 5) Telegram 통합
모든 진입 / 청산 / 에러를 텔레그램으로. 모바일에서도 봇 상태 알 수 있게.

## 운영 현황 (스냅샷)

운영 중인 거래소 수: 17+
주력 페어: BTC/ETH, ETH/SOL
레버리지: 10x (예전 15x)
사이즈: 진입당 $50 마진
최대 동시 포지션: 거래소당 3
일일 PnL 분포: ±2~3% (자본 대비)

이게 정확한 수익을 의미하지 않는다. 어떤 날은 손실, 어떤 날은 수익. 시간이 지나면서 평균이 양수면 성공. 매일 잔고 트래커로 진실을 본다 (DB의 PnL 수치는 부정확할 수 있음 — 다음 장 포함).

## 다음 장

다음은 김프 + Cross-Venue 차익. 한국 거래소와 글로벌 거래소 간의 가격 차이를 어떻게 자동화하는지.
