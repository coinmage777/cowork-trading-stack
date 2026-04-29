# subprocess-bridge

> 한 줄 요약 (One-liner): 거래소 SDK들이 deps 충돌할 때, 거래소별 격리 venv에서 자식 프로세스를 띄우고 stdin/stdout JSON-RPC로 통신하는 부모 측 래퍼.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only (`asyncio.subprocess`)

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "lighter SDK는 grpcio 1.x, GRVT는 2.x — 한 venv에 못 같이 못 깐다. 거래소별로 venv 만들고 자식 프로세스로 띄우고 JSON-RPC 통신하는 wrapper 짜줘. 자식이 hang하면 자동 재시작. Windows랑 Linux 둘 다."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 자식 프로세스가 stdout으로 그냥 `print(...)` (예: SDK가 reconnect 시 디버그 출력) 한 라인을 JSON으로 파싱하려다 실패해서 락업. 첫 글자가 `{` 또는 `[`인지 가드 필수.
- Windows에서 부모가 Ctrl+C 받았을 때 자식이 같은 process group이라 동시에 죽으면서 graceful close가 안 됨 → `CREATE_NEW_PROCESS_GROUP` 플래그로 격리.
- 좀비 자식 프로세스 정리를 잊음. 부모 재시작 시 같은 venv에서 또 자식이 떠서 stdin/stdout 충돌 → start() 진입 시 같은 패턴의 좀비를 pkill/PowerShell로 청소.
- 타임아웃 1회마다 재시작하면 변동성 큰 시장에서 무한 재시작 루프. 3회 연속일 때만 트리거.

## 코드 (드롭인 단위)
`subprocess_wrapper.py` — `SubprocessExchangeWrapper` 클래스. 자식 측 `exchange_bridge.py`는 **포함하지 않음** (거래소마다 다르므로 사용자가 작성). 자식이 따라야 할 JSON-RPC 스펙: `{"id": int, "method": str, "params": dict}` 입력 → `{"id": int, "result": ...}` 또는 `{"id": int, "error": str}` 출력.

## 사용 예시 (Usage)

```python
import asyncio
from subprocess_wrapper import SubprocessExchangeWrapper

async def main():
    w = SubprocessExchangeWrapper(
        venv_python="./lighter_venv/bin/python",
        exchange="lighter",
        config_path="config.yaml",
        account="lighter_main",
    )
    await w.start()
    try:
        price = await w.get_mark_price("BTC")
        await w.create_order(symbol="BTC", side="buy", amount=0.001, order_type="market")
    finally:
        await w.close()

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)
- 자식 측 SDK가 자체적으로 `signal.signal(SIGINT, ...)` 핸들러를 등록하면 부모가 보낸 close 명령보다 먼저 자기가 죽음. 자식 측에서 SIGINT 핸들러를 명시적으로 비활성화하거나 process group을 분리.
- stdin 버퍼링 — 자식이 `flush=True` 안 쓰면 응답이 부모에 도달 안 함. `print(json.dumps(...), flush=True)` 강제.
- `default=str`로 Decimal/datetime 직렬화 — `default=str` 안 넣으면 거래소 SDK가 반환하는 `Decimal('123.45')`에서 죽음.
- 좀비 정리는 부팅 시 1회만. 운영 중 재시작은 `_auto_restart`에서 자기 자식만 kill.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py`에서 lighter, GRVT, Reya, Bulk 거래소를 모두 이 wrapper로 띄웁니다.
- `health-monitor`는 wrapper의 `is_alive()` 체크 결과로 거래소 health를 판단합니다.
- 콘타보 운영 노트: SDK 업그레이드 시 해당 venv만 `pip install -U` 후 trigger-watcher로 reload — 봇 전체 재배포 안 해도 됨.
