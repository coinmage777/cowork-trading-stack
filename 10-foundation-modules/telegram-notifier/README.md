# telegram-notifier

> 한 줄 요약 (One-liner): Telegram + Discord에 비동기로 알림을 보내는 dedup throttling 내장 모듈. 환경변수 없으면 조용히 no-op.

## 의존성 (Dependencies)
- Python 3.10+
- `aiohttp >= 3.9`

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "Telegram 알림 함수 만들어. 환경변수 없으면 봇이 죽으면 안 되고 그냥 no-op. 같은 메시지 1분에 한 번만 보내야 함 (dedup). HTML parse 실패하면 plain text로 재시도해."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 토큰이 없으면 `RuntimeError`를 던져서 봇 전체를 죽임 → 운영 환경에서는 토큰이 없어도 거래는 계속돼야 함. `is_enabled()` 체크 후 `False` 반환이 정답.
- HTML `<` 문자가 본문에 들어가면 Telegram이 400을 뱉는데, 그대로 fail함. parse_mode 빼고 plain으로 재시도해야 진짜로 통과.
- dedup을 dict에만 저장하고 `asyncio.Lock`을 안 잡아서 race로 같은 키가 동시에 두 번 통과함.
- 동기 컨텍스트에서 `asyncio.run(notify(...))`을 부르는데 이미 루프가 돌고 있으면 `RuntimeError: This event loop is already running`. `notify_sync` 같은 헬퍼가 필요.

## 코드 (드롭인 단위)
`notifier.py` 한 파일. 비동기 `notify()`, 동기 `notify_sync()`, 필터 파일 (`alert_filters.json`)로 특정 dedup_key 차단 가능.

## 사용 예시 (Usage)

```python
import asyncio
from notifier import notify, is_enabled

async def main():
    if not is_enabled():
        print("Telegram 미설정 — no-op 동작")
    # 부팅 알림 (silent=True면 푸시 없이 채팅에만)
    await notify("<b>BOT START</b>", dedup_key="bot_start", silent=True)

    # 긴급 — 12시간 dedup
    await notify(
        "<b>[CIRCUIT BREAKER]</b> daily PnL $-200",
        dedup_key="cb_daily",
        dedup_seconds=43200,
    )

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)
- 한국 시간 새벽에 알림 폭탄을 맞아본 뒤로는 모든 정기 알림에 `silent=True`를 넣고, 진짜 긴급(CB / disable / drop)만 push 알림을 살림.
- `<b>...</b>` 태그를 포함한 메시지에 `<` 부등호 (예: "PnL < -50")가 같이 들어가면 400 에러. 본 코드에는 plain text 자동 재시도가 들어있음.
- WS fallback 같은 고빈도 알림은 dedup_key가 동일하면 600초 안에 묶이지만, exchange별로 dedup_key가 갈리면 거래소 N개 × 분당 알림 = 스팸. `dedup_key=f"ws_fallback_{exchange}"`로 쓰되 윈도우는 길게 (10분+).

## 응용 예시 (Real-world usage in this repo)
- 이 모듈은 `health-monitor`가 circuit breaker / auto-disable 발동 시 호출합니다.
- `kill-switch`가 작동하면 부팅 시 즉시 한 번 `notify(..., dedup_key="kill_switch_active")`로 알립니다.
- `trigger-watcher`가 close.trigger를 감지해 전체 청산을 시작할 때도 동일 채널로 보고합니다.
