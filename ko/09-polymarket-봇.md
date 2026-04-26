# 09. [Polymarket](https://polymarket.com/?ref=coinmage) 봇

운영해온 봇 중 Perp DEX와 별개의 트랙입니다. 예측 시장(prediction market) 자동매매입니다.

## Polymarket이 뭔가요

- **Polygon 기반 예측 시장**
- 이진 outcome 마켓: "이 사건이 일어날까?" → YES 토큰 / NO 토큰
- 가격 = 확률 (0–1 USD)
- 사건 발생 시 정답 토큰이 1 USD로 정산되고, 오답은 0
- 트럼프 당선 시장 같은 거대 마켓부터, "이번 주 ETH 가격" 같은 작은 것도 있습니다

### Polymarket 데이터의 특이점

- **CLOB (중앙 오더북)** — Polygon 위에 있지만 매칭은 오프체인 / 정산은 온체인
- **수수료 없음** (USDC 매수/매도 자체 수수료. 가스비는 약간)
- **유동성** 마켓에 따라 천차만별입니다 — 트럼프 시장은 깊고, 작은 마켓은 얕습니다

### 운영 중인 봇 트랙

1. **expiry_snipe** — 임박한 만료 시점 가격이 떨어진 토큰 매수
2. **hedge_arb** — 같은 마켓 YES + NO 합계 < 0.99 (또는 결합 다른 거래소)일 때 차익
3. **weather** — 날씨 마켓 전용 (이미 비활성화)
4. **predict_snipe** — [Predict.fun](https://predict.fun?ref=5302B) (Polymarket 비슷한 BSC 기반 예측 시장)

## 왜 예측 시장이 매력적인가요

- **시장이 비효율적** — Polymarket은 크립토 트레이더 + 정치 베팅 사람들이 섞여 있어 흥미롭습니다
- **명확한 outcome** — 가격이 0 또는 1로 수렴합니다 (정산일에)
- **무한정 보유 가능** — 청산 없음, 펀딩비 없음
- **차익 기회** — 다른 예측 시장 ([Predict.fun](https://predict.fun?ref=5302B), Kalshi)과 가격 차이

## 왜 어려운가요 (망한 케이스)

Polymarket 서브계정 하나로 8일 만에 약 60% 떨어진 적이 있습니다. 그 과정에서 배운 것들입니다.

### 1) 역선택 (Adverse Selection)

- mid-price에 리밋 매수 주문을 박음
- 그 가격에서 체결되려면 누가 그 가격에 매도해야 함
- 매도자는 봇 / 정보 우위가 있는 사람
- → 체결되는 주문은 봇 예측이 틀린 케이스

데이터: 체결률 14%, 체결 시 승률 29%. 즉, 체결 자체가 마이너스 시그널입니다.

**교훈**: 단순 방향성 베팅을 리밋 주문으로 하면 마켓메이킹이나 정보 우위 없이는 거의 불가능합니다.

### 2) Polymarket 오더북 구조 함정

- `/book` API는 bid 0.01–0.10 / ask 0.90–0.99만 표시 (보완 토큰 매칭 때문)
- 실제 mid는 0.50 근처
- "ask" 가격에 매수하려고 하면 0.90 같은 비싼 가격에 박힘 → 체결 안 됨

**해결**: `/price?side=buy` API를 사용해서 실제 매수 가능 가격을 조회합니다.

### 3) verify_order_fill + cancel = ghost position

했던 실수입니다:
1. 주문 후 N초 안에 체결 확인
2. 미체결로 판단 → cancel
3. 실제로는 체결됨 (CLOB 매칭 지연)
4. DB 미기록 → ghost position
5. 결국 정산 시점에 누군가의 토큰이 자산이지만 추적 안 됨 → 사실상 손실

**교훈**: **절대 주문 후 cancel 하지 마세요.** 대신 정산 시점에 `get_order()`로 사후 확인하세요.

### 4) 날씨 마켓 ghost position

`expiry_time=""` 빈 문자열 저장 → DB 쿼리 `expiry <= 0` 으로 평생 안 닫힘 → 포지션 슬롯 영구 점유.

**해결**: 
- weather opp dict에 expiry_ts 필드 명시
- 24시간 이상 된 expiry=0 트레이드 자동 강제 close (안전망)

### 5) PnL 공식 버그

DB의 `size`는 달러 비용인데 공식이 shares처럼 계산됨.
- 이전 (잘못): `WIN = (1-ep) * size * 0.9`
- 수정: `WIN = size * (1/ep - 1)`

이 버그로 DB PnL이 실제 잔고 변화와 크게 달랐습니다. **DB PnL 신뢰 X. 온체인 잔고가 진실의 원천입니다**.

### 6) [Predict.fun](https://predict.fun?ref=5302B) 가스 부족

Signer EOA에 BNB가 없으면 클레임이 실패합니다. Predict Account가 아니라 **Signer 주소**에 BNB가 필요합니다.

**해결**: 가스 부족 감지 시 클레임 루프 즉시 중단 + 다음 주기에 재확인 후 재개.

## 정착한 패턴

### 라이브 전략 두 가지

1. **hedge_arb** (차익): 같은 마켓 YES + NO 합계 < 0.93. 무위험 차익. 빈도 낮음
2. **predict_snipe** ([Predict.fun](https://predict.fun?ref=5302B)): 임박 만료 + 변동성 기반 가격 모델로 가격이 잘못된 경우 진입

### Shadow 전략

`expiry_snipe`는 라이브 비활성화, shadow에서만 추적합니다 (DB에 기록만, 실제 주문 X). 데이터 누적 후 패턴이 발견되면 다시 라이브 검토합니다.

### 안전 장치

- **circuit breaker**: 일일 -$30 이상 손실 시 라이브 진입 중단 (모니터링은 계속)
- **balance snapshots**: 10분마다 USDC 잔고 기록 → 실제 PnL 추적
- **auto claim**: 정산된 마켓 자동 리딤 (`auto_claimer.py`, 120초 주기)
- **WAL 체크포인트**: SQLite 30분마다 자동 (DB corruption 방지)
- **API retry**: 모든 외부 호출 3회 + 지수 백오프
- **API 타임아웃**: 30초 강제

### 코드 안정성

시행착오로 추가한 방어 로직입니다:

```python
# 1. price 검증
def place_order(price, ...):
    if price < 0.001:
        raise ValueError("Polymarket min price is 0.001")
    # ...

# 2. RSI 엣지케이스
def rsi(prices, period=14):
    gains, losses = ...
    if gains == losses == 0:
        return 50  # neutral, not 100
    # ...

# 3. log_trade 방어
def log_trade(**kwargs):
    try:
        # DB insert
    except Exception as e:
        logger.error(f"log_trade failed: {e}")
        # 봇 크래시는 막음

# 4. expiry 보장
def open_weather_position(opp):
    assert opp["expiry_ts"] > 0
    log_trade(expiry_time=str(opp["expiry_ts"]), ...)
```

## [Predict.fun](https://predict.fun?ref=5302B) 통합

Predict.fun은 BSC 기반 예측 시장입니다. 비슷한 패턴이지만 차이점이 있습니다:

- **지갑 구조**: Signer EOA + Predict Account (스마트 컨트랙트). 가스비는 Signer EOA에 BNB로
- **API**: REST + WebSocket. 자체 SDK 있음
- **자산**: BTC / ETH / SOL / BNB 마켓 다수
- **만료 시간 짧음**: 1분 ~ 1시간 마켓이 흔함 → 빠른 사이클

운영 봇 통합:
- main.py에 `_predict_loop()`로 통합 (별도 프로세스 X)
- DB 스키마에 strategy_name="predict_snipe" 컬럼 추가
- Telegram 알림 / 클레임 / resolve 모두 통합
- `.env`에서 파라미터 동적 로드 (재시작 없이 튜닝)

### [Predict.fun](https://predict.fun?ref=5302B) 확률 모델

단순 선형 모델은 부정확합니다. **변동성 기반 정규분포 CDF**:

```python
import scipy.stats as stats
import math

def predict_probability(asset, current_price, target_price, minutes_left):
    sigma_per_minute = {
        "BTC": 0.0012,  # 0.12%
        "ETH": 0.0018,  # 0.18%
        "SOL": 0.0025,  # 0.25%
        "BNB": 0.0015,  # 0.15%
    }[asset]
    
    sigma = sigma_per_minute * math.sqrt(minutes_left)
    log_diff = math.log(target_price / current_price)
    z = log_diff / sigma
    return 1 - stats.norm.cdf(z)  # P(price > target)
```

이 모델 + 자산별 edge buffer (BTC 0%, ETH 3%, SOL 3%, BNB 2%) 로 진입을 결정합니다.

### 파라미터 (현재)

```bash
PREDICT_BET_SIZE=3
PREDICT_MAX_ENTRY_PRICE=0.70
PREDICT_MIN_EDGE=0.04
PREDICT_MAX_MINUTES=5  # 2-5분 구간 WR 85%+ 집중
```

## 보고 시스템

매일 보는 것입니다:

```bash
python poly_report.py --days 7  # 최근 7일
python poly_report.py --live    # 오늘
python poly_report.py --date 2026-04-20
```

출력 예 (실제 수치 비공개):
```
=== YYYY-MM-DD Polymarket 일일 성과 ===

전략별:
  hedge_arb     +X.XX  (n=X, WR XX%)
  predict_snipe +X.XX  (n=X, WR XX%)

USDC 잔고 변화: $XXX → $XXX
순 PnL: $±X.XX

활성 포지션: N
미정산 마켓: M
```

## 운영 교훈 정리

1. **DB PnL ≠ 실제 PnL**. 잔고 스냅샷이 진실
2. **리밋 주문은 역선택 위험**. 마켓메이킹이나 정보 우위 없으면 위험
3. **주문 후 cancel 금지**. ghost position 발생
4. **모든 외부 API에 retry + timeout**
5. **circuit breaker 필수** — 무한 손실 방지
6. **shadow 모드 활용** — 라이브 박기 전 안전 검증
7. **베팅 사이즈 올리기 전에 체결률 확인** — 체결 안 되는 주문 사이즈 올리면 손실만 커짐

## 다음 장

다음은 Gold cross-exchange arb입니다 — 금 ETF / 토큰화 금 / Perp 금 시장에서의 차익. 일반화된 cross-exchange 패턴입니다.
