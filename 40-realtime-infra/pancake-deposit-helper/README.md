# pancake-deposit-helper

> 한 줄 요약: PancakeSwap V2 (BSC) 직접 swap + Stargate / Across V3 cross-chain bridge 라이브러리. CEX → 거래소 deposit, farmer 자금 이동, BSC 위 token A → B 변환을 자동화하기 위한 모듈입니다.

## 의존성 (Dependencies)

- `web3.py` (RPC 호출)
- `eth-account`, `eth-utils`
- `aiohttp` (Stargate / Across API 통신)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "BSC 위에서 PancakeSwap V2 router 를 직접 호출해 token A → B swap 하는 함수를 만들어 줘. `swap_exact_tokens_for_tokens(amount_in_wei, token_in, token_out, slippage_bps=50, deadline_sec=300)` 시그니처. gas 는 `eth_gasPrice` × 1.5 buffer. 추가로 Stargate V2 (BSC ↔ Ethereum ↔ Arbitrum) 와 Across V3 (Optimism/Arbitrum/Base) cross-chain bridge 도 같은 인터페이스로 감싸서 `bridge_client.py` 단일 진입점에서 호출 가능하게 해 줘."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **BSC USDT decimals = 18 (mainnet ERC20 USDT 의 6 이 아님)**: AI 는 거의 항상 USDT 를 6 으로 가정. 실제 BSC USDT (`0x55d398326f99059fF775485246999027B3197955`) 는 18 decimals. amount 계산이 1e12 배 차이 나는 사고가 발생.
- **slippage_bps default 부족**: PancakeSwap V2 는 0.5% (50 bps) default 로 큰 사이즈 swap 에서 자주 revert. 사용자가 명시적으로 늘릴 수 있도록 인자화 필요.
- **token approve 누락**: ERC20 swap 전 router contract 에 approve 안 하면 transfer revert. 매번 approve 호출 + gas cost 가 아까우면 max approve (`2**256-1`) 한번이 표준이지만 AI 는 매번 approve 코드를 짜는 경우가 많음.
- **deadline 단위**: `deadline_sec` 을 epoch 가 아니라 relative 로 받는데 AI 가 헷갈려서 unix timestamp 를 그대로 넣는 경우.

힌트: 이 모듈은 BSC 자금 이동 자동화에 4번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **BSC USDT decimals 18, approve 캐싱, slippage_bps 명시화, gas estimation buffer 1.5x** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `pancake_swap.py` | PancakeSwap V2 router 직접 호출 (`swap_exact_tokens_for_tokens` 등) |
| `stargate_bridge.py` | Stargate V2 cross-chain transfer (USDC/USDT) |
| `across_bridge.py` | Across V3 fast bridge (Optimism/Arbitrum/Base 등) |
| `bridge_client.py` | 통합 진입점 (chain-agnostic, src/dst 입력 받아 적절한 bridge 선택) |

## 주요 함수

```python
class PancakeSwap:
    def __init__(self, rpc: str, private_key: str)
    async def swap_exact_tokens_for_tokens(
        self,
        amount_in_wei: int,
        token_in: str,    # ERC20 address
        token_out: str,
        slippage_bps: int = 50,  # 0.5%
        deadline_sec: int = 300,
    ) -> str  # tx_hash
    async def get_amounts_out(self, amount_in_wei: int, path: List[str]) -> int

class StargateBridge:
    async def bridge(
        self,
        src_chain: str,    # "BSC"
        dst_chain: str,    # "Ethereum"
        token: str,         # "USDC"
        amount_wei: int,
    ) -> str  # tx_hash
```

## 사용 예시 (Usage)

```python
import asyncio, os
from pancake_swap import PancakeSwap

async def main():
    pancake = PancakeSwap(
        rpc=os.getenv("BSC_RPC", "https://bsc-dataseed1.binance.org"),
        private_key=os.getenv("PRIVATE_KEY"),
    )

    # USDT (BSC, 18 decimals!) → BUSD swap
    USDT = "0x55d398326f99059fF775485246999027B3197955"
    BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"

    tx_hash = await pancake.swap_exact_tokens_for_tokens(
        amount_in_wei=int(100 * 10**18),  # 100 USDT (BSC = 18 decimals)
        token_in=USDT,
        token_out=BUSD,
        slippage_bps=50,
    )
    print(f"swap tx: {tx_hash}")

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **BSC USDT decimals 18 → 1e12 배 amount 사고**: 처음 mainnet ERC20 USDT 6 decimals 가정으로 swap 하니 `100 * 10**6 = 1e8` wei 만 보내고 거래는 성공한 것처럼 보였는데 사실상 1e-10 USDT 만 swap 됨. token 별 decimals 매핑 테이블 필수.
- **PancakeSwap router approve 누락**: 신규 token 첫 swap 시 approve 안 했더니 router 가 transferFrom 실패. swap 전 allowance 체크 + 부족하면 approve.
- **Stargate V2 의 dst chain ID 불일치**: Stargate 가 사용하는 chain ID 는 LayerZero 의 endpoint ID 라서 EVM chain ID 와 다름 (예: BSC 의 LZ ID = 30102, EVM = 56). 잘못 넣으면 transfer 가 다른 체인으로 보내짐.
- **Across V3 fee feeq 사전 quote 누락**: bridge 호출 전 `/suggested-fees` 엔드포인트로 quote 받아야 정확한 amount 가 dst 에 도착. 그냥 보내면 fee 가 source 에서 차감되어 부족분 transfer 가 됨.
- **gas estimation 1.0x → out of gas**: BSC 가끔 mempool 혼잡 시 gas price 급변. `eth_gasPrice` × 1.5 buffer 넣은 후 안정.

## 응용 (How this fits with other modules)

- `30-strategy-patterns/aster-spot-buyer` 의 Aster deposit 직전 단계로 swap (USDT → Aster collateral) 자동화
- `30-strategy-patterns/volume-farmer` 의 자금 분산 시 cross-chain bridge 호출
- CEX 출금 → BSC 도착 → 본 모듈로 swap → Aster/Standx 등 BSC perp DEX 입금 의 pipeline 마지막 단계

결합 사용 시: bridge → swap 의 sleep 가드 (BSC block confirm) 필요. 일반적으로 bridge 도착 polling 후 swap.

## 환경변수

```env
PRIVATE_KEY=<EVM_PK>
BSC_RPC=https://bsc-dataseed1.binance.org
ETH_RPC=<...>
ARB_RPC=<...>
OP_RPC=<...>
```

## 거래소 가입 링크

- Aster: https://www.asterdex.com/en/referral/e70505
- Standx: https://standx.com/referral?code=coinmage
