# aster-spot-buyer

[Aster](https://www.asterdex.com/en/referral/e70505) (BSC perp DEX) 현물 자동 매수 + farmer hedge leg. BNB Smart Chain의 저비용 perp 인터페이스로 빠른 진입/청산. var_aster_farmer / ethereal_aster_farmer의 hedge venue로 활용 가능.

## 핵심 기능

- **Aster wrapper**: ccxt-style 인터페이스 (get_position / create_order / close_position)
- **USDT-M futures**: BTCUSDT / ETHUSDT / SOLUSDT 등 standard symbol
- **DEX trader**: PancakeSwap V2 + DEX 직접 swap (cross-chain bridge 통합)
- **Farmer hedge leg**: var_aster / ethereal_aster volume farmer의 short side로 사용

## 파일 구조

```
aster-spot-buyer/
├── aster.py             — Aster wrapper (mpdex/exchanges에서 추출)
├── dex_trader.py        — PancakeSwap/Uniswap DEX trade 실행
├── var_aster_farmer.py  — Variational ↔ Aster volume farmer (hedge mode)
└── README.md
```

## 주요 함수

```python
class AsterExchange:
    async def init(self) -> "AsterExchange"
    async def get_collateral(self) -> dict
    async def get_mark_price(self, symbol: str) -> float
    async def get_funding_rate(self, symbol: str) -> Optional[float]
    async def create_order(self, symbol, side, amount, price=None,
                           order_type='market', is_reduce_only=False) -> dict
    async def close_position(self, symbol, position, is_reduce_only=True) -> dict
```

## 사용 예시

```python
import asyncio, os
from aster import AsterExchange

async def main():
    aster = await AsterExchange(
        api_key=os.getenv("ASTER_API_KEY"),
        secret=os.getenv("ASTER_SECRET"),
    ).init()

    col = await aster.get_collateral()
    print(f"Aster: ${col['available_collateral']}")

    # BTC 0.001 매수
    result = await aster.create_order("BTCUSDT", "buy", 0.001, order_type="market")
    print(result)

asyncio.run(main())
```

## 환경변수

```env
ASTER_API_KEY=<...>
ASTER_SECRET=<...>
BSC_RPC=https://bsc-dataseed1.binance.org
```
