# perp-dex-wrappers

> **Advanced — 처음이라면 setup-guides/ 의 1-2 개 거래소만 보셔도 충분합니다.**
> 본 _combined/ 폴더는 22 개 거래소 wrapper 의 union 으로, 빌더 단계에서 reference 로 사용됩니다.

> 한 줄 요약: 22개+ Perp DEX 에 동일 인터페이스 (`get_position`, `get_collateral`, `get_mark_price`, `create_order`, `close_position`, `get_open_orders`) 를 씌운 wrapper 라이브러리. 전략 코드가 거래소를 추상화한 채로 작성될 수 있도록 하기 위한 모듈입니다.

## 거래소별 특이점 (Quick reference)

22 개 거래소 모두 본 `_combined/` 안에 통합되어 있습니다. 각 거래소의 핵심 quirk:

- **Hyperliquid**: HL 메인 + HIP-3 builder rotation. cloid prefix (예: Miracle `0x4d455243`) 가 포인트 attribution 의 핵심
- **Lighter**: SDK 가 `__init__` 에서 sync HTTP → asyncio deadlock. subprocess bridge 로 격리 필수
- **GRVT**: `account_id` 가 numeric (address 아님), 심볼 `BTC_USDT_Perp` 형식, EIP-712 서명
- **Aster**: BSC chain, Binance-style REST + listenKey, builder rebate 구조
- **Standx**: BSC chain, EIP-712 personal_sign. **eth_account 0.13+ 의 `signature.hex()` 가 `0x` prefix 누락 → 401 침묵 실패** (fix: `if not sig.startswith("0x"): sig = "0x"+sig`). MM uptime 프로그램 (5M/month pool) — 호가 ±10bps + 시간당 30 분 이상 유지 시 unlock
- 나머지 17 개 거래소 (Backpack, Decibel, EdgeX, Ethereal, Extended, Hotstuff, Katana, Kraken, Nado, Ostium, Pacifica, Paradex, Reya, TreadFi, Variational, Bulk 등): 각 거래소별 폴더 내 코드 참조


## 의존성 (Dependencies)

- Python 3.11+ (Reya 만 3.12 필수)
- 거래소별 SDK (각 폴더 `requirements.txt`):
  - `hyperliquid-python-sdk`, `lighter-python-sdk`, `paradex-py`, `ccxt` 등
- `aiohttp`, `websockets`, `eth-account`, `msgpack`, `keccak`

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "여러 perp DEX 의 SDK가 인터페이스가 제각각인데, 전략 코드 한 벌로 모든 거래소에 같은 호출을 보낼 수 있게 wrapper 추상 클래스를 만들어 줘. 메소드 시그니처는 `get_position(symbol)`, `create_order(symbol, side, amount, price, order_type, is_reduce_only)` 처럼 통일하고, 거래소별 심볼 포맷 차이는 symbol_adapter 에 모아 줘. SDK 가 sync HTTP 를 `__init__` 에서 호출하는 거래소는 subprocess bridge 로 분리해 줘."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **HL signature 의 `0x` prefix 누락**: msgpack + keccak + ECDSA 까지는 맞추는데, `eth_account` 버전에 따라 `signature.signature.hex()` 가 `0x` 를 빼고 리턴하는 경우가 있어서 401 이 나는 걸 못 알아챔. 우리 코드는 `r/s/v` 를 따로 직렬화하는 식으로 우회.
- **Lighter SDK 의 sync HTTP in `__init__`**: 비동기 코드에서 `await create_exchange("lighter")` 하는 순간 이벤트 루프가 sync 호출에 걸려 deadlock. AI는 "asyncio.to_thread 로 감싸면 됨" 식으로 답하지만 그 안에서도 SDK 내부 lock 이 풀리지 않아서, 결국 별도 venv + subprocess bridge 가 정답.
- **EIP-712 typed data 의 chain_id / domain separator 혼동**: Reya/Paradex/RiseX 가 각각 chain_id 가 다른데 (특히 RiseX 의 4153), AI 는 mainnet 기본값을 그대로 넣는 경우가 있음.

힌트: 이 모듈은 22번 이상 재작업했고, 그때마다 AI가 놓친 것은 SDK의 sync init / 시그니처 byte ordering / 거래소별 심볼 포맷 (`BTC` vs `BTC-PERP` vs `BTCUSDT` vs `hyna:BTC`) 차이입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `_common/factory.py` | `create_exchange(name, **keys)` 단일 진입점 |
| `_common/hyperliquid_base.py` | HL 공통 베이스 (msgpack action, builder rotation) |
| `_common/hl_sign.py` | HL signature helpers (Rust 로 마이그 가능) |
| `_common/symbol_adapter.py` | 거래소별 심볼 포맷 변환 (`BTC` ↔ `BTC-PERP` ↔ `hyna:BTC` 등) |
| `_common/base.py` | `Exchange` 추상 클래스 |
| `hyperliquid/hyperliquid.py` | HL wrapper + builder code rotation 지원 |
| `lighter/lighter.py` | Lighter wrapper, subprocess bridge 통해 호출 |
| `grvt/grvt.py` | GRVT wrapper (account_id + EIP-712) |
| `paradex/paradex.py` | StarkNet, JWT + L1/L2 키 분리 |
| `backpack/backpack.py` | Backpack (API key + ED25519) |
| `aster/aster.py` | BSC perp DEX, USDT-M futures |
| `pacifica/pacifica.py` | HL HIP-3 builder, hyna:BTC 심볼 |
| `edgex/edgex.py` | StarkNet DEX (account_id + StarkNet PK) |
| `risex/risex.py` | RISE Chain, EIP-712 VerifyWitness/RegisterSigner |
| `nado/nado.py` | HL HIP-3 기반 builder |
| `variational/variational.py` | RFQ 방식 (funding feed 없음) |
| 그 외 | `extended/`, `ethereal/`, `ostium/`, `katana/`, `kraken/`, `decibel/`, `hotstuff/`, `superstack/`, `treadfi_*/`, `standx/` |

## 지원 거래소 매트릭스

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
| TreadFi | `treadfi_hl/`, `treadfi_pc/` | HL/PC 기반 builder |
| HyENA / Miracle / DreamCash / Bullpen | (HL aliases) | builder code only, base wrapper 공유 |
| Decibel | `decibel/` | pre-launch perp DEX |
| Variational | `variational/` | RFQ 방식 (funding feed 없음) |
| Ostium | `ostium/` | RWA perp DEX |
| Katana | `katana/` | Hyperliquid 기반 |
| Ethereal | `ethereal/` | Ethena 생태계 (USDe perp) |
| Extended | `extended/` | StarkNet DEX |
| Hotstuff | `hotstuff/` | HL 기반 |
| Nado | `nado/` | HL 기반 |

## 사용 예시 (Usage)

```python
import asyncio
from perp_dex_wrappers._common.factory import create_exchange

async def main():
    # Hyperliquid (builder rotation 포함)
    hl = await create_exchange(
        "hyperliquid",
        wallet="<USER_WALLET>",
        agent_key="<AGENT_PRIVATE_KEY>",
        builder_rotation=[
            {"name": "miracle",   "builder_code": "<...>", "fee_pair": {"base": "1 50"}},
            {"name": "dreamcash", "builder_code": "<...>", "fee_pair": {"base": "1 50"}},
        ],
    ).init()

    col = await hl.get_collateral()
    print(f"HL collateral: ${col['total_collateral']}")

    pos = await hl.get_position("BTC")
    if pos:
        print(f"BTC position: {pos['side']} {pos['size']} @ {pos['entry_price']}")

    result = await hl.create_order(
        symbol="BTC", side="buy", amount=0.001,
        order_type="market", is_reduce_only=False,
    )
    print(result)

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **HL signature 의 hex prefix 누락 → 401**: `eth_account` 버전 차이로 `signature.hex()` 가 `0x` prefix 를 떨어뜨리는 케이스가 있어 처음 401 만 보고 디버깅하느라 며칠 날림. `_common/hl_sign.py` 에서 `r/s/v` 를 따로 직렬화하도록 통일.
- **Lighter SDK sync init → asyncio deadlock**: `LighterClient(...)` 가 내부에서 sync `requests.get` 을 부르며 main loop 를 잡아먹음. 격리 venv 띄워 `subprocess_wrapper.py` 로 stdin/stdout JSON 프로토콜 운용.
- **Lighter subprocess stdout 오염**: subprocess 안 `print()` 로그가 그대로 부모로 흘러 들어가 JSON parse error 폭주. `subprocess_wrapper.py` 첫 char 가드 추가 (`{` 또는 `[` 가 아니면 stderr 로 전환).
- **GRVT parse_order 의 metadata KeyError**: 에러 응답 dict 에는 `metadata` 키 자체가 없는 경우가 있어 `.get('metadata', {}).get('client_order_id')` 로 방어.
- **HL builder code + cloid prefix 누락**: Miracle 포인트가 안 잡힘 → 확인해 보니 cloid 가 `0x4d455243` (= "MERC") 으로 시작해야 builder 가 인식. `hyperliquid_base.py` 의 `_make_cloid` 에 prefix 강제.
- **HIP-3 asset_id 계산 실수**: `140000 + perpDexIndex` 인데 (예: HyENA index=4 → 140004), AI 가 그냥 `40004` 같은 값으로 답하는 경우가 있어 두 차례 잘못된 주문이 나감.
- **RiseX `_target` precedence 함정**: `addrs.router or perp_v2.orders_manager or contract_addresses.perps_manager` 식의 Python `or` 체인은 빈 문자열/None 평가 우선순위 때문에 의도와 다른 주소가 선택될 수 있어 명시적 if-else 로 재작성.

## 응용 (How this fits with other modules)

- `30-strategy-patterns/_combined/pair_trader.py` → **이 모듈** → 거래소 (HL/Lighter/Aster/...)
- `30-strategy-patterns/volume-farmer/*` → **이 모듈** 두 개 인스턴스 (long leg + short leg)
- `40-realtime-infra/cross-venue-arb-scanner` → **이 모듈** + ccxt 페어로 spot/perp 동시 모니터
- `50-rust-acceleration/rust-services/hl-sign` → 이 모듈의 `_common/hl_sign.py` 핫패스 대체

결합 사용 시: factory.py 는 거래소 이름만 바꾸면 되도록 설계되었기 때문에, 전략 코드는 `create_exchange(name, **keys)` 한 줄만 바꿔서 다른 venue 로 옮겨갈 수 있습니다.

## 환경변수 패턴

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

## 거래소 가입 링크

- Hyperliquid: https://miracletrade.com/?ref=coinmage
- Lighter: https://app.lighter.xyz/?referral=GMYPZWQK69X4
- Aster: https://www.asterdex.com/en/referral/e70505
- Pacifica: https://app.pacifica.fi?referral=cryptocurrencymage
- GRVT: https://grvt.io/exchange/sign-up?ref=1O9U2GG
- EdgeX: https://pro.edgex.exchange/referral/570254647
- Reya: https://app.reya.xyz/trade?referredBy=8src0ch8
- Extended: https://app.extended.exchange/join/COINMAGE
- Variational: https://omni.variational.io/?ref=OMNICOINMAGE
- Standx: https://standx.com/referral?code=coinmage
- Nado: https://app.nado.xyz?join=NX9LLaL
