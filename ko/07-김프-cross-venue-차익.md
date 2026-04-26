# 07. 김프 + Cross-Venue 차익

김프 (Kimchi Premium) — 한국 거래소(업비트, 빗썸, 코인원 등)에서 비트코인이나 다른 코인 가격이 글로벌 거래소(바이낸스, OKX 등)보다 높게 거래되는 현상.

이 차이를 차익으로 만드는 방법을 정리한다.

## 김프란

원리:
- 한국은 자본 통제가 있어서 USDT/USD를 자유롭게 들고 나가기 어려움
- 한국 시장의 매수 수요 > 공급 → KRW 기준 가격이 USD 가격(원화 환산) 대비 비쌈
- 평소 +1~3%, 시장 과열 시 +10~20%까지

### 김프 보는 법

기본 공식:
```
김프 = (한국 거래소 KRW 가격 / (글로벌 USD 가격 × USD/KRW 환율) - 1) × 100
```

예:
- 업비트 BTC: 100,000,000 KRW
- 바이낸스 BTC: 70,000 USD
- 환율: 1380 KRW/USD
- → 김프 = (100,000,000 / (70,000 × 1380) - 1) × 100 = +3.5%

[kimpga.com](https://kimpga.com), [coinview.io](https://coinview.io) 같은 사이트에서 실시간 확인 가능.

## 김프 차익의 두 가지 방향

### 1) 김프가 높을 때 — 들어가는 흐름

- 글로벌에서 USDT로 코인 매수
- 한국 거래소로 전송
- KRW로 매도 (높은 가격에 팔림)
- KRW를 USDT로 바꿔서 나가야 하는데 → 여기가 어려움

**문제**: 한국 거래소에서 USD/USDT 매수 / 출금 못 함. KRW를 들고 나가는 채널이 제한적.

전통 방식:
- KRW로 다른 코인 매수 → 글로벌 거래소로 전송 → USDT 매도 (역김프 시점이면 손실)
- 또는 OTC / P2P → 환전 (수수료 + 위험)

### 2) 김프가 낮을 때 (역김프) — 나가는 흐름

- 한국에 미리 코인을 사둠 (저렴할 때)
- KRW로 매도 (정상 김프 시점에)
- 차익

이건 시간 차가 길고, 김프가 음수일 때만 가능 (드물고 짧음).

### 보는 현실

순수 김프 차익은 어렵다. **자본 흐름의 비대칭** 때문이다. 한국 → 글로벌이 어려워서 차익 기회가 살아남는 것. 그 어려움을 운영자도 똑같이 겪는다.

그래서 보는 더 실용적 형태:

## Cross-Venue 차익 — 더 일반화된 형태

김프는 cross-venue 차익의 한 케이스. 더 일반적으로:

- **거래소 A 가격 ≠ 거래소 B 가격**일 때
- 양쪽에 자본 미리 분배 (전송 안 해도 됨)
- A에서 매수 + B에서 매도 (또는 A에서 short + B에서 long)
- 가격 수렴할 때까지 보유

### 어떤 차이를 노릴 수 있나

1. **CEX vs CEX 가격 차이** — 흔치 않지만 큰 변동 시 발생 (예: 바이낸스 -1% / OKX +0.5%)
2. **CEX vs DEX 가격 차이** — [Hyperliquid](https://miracletrade.com/?ref=coinmage) / dYdX vs 바이낸스 (펀딩 / 청산 캐스케이드 시)
3. **현물 vs Perp 베이시스** — 현물 거래소에서 매수 + Perp에서 short
4. **A 거래소 펀딩 vs B 거래소 펀딩** — 같은 자산, 펀딩비 차이로 차익

### 자주 쓰는 형태: 펀딩비 차익

같은 자산이 거래소마다 펀딩비가 다르다. 예:
- 바이낸스 BTC perp 펀딩: +0.05% (롱이 숏에 줌)
- Hyperliquid BTC perp 펀딩: -0.02% (숏이 롱에 줌)
- 차이: 0.07% / 8시간 = 0.21% / 일

전략:
- 바이낸스 BTC short (펀딩 받음 +0.05%)
- Hyperliquid BTC long (펀딩 받음 +0.02%)
- 가격은 둘이 같이 움직이니 노출 0
- 펀딩만 누적: 일 0.21% × 365 = 76% APR (이론치)

### 펀딩 차익의 리스크

- **펀딩비는 고정 아님** — 다음 라운드에 뒤집히면 즉시 손실 시작
- **양쪽 가격 충분히 안 따라옴** — 베이시스 발생 시 가격 노출 발생
- **거래소 리스크** — 한쪽 거래소가 출금 막히거나 청산되면 한쪽 노출
- **자본 락업** — 양쪽에 자본 묶임 → ROI 계산 시 자본 비용 고려

펀딩 차익 하려면 최소:
- 동일 자산 양쪽 펀딩 |차이| > 0.03% / 8h 지속 6시간 이상
- 양쪽 거래소 안정성 검증 끝
- 시장 베타 1에 가까운 자산 (BTC, ETH)
- 사이즈는 자본의 30~40% 이하 (충격 + 청산 여유)

### CEX vs DEX 베이시스 — 큰 무브 시 기회

큰 청산 캐스케이드 발생 시 (예: BTC 5% 빠짐):
- DEX (Hyperliquid)는 청산 인덱스가 다른 가격 피드 → 잠시 오버슈트
- CEX (바이낸스)는 더 안정적

→ DEX에서 매수 + CEX에서 매도 → 인덱스 수렴 후 청산

이거 자동화하려면:
- 두 거래소 mark price 실시간 모니터링 (WebSocket)
- 차이 임계값 (예: 0.5%) 넘으면 진입
- 임계값 회복 시 (0.1% 이하) 청산
- 둘 다 충분한 유동성 확인

이 패턴은 백테스트만 해보고 라이브 안 굴려봤다. 큰 무브 빈도가 낮고, 진입 시 양쪽 동시 체결이 어려워서.

## 김프 자동화 — 현실적 시나리오

진짜 김프 차익을 자동화하려면 사실상 두 사람 시스템이 효율적이다:

- **글로벌 사이드 운영자**: USDT → 코인 매수 → 한국 전송
- **한국 사이드 운영자**: 코인 받기 → KRW 매도 → KRW 들고 다음 라운드 대기

혼자 하면 KRW를 다시 USDT로 바꾸는 채널이 병목이다.

### 실제 운영하는 방식

본격 김프 봇은 안 굴린다. 대신 **김프가 비정상적으로 높을 때** 알림 → 수동 판단으로 들어감. 자주 있는 일 아니라 자동화 ROI가 안 나옴.

알림 봇 구조 (간단):

```python
async def kimchi_monitor():
    while True:
        upbit_btc = await fetch_upbit("KRW-BTC")
        binance_btc = await fetch_binance("BTCUSDT")
        usdkrw = await fetch_fx_rate()
        
        kimchi = (upbit_btc / (binance_btc * usdkrw) - 1) * 100
        
        if kimchi > 5:  # 5% 이상이면 알림
            await notify(f"🚨 김프 {kimchi:.2f}% — 수동 검토")
        
        await asyncio.sleep(60)
```

이 정도가 김프에 대한 자동화의 실용적 최대치다.

## 더 현실적인 차익 자동화: 펀딩 모니터

더 자주 쓰는 건 펀딩비 모니터:

```python
async def funding_monitor():
    exchanges = ["binance", "bybit", "okx", "hyperliquid", "lighter"]
    while True:
        rows = []
        for ex in exchanges:
            for sym in ["BTC", "ETH", "SOL"]:
                fr = await get_funding_rate(ex, sym)
                rows.append({"ex": ex, "sym": sym, "fr": fr})
        
        # 같은 sym에서 max - min > threshold 찾기
        for sym in ["BTC", "ETH", "SOL"]:
            sym_rates = [r for r in rows if r["sym"] == sym]
            mx = max(sym_rates, key=lambda x: x["fr"])
            mn = min(sym_rates, key=lambda x: x["fr"])
            diff = mx["fr"] - mn["fr"]
            if diff > 0.01:  # 0.01% / 8h 차이
                await notify(f"{sym}: {mx['ex']}({mx['fr']:.4f}) vs {mn['ex']}({mn['fr']:.4f})")
        
        await asyncio.sleep(300)
```

이걸 매 5분마다 돌리면 펀딩 차익 기회 알람 받음. 들어갈지 말지는 판단.

## 다음 장

다음은 백테스팅 — 전략을 라이브에 박기 전에 어떻게 검증하는가. Minara 기반 워크플로우.
