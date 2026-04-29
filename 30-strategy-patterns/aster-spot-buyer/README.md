# aster-spot-buyer

> 한 줄 요약: Aster (BSC perp DEX) wrapper + PancakeSwap V2 / DEX 직접 swap 실행 모듈. var_aster_farmer / ethereal_aster_farmer 의 hedge venue 로 활용 가능합니다.

## 의존성 (Dependencies)

- Python 3.11+
- `aiohttp`, `web3.py`, `eth-account`
- `ccxt` (Aster wrapper 가 ccxt-style 인터페이스)
- `20-exchange-wrappers/_combined/aster` (factory 통한 호출 시)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "Aster (BSC perp DEX) 의 USDT-M futures API 를 ccxt-style 인터페이스로 감싸 줘. `get_position(symbol)`, `create_order(symbol, side, amount, price, order_type, is_reduce_only)`, `close_position(symbol, position)` 시그니처는 다른 wrapper 와 동일. 추가로 PancakeSwap V2 router 호출로 BSC 위에서 token A → B 직접 swap 도 지원해 줘. 봇이 cross-chain bridge 로 Aster 에 자금 입금할 때 쓸 수 있어야 함."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **BSC USDT decimals = 18 (ERC20 USDT 의 6 이 아님)**: AI 는 거의 항상 USDT 를 6 decimals 로 답함. 실제 BSC USDT (`0x55d398326f99059fF775485246999027B3197955`) 는 18 decimals 라서 잘못된 amount 계산하면 1e12 배 차이 나는 사고가 발생.
- **withdraw 권한 ON 권장**: 보안 측면에서 OFF 가 맞는데, AI 는 "withdraw 도 켜야 자동 출금이 가능합니다" 식으로 답하는 경우가 있음. 운영 시 무조건 OFF.
- **available vs total collateral 혼동**: Aster 는 unrealized PnL 로 `total_collateral` 이 음수 갈 수 있어 `available_collateral` 사용해야 함. AI 가 total 로 답하면 음수 잔고에서도 진입 시도가 발생.

힌트: 이 모듈은 var_aster / ethereal_aster farmer 와 함께 4번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **BSC USDT decimals 18, withdraw OFF 강제, available_collateral 사용** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `aster.py` | Aster wrapper (USDT-M futures, ccxt-style 인터페이스) |
| `dex_trader.py` | PancakeSwap / Uniswap DEX trade 실행 (BSC 위 token swap) |
| `var_aster_farmer.py` | Variational ↔ Aster volume farmer (hedge mode) |

## 주요 함수

```python
class AsterExchange:
    async def init(self) -> "AsterExchange"
    async def get_collateral(self) -> dict        # available_collateral 사용
    async def get_mark_price(self, symbol: str) -> float
    async def get_funding_rate(self, symbol: str) -> Optional[float]
    async def create_order(self, symbol, side, amount, price=None,
                           order_type='market', is_reduce_only=False) -> dict
    async def close_position(self, symbol, position, is_reduce_only=True) -> dict
```

## 사용 예시 (Usage)

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

    # BTC 0.001 매수 (USDT-M futures, BTCUSDT 심볼)
    result = await aster.create_order("BTCUSDT", "buy", 0.001, order_type="market")
    print(result)

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **BSC USDT decimals 18 함정**: BSC 위 USDT 는 18 decimals. ERC20 mainnet USDT (6 decimals) 와 헷갈려 amount 를 1e12 배 적게 보낸 사고가 있었음. `dex_trader.py` 의 swap 호출 시 token-by-token decimals 매핑 테이블 유지.
- **withdraw 권한 ON 시 침해 위험**: API key 가 노출됐을 때 즉시 출금 가능. 신규 발급 시 무조건 trading + read 만 ON, withdraw OFF 강제.
- **unrealized PnL 로 total_collateral 음수**: 큰 손실 포지션을 들고 있으면 total 이 음수 → AI 코드는 `if total_collateral > 0` 같은 가드를 안 짜서 음수 잔고로 진입 시도. `available_collateral` 사용 표준.
- **PancakeSwap router slippage default 0.5%**: 큰 사이즈 swap 시 0.5% 슬리피지로는 부족해 거래 revert 흔함. 사용자가 slippage_bps 를 명시할 수 있도록 인자화.

## 응용 (How this fits with other modules)

- `20-exchange-wrappers/_combined/aster` 의 wrapper 와 동일한 코드 (이 모듈은 그 sub-set 을 standalone 으로 추출)
- `30-strategy-patterns/volume-farmer` 의 var_aster_farmer / ethereal_aster_farmer 가 hedge leg 으로 호출
- `40-realtime-infra/pancake-deposit-helper` 의 bridge 와 결합하면 CEX → BSC → Aster 자동 deposit pipeline 가능

결합 사용 시: PancakeSwap swap 후 Aster deposit 까지 한 흐름으로 묶을 때, swap 결과 token 의 wallet balance polling 후에 Aster API 호출하는 sleep 가드 필요 (BSC block confirm 시간).

## 환경변수

```env
ASTER_API_KEY=<...>
ASTER_SECRET=<...>
BSC_RPC=https://bsc-dataseed1.binance.org
PRIVATE_KEY=<EVM_PK>           # PancakeSwap swap 용
```

## 거래소 가입 링크

- Aster: https://www.asterdex.com/en/referral/e70505
- Variational: https://omni.variational.io/?ref=OMNICOINMAGE
