# perp-dex-wrappers

22+ Perp DEX 통합 wrapper 라이브러리. 모든 거래소가 동일 인터페이스 (`get_position`, `get_collateral`, `get_mark_price`, `create_order`, `close_position`, `get_open_orders`)를 노출하므로, 전략 코드는 거래소를 추상화한 채 작성 가능.

## 핵심 기능

- **Factory pattern**: `create_exchange("hyperliquid", wallet=..., agent_key=...).init()` → 단일 진입점
- **격리 venv**: SDK가 sync HTTP 호출하는 거래소 (Lighter/GRVT/Reya/Bulk)는 자동으로 subprocess bridge로 분리
- **Builder rotation**: 같은 HL 지갑에서 여러 builder code (Miracle/DreamCash/HyENA/Bullpen 등) 라운드로빈 → 중복 인스턴스 없이 볼륨 분배
- **EIP-712 typed data signing**: Reya/Paradex/RiseX/GRVT 등 EIP-712 서명 거래소 통일 처리
- **Symbol adapter**: 거래소별 심볼 포맷 (`BTC` vs `BTC-PERP` vs `BTCUSDT` vs `hyna:BTC`) 자동 변환

## 지원 거래소

| 거래소 | 폴더 | 특이사항 |
|--------|------|----------|
| Hyperliquid | `hyperliquid/` | HL 메인 + builder code rotation 지원 |
| Lighter | `lighter/` | 격리 venv (SDK sync init) |
| GRVT | `grvt/` | 격리 venv, account_id + EIP-712 |
| Paradex | `paradex/` | StarkNet, JWT + L1/L2 키 분리 |
| Backpack | `backpack/` | API key + ED25519 signing |
| Aster | `aster/` | BSC perp DEX, USDT-M futures |
| Pacifica | `pacifica/` | HL HIP-3 builder, hyna:BTC 심볼 |
| EdgeX | `edgex/` | StarkNet DEX, account_id + StarkNet PK |
| Reya | `reya/` | Arbitrum Orbit, Python 3.12 필수, 격리 venv |
| RiseX | `risex/` | RISE Chain, EIP-712 VerifyWitness/RegisterSigner |
| Standx | `standx/` | BSC DEX |
| TreadFi | `treadfi/` | HL 기반 builder |
| HyENA | `hyena/` | HL HIP-3 builder, USDe 마진 통합 |
| Decibel | `decibel/` | pre-launch perp DEX |
| Variational | `variational/` | RFQ 방식 (funding feed 없음) |
| Ostium | `ostium/` | RWA perp DEX |
| Katana | `katana/` | Hyperliquid 기반 |
| Ethereal | `ethereal/` | Ethena 생태계 (USDe perp) |
| Bulk | `bulk/` | 테스트넷 (격리 venv, Ed25519) |
| Extended | `extended/` | StarkNet DEX |
| Hotstuff | `hotstuff/` | HL 기반 |
| Nado | `nado/` | HL 기반 |
| Miracle / DreamCash / Bullpen | (HL aliases) | builder code only, base wrapper 공유 |

## 파일 구조

```
perp-dex-wrappers/
├── _common/
│   ├── factory.py           — create_exchange(name, **keys) 진입점
│   ├── hyperliquid_base.py  — HL 공통 베이스 (msgpack action, builder rotation)
│   ├── hl_sign.py           — HL signature helpers (Rust로 마이그 가능)
│   ├── symbol_adapter.py    — 거래소별 심볼 포맷 변환
│   └── base.py              — Exchange 추상 클래스
├── hyperliquid/
├── lighter/
├── grvt/
├── ... (각 거래소 폴더)
```

각 거래소 폴더에는 다음 파일:
- `{exchange}.py` — wrapper 메인 클래스
- `{exchange}_ws_client.py` (있는 경우) — WebSocket 구독
- `__init__.py`

## 주요 클래스 / 메소드

```python
class HyperliquidExchange:
    async def init(self) -> "HyperliquidExchange"
    async def get_collateral(self) -> dict  # {total_collateral, available_collateral, ...}
    async def get_position(self, symbol: str) -> Optional[dict]
    async def get_mark_price(self, symbol: str) -> float
    async def create_order(self, symbol, side, amount, price=None, order_type='market',
                           is_reduce_only=False) -> dict
    async def close_position(self, symbol: str, position: dict, is_reduce_only=True) -> dict
    async def cancel_orders(self, symbol: str = None) -> dict
    async def get_open_orders(self, symbol: str = None) -> List[dict]
    async def get_funding_rate(self, symbol: str) -> Optional[float]
```

## 사용 예시

```python
import asyncio
from perp_dex_wrappers._common.factory import create_exchange

async def main():
    # Hyperliquid
    hl = await create_exchange(
        "hyperliquid",
        wallet="<USER_WALLET>",
        agent_key="<AGENT_PRIVATE_KEY>",
        builder_rotation=[
            {"name": "miracle", "builder_code": "<...>", "fee_pair": {"base": "1 50"}},
            {"name": "dreamcash", "builder_code": "<...>", "fee_pair": {"base": "1 50"}},
        ],
    ).init()

    # 잔고 + 포지션
    col = await hl.get_collateral()
    print(f"HL collateral: ${col['total_collateral']}")

    pos = await hl.get_position("BTC")
    if pos:
        print(f"BTC position: {pos['side']} {pos['size']} @ {pos['entry_price']}")

    # 주문
    result = await hl.create_order(
        symbol="BTC", side="buy", amount=0.001,
        order_type="market", is_reduce_only=False,
    )
    print(result)

asyncio.run(main())
```

## 의존성

- Python 3.11+ (Reya는 3.12 필수)
- 거래소별 SDK (각 폴더 `requirements.txt`):
  - `hyperliquid-python-sdk`, `lighter-python-sdk`, `paradex-py`, `ccxt` 등
- `aiohttp`, `websockets`, `eth-account`, `msgpack`, `keccak`

## 환경변수

각 거래소별 키는 모듈 호출 시 인자로 주입. `.env`에 다음 키들 정의:
```env
PRIVATE_KEY=<EVM_PK>
HL_AGENT_KEY=<HL_AGENT_PK>
LIGHTER_API_KEY=<LIGHTER_API_KEY>
LIGHTER_ACCOUNT_INDEX=<NUM>
GRVT_ACCOUNT_ID=<NUM>
PARADEX_L1_KEY=<L1_PK>
PARADEX_L2_KEY=<L2_PK>
ASTER_API_KEY=<...>
ASTER_SECRET=<...>
EDGEX_ACCOUNT_ID=<NUM>
EDGEX_STARKNET_PK=<...>
REYA_PRIVATE_KEY=<...>
RISEX_SESSION_KEY=<EIP712_SESSION_KEY>
```
