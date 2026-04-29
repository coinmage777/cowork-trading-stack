# wallet-trackers

> 한 줄 요약: 특정 지갑(들)을 실시간 모니터링하면서 transfer / swap 이벤트를 감지하고 알림을 보내는 트래커. Pacifica (HL HIP-3 기반) 와 Solana SPL 두 체인 별 구현.

## 의존성

- `aiohttp`, `web3`, `solana-py`
- `10-foundation-modules/telegram-notifier/` (알림)

## 모듈

| File | 체인 | 용도 |
|------|------|------|
| `pacifica_wallet_tracker.py` | Hyperliquid HIP-3 (Pacifica) | 지갑 주소의 hyna:* 심볼 trade / position change 감지 |
| `solana_wallet_tracker.py` | Solana SPL | SPL token transfer / Jupiter swap 감지 |

## 사용 예시

```python
import asyncio
from pacifica_wallet_tracker import PacificaWalletTracker

async def main():
    tracker = PacificaWalletTracker(
        target_addresses=["0x..."],
        notify_callback=lambda msg: print(msg),
    )
    await tracker.run()

asyncio.run(main())
```

## 실전 함정

- **WS reconnect 빈번**: Pacifica/Solana 모두 connection drop 빈번. 백오프 `[2,5,10,30,60,180,300]` 권장
- **rate limit**: Solana RPC 는 무료 endpoint 의 경우 ~10 req/s — 다중 지갑 모니터링 시 분산 필요
- **dust transfer 노이즈**: $1 이하 transfer 는 필터링 권장 (점심 사기 / spam attack)

## 응용

- Smart money copy-trading: 추적한 지갑 entry/exit 을 시그널로 사용
- 큰 인플로우 detection → market move 사전 경고
