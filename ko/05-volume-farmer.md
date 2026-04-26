# 05. Volume Farmer

본격 트레이딩 봇 만들기 전에 가장 단순한 자동화부터 — 볼륨 파머. 거래소가 볼륨 기준으로 포인트 / 리워드 주는 시즌에 대단히 유용하다.

## 볼륨 파머가 뭔가

기본 아이디어: **양방향 동시 진입 / 청산으로 볼륨만 누적, 가격 노출은 0에 가깝게.**

예: BTC long $1000 + BTC short $1000 → 가격 움직여도 PnL ≈ 0, 거래량 $2000 누적.

이걸 자동으로 N분마다 반복하면 시즌 끝날 때 포인트가 쌓인다.

### 어디서 유용한가

- **에어드랍 시즌의 Perp DEX** ([Hyperliquid](https://miracletrade.com/?ref=coinmage), Lighter, [EdgeX](https://pro.edgex.exchange/referral/570254647), Nado, [GRVT](https://grvt.io/exchange/sign-up?ref=1O9U2GG) 등)
- **거래량 기반 거래소 캠페인** (Bybit, OKX 등 종종 진행)
- **메이커 리베이트 거래소** — 메이커 수수료가 음수면 볼륨 자체가 수익

### 어디서 안 통하는가

- 수수료가 비싼 거래소 (CEX 평균 0.05% taker → 양방향 0.1% 손실)
- "트레이드 PnL > 0"이 포인트 조건인 캠페인
- 동일 계정 양방향 매칭을 막는 거래소 (이상거래 탐지로 BAN 가능)

## 구조 — 가장 단순한 버전

```python
import asyncio
import ccxt.async_support as ccxt

async def volume_farm(exchange, symbol, size_usd, sleep_sec):
    while True:
        try:
            ticker = await exchange.fetch_ticker(symbol)
            price = ticker["last"]
            qty = size_usd / price

            # 1. 롱 진입 (마켓)
            long_order = await exchange.create_market_buy_order(symbol, qty)
            await asyncio.sleep(1)

            # 2. 숏 진입 (마켓) — 같은 사이즈
            short_order = await exchange.create_market_sell_order(symbol, qty)
            await asyncio.sleep(1)

            # 3. 둘 다 청산
            await exchange.create_market_sell_order(symbol, qty)  # 롱 청산
            await asyncio.sleep(1)
            await exchange.create_market_buy_order(symbol, qty)   # 숏 청산

            print(f"[{symbol}] cycle done, vol ~${size_usd*4}")
        except Exception as e:
            print(f"error: {e}")
        await asyncio.sleep(sleep_sec)

async def main():
    ex = ccxt.bybit({
        "apiKey": os.getenv("BYBIT_API_KEY"),
        "secret": os.getenv("BYBIT_SECRET"),
        "options": {"defaultType": "swap"},
    })
    await volume_farm(ex, "BTC/USDT:USDT", size_usd=100, sleep_sec=300)

asyncio.run(main())
```

이거 한 시간에 약 $1200 볼륨, 하루 $30k 정도. 작은 자본으로 시작 가능.

## 위 코드의 문제점 — 실전에서는 깨진다

위 단순 버전은 시연용이지 실전용이 아니다. 실전에서 만난 문제들:

### 1) 슬리피지로 PnL이 0이 안 됨

마켓 주문 두 번이면 평균 0.02~0.05% 슬리피지가 양방향에서 발생한다. 매 사이클마다 작은 손실이 쌓인다.

**해결**: 마켓 주문 대신 BBO (best bid/offer) 지정가로. 또는 Post-Only.

```python
ob = await exchange.fetch_order_book(symbol)
best_bid = ob["bids"][0][0]
best_ask = ob["asks"][0][0]
# 롱 진입: best_bid에 지정가 매수 (메이커)
# 숏 진입: best_ask에 지정가 매도 (메이커)
```

이렇게 하면 메이커 수수료만 내고, 거래소에 따라 리베이트까지 받는다.

### 2) 한쪽만 체결되면 노출 발생

마켓 주문은 거의 즉시 체결되지만, 지정가는 안 될 수도 있다. 한쪽만 체결되면 갑자기 단방향 포지션이 생긴다.

**해결**: 
- 한쪽 체결 후 N초 안에 반대쪽 체결 안 되면 cancel + 재시도
- 또는 일부러 마켓 주문 (슬리피지 감수)

```python
async def safe_pair_open(exchange, symbol, qty, timeout=10):
    long_order = await place_post_only(exchange, symbol, "buy", qty)
    short_order = await place_post_only(exchange, symbol, "sell", qty)
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        long_filled = await is_filled(exchange, long_order["id"])
        short_filled = await is_filled(exchange, short_order["id"])
        if long_filled and short_filled:
            return True
        await asyncio.sleep(0.5)
    
    # 둘 다 체결 안 됐거나 한쪽만 체결됨
    await exchange.cancel_order(long_order["id"], symbol)
    await exchange.cancel_order(short_order["id"], symbol)
    
    # 한쪽만 체결됐으면 마켓으로 정리
    # (포지션 조회 → 반대 사이드 마켓 청산)
    return False
```

### 3) 거래소 BAN

같은 계정에서 양방향 셀프 트레이드 탐지하는 거래소 있다. 특히 가격 정확히 같은 곳에 두 주문 동시에 던지면 의심받는다.

**해결**:
- 가격을 살짝 다르게 (best_bid + 1tick / best_ask - 1tick)
- 시간차를 둠 (long 진입 → 30초 대기 → short 진입)
- 사이즈를 살짝 다르게 (10% 변동 허용)
- 두 계정으로 운영 (하나는 long-only, 하나는 short-only — 더 안전)

### 4) 변동성 큰 시장에서 양방향 손실

거래소 한쪽이 진입 시점부터 청산 시점까지 가격이 1% 움직였다면, 양쪽 PnL 합쳐도 0이 안 될 수 있다 (수수료 + 펀딩비 때문).

**해결**: 
- 변동성 큰 시간대 (CPI / FOMC / 큰 리스트 직후) 회피
- 페어 보유 시간 짧게 (5분 안에 진입+청산)
- 펀딩비 낮을 때만 진입

### 5) 펀딩비

Perp이라 펀딩비가 8시간마다 발생한다. 양쪽 보유 중이면 long pays + short receives = 0이지만, 한쪽 청산 후 잠시 단방향이면 펀딩비 손실 발생.

**해결**: 펀딩비 시간 (00, 08, 16 UTC) 직전 / 직후 회피.

## 운영해온 패턴

수년간 시행착오 끝에 정착한 구조:

### 거래소별 모듈화

각 거래소의 차이를 추상화하는 어댑터 레이어:

```python
class ExchangeAdapter:
    async def place_post_only(self, symbol, side, qty, price): ...
    async def cancel_order(self, order_id, symbol): ...
    async def get_position(self, symbol): ...
    async def get_balance(self): ...
    # ...
```

각 거래소(`hyperliquid.py`, `bybit.py`, ...)가 이 인터페이스 구현. 봇 본체는 어댑터만 호출.

### 사이클 디자인

1. **진입**: BBO 지정가 양방향 동시 (시간차 1~5초)
2. **체결 확인**: 10초 타임아웃, 안 되면 cancel + 마켓 폴백
3. **보유**: 30초 ~ 5분 (랜덤화 — BAN 회피)
4. **청산**: 동일 패턴
5. **다음 사이클까지 대기**: 랜덤 60~300초

### 모니터링

- 사이클당 PnL 기록 (DB)
- 누적 볼륨 / 누적 수수료 추적
- 일일 효율성: `포인트 / 비용`
- 실패율: 한쪽만 체결된 비율

### 거래소 선택 기준

보는 메트릭:

| 메트릭 | 좋은 값 |
|--------|---------|
| 메이커 수수료 | < 0.005% (또는 음수) |
| 테이커 수수료 | < 0.05% |
| 펀딩비 변동성 | 안정적 (1시간 내 0.05% 미만) |
| 오더북 깊이 | 적당한 사이즈 충분히 흡수 |
| API 안정성 | 24시간 5xx 에러 < 1% |
| 캠페인 ROI | 볼륨당 포인트 가치 |

### 자본 효율

볼륨 $100k 만들려고 자본 $100k 다 박아둘 필요 없다. 사이클당 $1k 사이즈면, 사이클이 100번 돌아가면 $100k 볼륨이다. 자본은 $1k면 충분.

레버리지 쓰면 더 줄어든다. 5x 레버리지면 자본 $200으로 사이즈 $1k 가능. 단 청산 리스크 관리 필수.

## 거래소 그룹 분산

여러 거래소를 동시에 굴리는데, 모두 같은 시점에 진입하면 시장 충격이 커지고 BAN 위험이 올라간다. 그래서 그룹별 시차를 둔다:

| 그룹 | 시차 | 사이즈 배수 | 거래소 |
|------|------|-------------|--------|
| GA | 0초 | 1.0x | 거래소 6개 |
| GB | 30초 | 1.0x | 거래소 6개 |
| GC | 60초 | 1.5x | 거래소 6개 |
| GD | 90초 | 2.0x | 거래소 4개 |

이러면 한 시장 충격이 분산되고, 알고리즘 탐지에도 덜 걸린다.

## 다음 장

다음은 본격 트레이딩 봇 — 멀티 Perp DEX 페어 트레이딩. 메인 봇의 핵심 전략이다.
