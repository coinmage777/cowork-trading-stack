# pancake-deposit-helper

PancakeSwap V2 BSC 스왑 + Stargate / Across V3 cross-chain bridge. CEX → 거래소 deposit + farmer 자금 이동 자동화.

## 핵심 기능

- **PancakeSwap V2 swap**: BSC 위에서 token A → B 직접 swap (router 호출)
- **Stargate V2 bridge**: USDC/USDT cross-chain (BSC ↔ Ethereum ↔ Arbitrum 등)
- **Across V3 bridge**: 빠른 cross-chain (Optimism/Arbitrum/Base 등)
- **Slippage 관리**: 0.5% default, 큰 사이즈 시 사용자 지정
- **Gas 추정**: BSC RPC eth_gasPrice + 안전 buffer 1.5x

## 파일 구조

```
pancake-deposit-helper/
├── pancake_swap.py        — PancakeSwap V2 router 직접 호출
├── stargate_bridge.py     — Stargate V2 cross-chain transfer
├── across_bridge.py       — Across V3 fast bridge
└── bridge_client.py       — 통합 진입점 (chain-agnostic)
```

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

## 사용 예시

```python
import asyncio, os
from pancake_swap import PancakeSwap

async def main():
    pancake = PancakeSwap(
        rpc=os.getenv("BSC_RPC", "https://bsc-dataseed1.binance.org"),
        private_key=os.getenv("PRIVATE_KEY"),
    )

    # USDT (BSC) → BUSD swap
    USDT = "0x55d398326f99059fF775485246999027B3197955"
    BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"

    tx_hash = await pancake.swap_exact_tokens_for_tokens(
        amount_in_wei=int(100 * 10**18),  # 100 USDT
        token_in=USDT,
        token_out=BUSD,
        slippage_bps=50,
    )
    print(f"swap tx: {tx_hash}")

asyncio.run(main())
```

## 환경변수

```env
PRIVATE_KEY=<EVM_PK>
BSC_RPC=https://bsc-dataseed1.binance.org
ETH_RPC=<...>
ARB_RPC=<...>
OP_RPC=<...>
```

## 의존성

- `web3.py` (RPC 호출)
- `eth-account`
- `eth-utils`
