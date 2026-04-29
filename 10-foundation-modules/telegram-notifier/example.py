"""telegram-notifier 사용 데모. 환경변수 미설정 시 no-op."""
import asyncio
import os
from notifier import notify, is_enabled

# export TELEGRAM_BOT_TOKEN=... ; export TELEGRAM_CHAT_ID=...
print(f"enabled={is_enabled()}")


async def main():
    # 일반 알림 — silent=True 면 푸시는 안 오고 채팅에만 남음
    await notify("<b>BOT START</b> v1.2.3", dedup_key="bot_start", silent=True)

    # 같은 dedup_key는 600초 내 중복 차단
    await notify("<b>BOT START</b> 두 번째", dedup_key="bot_start")  # 무시됨

    # 긴급 알림 — dedup_seconds 짧게
    await notify("[CIRCUIT BREAKER] daily PnL -$200", dedup_key="cb_daily", dedup_seconds=43200)


asyncio.run(main())
