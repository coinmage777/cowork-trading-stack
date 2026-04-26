# predict-fun-sniper

Predict.fun (BSC 기반 1시간 prediction market) mid-time sniper. JWT auth + asset 회전 + 변동성 기반 확률 모델 + BNB 가스 monitor.

## 핵심 기능

- **1시간 마켓 진입**: 매 시간 자동 생성되는 BTC/ETH/SOL/BNB Up/Down 마켓에 mid-time entry
- **변동성 기반 확률 모델**: 자산별 1분 σ (BTC 0.12% / ETH 0.18% / SOL 0.25% / BNB 0.15%) → Normal CDF로 P(target hit) 계산
- **자산별 edge buffer**: BTC +0% / ETH +3% / SOL +3% / BNB +2% — 변동성 큰 자산은 더 큰 edge 요구
- **Slug-direct fetcher fallback**: categories listing이 가까운 만기 누락 시 슬러그 직접 GET (BTC/ETH × ±2시간)
- **BNB gas monitor**: Signer EOA 잔고 < 0.002 BNB 시 Telegram 알림, < 0.0005 시 claim loop 차단
- **JWT 자동 갱신**: 30분 만료 전 재발급

## 파일 구조

```
predict-fun-sniper/
├── predict_sniper.py       — 메인 sniper loop
├── predict_client.py       — Predict.fun SDK wrapper (predict-sdk)
├── bnb_monitor.py          — Signer EOA BNB 잔고 모니터
└── auto_claimer.py         — 만료 winning 포지션 자동 리딤
```

## 주요 함수

```python
class PredictSniper:
    async def fetch_open_crypto_markets(self) -> List[dict]
        # listing + slug-direct GET 합집합
    def _calc_snipe_prob(self, market, current_price, asset) -> float
        # 변동성 기반 P(target hit) Normal CDF
    async def _execute_snipe(self, opp) -> dict
        # 진입 + DB 로깅
    async def claim_resolved(self) -> List[dict]
        # 만료 winning 포지션 자동 리딤
```

## 사용 예시

```python
import asyncio
from predict_sniper import PredictSniper

async def main():
    sniper = PredictSniper(
        api_key=os.getenv("PREDICT_API_KEY"),
        private_key=os.getenv("PREDICT_PRIVATE_KEY"),
        account=os.getenv("PREDICT_ACCOUNT"),
        bet_size=3,
        max_entry_price=0.70,
        min_edge=0.03,
        max_minutes=60,
    )
    await sniper.connect()
    await sniper.run()

asyncio.run(main())
```

## 환경변수

```env
PREDICT_API_KEY=<...>
PREDICT_PRIVATE_KEY=<EVM_PK_FOR_BSC>
PREDICT_ACCOUNT=<PREDICT_ACCOUNT_CONTRACT_ADDR>

PREDICT_SNIPE_BET_SIZE=3
PREDICT_SNIPE_MAX_ENTRY_PRICE=0.70
PREDICT_SNIPE_MIN_EDGE=0.03
PREDICT_SNIPE_MAX_MINUTES=60.0
PREDICT_SNIPE_MIN_STRIKE_DIST=0.001
PREDICT_ASSET_EDGE_BUFFERS=BTC:0.0,ETH:0.03,SOL:0.03,BNB:0.02

# BSC RPC (multiple for rotation)
BSC_RPC=https://bsc-dataseed1.binance.org
BSC_RPC_ALT_1=https://bsc-dataseed2.binance.org
BSC_RPC_ALT_2=https://rpc.ankr.com/bsc
```

## 의존성

- `predict-sdk` 0.0.16+
- `web3.py` (BSC 잔고 조회)
- `eth-account`
- `aiohttp` (JWT auth)
