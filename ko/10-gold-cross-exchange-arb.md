# 10. Gold Cross-Exchange Arb

굴리는 차익 패턴 중에서 일반화 가치가 있는 것 — 토큰화된 자산이나 동일 자산이 여러 거래소에서 다른 가격에 거래될 때.

이름이 "Gold"인 이유: 처음 적용한 게 토큰화 금(PAXG, XAUT 등)에서였다. 같은 패턴이 다른 자산에도 적용된다.

## 토큰화 금 시장의 특징

크립토에 토큰화된 금:
- **PAXG** (Paxos Gold) — 1 PAXG = 1 troy ounce 금
- **XAUT** (Tether Gold) — 동일
- **TRADFI 금 ETF** (GLD, IAU 등) — 직접 크립토는 아니지만 가격 비교용
- **선물 시장 금** — COMEX 등

### 차익 기회

이론적으로는 모두 같은 자산 (1 oz 금)이지만:
- 거래소별로 가격 다름 (유동성 / 수요 차이)
- 크립토 vs TradFi 시간 갭 (TradFi는 주말 휴장)
- 펀딩비 / 보관 수수료 차이

이 차이 중 **거래 가능한 것**을 자동화로 잡는 게 핵심.

## 쓰는 일반 패턴

### Pattern 1: 동일 자산 거래소간

같은 자산 A가 거래소 X에서는 $100, 거래소 Y에서는 $99에 거래.

전략:
- Y에서 매수, X에서 매도
- 수렴할 때까지 보유
- 수렴 후 청산

조건:
- 양 거래소에 자본 미리 분배 (전송 안 해도 됨)
- 수수료 + 슬리피지 < 가격 차이
- 둘 다 충분한 유동성

### Pattern 2: 베이시스 차익 (현물 vs Perp)

같은 자산:
- 현물 가격 $100
- Perp 가격 $101 (펀딩비 양수, 롱이 비쌈)

전략:
- 현물 매수 + Perp short
- 펀딩 받음 + 가격 수렴 시점에 청산

이건 펀딩비가 안정적으로 양수인 경우 거의 무위험. **carry trade**.

리스크:
- 펀딩비가 음수로 뒤집힘
- Perp 거래소의 청산 / 출금 정지
- 자본 락업 (즉시 회수 어려움)

### Pattern 3: 시간 차이 (TradFi vs 크립토)

TradFi 시장 휴장 (주말, 미국 휴일):
- TradFi 가격 고정 (Friday close)
- 크립토 시장은 계속 움직임 → 가격 차이 벌어짐
- 월요일 TradFi 오픈 시 갭 채워짐

전략:
- 주말 동안 크립토 가격 추적
- 월요일 직전 차이가 크면 진입
- 갭 채워지면 청산

이건 백테스트 빡세게 해야 한다. TradFi 갭이 항상 채워지는 것 아님 (큰 뉴스 시 안 채워짐).

### Pattern 4: 토큰화 자산 vs 원본

PAXG (토큰화 금) vs LBMA 금 가격:
- 평소 차이 ±0.5%
- 가끔 +2%까지 벌어짐 (크립토 시장 분리)
- 평균 회귀 베팅 가능

단, LBMA 가격은 24시간 거래 X. 비교 데이터 한계.

## 실전 자동화 — 코드 구조

```python
class CrossExchangeArb:
    def __init__(self, asset: str, exchanges: list[ExchangeAdapter], 
                 entry_spread_bps: float = 30, exit_spread_bps: float = 5):
        self.asset = asset
        self.exchanges = exchanges
        self.entry_spread_bps = entry_spread_bps
        self.exit_spread_bps = exit_spread_bps
        self.position = None  # {long_ex, short_ex, qty, entry_long_px, entry_short_px}

    async def fetch_prices(self) -> dict:
        """모든 거래소 mark price 동시 조회."""
        tasks = [ex.get_mark_price(self.asset) for ex in self.exchanges]
        prices = await asyncio.gather(*tasks, return_exceptions=True)
        return {ex.name: p for ex, p in zip(self.exchanges, prices) if not isinstance(p, Exception)}

    async def detect_opportunity(self, prices: dict):
        """가장 큰 가격 차이 찾기."""
        if len(prices) < 2:
            return None
        sorted_px = sorted(prices.items(), key=lambda x: x[1])
        cheap_ex, cheap_px = sorted_px[0]
        expensive_ex, expensive_px = sorted_px[-1]
        spread_bps = (expensive_px - cheap_px) / cheap_px * 10000
        if spread_bps >= self.entry_spread_bps:
            return {"long_ex": cheap_ex, "short_ex": expensive_ex, 
                    "long_px": cheap_px, "short_px": expensive_px, "spread_bps": spread_bps}
        return None

    async def open(self, opp):
        """양쪽 동시 진입."""
        long_task = self._exchange(opp["long_ex"]).create_order(
            self.asset, "buy", self.size, "limit", price=opp["long_px"])
        short_task = self._exchange(opp["short_ex"]).create_order(
            self.asset, "sell", self.size, "limit", price=opp["short_px"])
        long_order, short_order = await asyncio.gather(long_task, short_task)
        # 체결 확인 + 한쪽만 체결 시 정리
        # ...
        self.position = {...}

    async def check_exit(self, prices: dict):
        if not self.position:
            return False
        long_px = prices[self.position["long_ex"]]
        short_px = prices[self.position["short_ex"]]
        spread_bps = (short_px - long_px) / long_px * 10000
        return spread_bps <= self.exit_spread_bps

    async def close(self):
        long_task = self._exchange(self.position["long_ex"]).close_position(self.asset)
        short_task = self._exchange(self.position["short_ex"]).close_position(self.asset)
        await asyncio.gather(long_task, short_task)
        # PnL 계산 + 로깅
        self.position = None

    async def run(self):
        while True:
            try:
                prices = await self.fetch_prices()
                if self.position is None:
                    opp = await self.detect_opportunity(prices)
                    if opp:
                        await self.open(opp)
                else:
                    if await self.check_exit(prices):
                        await self.close()
            except Exception as e:
                await notify(f"[arb] error: {e}")
            await asyncio.sleep(5)
```

## 핵심 디테일

### 1) 양쪽 동시 진입의 어려움

이 차익의 가장 큰 위험은 **한쪽만 체결되는 경우**. 그러면 갑자기 단방향 노출이 생긴다.

해결:
- IOC (Immediate-or-Cancel) 주문 사용 — 즉시 체결 안 되면 취소
- 한쪽 체결 후 N초 안에 반대쪽 체결 안 되면 → 체결된 쪽 즉시 마켓 청산
- 또는 Maker-Taker 분리: 한쪽은 maker로 (시간 걸려도 OK), 다른 쪽은 taker로 (즉시 매칭)

### 2) 실제 체결 가능 가격

오더북 mid price만 보면 안 된다. 사이즈를 흡수하는 데 슬리피지 발생.

```python
def estimate_execution_price(order_book, side, qty):
    levels = order_book["asks"] if side == "buy" else order_book["bids"]
    remaining = qty
    total_cost = 0
    for px, sz in levels:
        take = min(sz, remaining)
        total_cost += take * px
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # 유동성 부족
    return total_cost / qty
```

### 3) 자본 분배

거래소 N개에 자본을 어떻게 나눌까? 룰:
- 각 거래소에 최소 사이즈의 3~5배 자본
- 페어 트레이딩이라 한쪽이 마이너스가 나도 다른 쪽이 플러스 → 청산 위험 작음
- 하지만 한 거래소에서 출금 정지되면 큰 문제 → 한 거래소에 자본 너무 몰지 마라
- 룰: 한 거래소 최대 = 총 자본의 25%

### 4) 거래소 신뢰도 차등화

새로 출시된 거래소는 출금 정지 / 해킹 리스크 있다. 등급:
- **Tier 1** (신뢰): 바이낸스, OKX, Bybit, [Hyperliquid](https://miracletrade.com/?ref=coinmage), Coinbase
- **Tier 2**: 잘 알려진 DEX들 (Lighter, dYdX, GMX)
- **Tier 3**: 새 DEX, 작은 거래소

Tier 3에는 차익 사이즈 작게 (총 자본의 5% 이하).

### 5) 모니터링

차익 봇은 한 번 진입하면 수렴까지 보유. 그동안 이상 상황 감지가 중요:
- 한쪽 거래소 가격 급변동 → 펀딩비 폭증 가능
- 한쪽 거래소 다운 → 청산 못 함
- 시장 전체 변동성 폭증 → 일시적 노출 위험

→ 30초마다 양쪽 mark price + 펀딩비 체크 + 임계 도달 시 알림.

## 운영해본 것

### 성공 케이스 (간헐적)

- BTC 베이시스 차익: Hyperliquid vs 바이낸스, 펀딩비 양수일 때 carry trade. 안정적
- ETH 펀딩 차익: 거래소 간 펀딩비 0.05% 차이 시점

### 실패 케이스 (배운 것)

- 작은 거래소 차익 도전: 한쪽 체결 후 다른 쪽 cancel → ghost position. 사이즈 줄여야 함
- TradFi 시간차 베팅: 큰 뉴스 시 갭 안 채워짐. 백테스트 너무 일반화함
- 토큰화 금 차익: 유동성이 너무 얕아서 실용적 사이즈 안 됨

## 결론

Cross-exchange 차익은 매력적이지만, 자본 효율 / 운영 복잡도 / 거래소 리스크 트레이드오프가 크다. 메인 전략은 안 하고 보조 전략으로만 굴린다.

**메인 전략은 페어 트레이딩 (한 거래소 안에서 BTC vs ETH)이 자본 효율과 운영 단순성에서 우위.**

차익은 시장이 비효율적인 특정 시점에만 알람으로 잡고, 수동으로 들어가는 것도 한 방법.

## 다음 장

다음은 운영 인프라 + 원칙. VPS, 모니터링, 로깅, 백업, 재시작, 보안 — 봇이 24/7 돌아가게 하는 것.
