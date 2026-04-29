"""health-monitor 사용 데모. circuit-breaker / kill-switch / telegram-notifier 연결."""
import asyncio
from pathlib import Path

# 실제 import 경로는 본인 프로젝트 기준 — 여기선 sys.path 설정 가정
import sys
sys.path.insert(0, "../circuit-breaker")
sys.path.insert(0, "../kill-switch")
sys.path.insert(0, "../telegram-notifier")

from circuit_breaker import CircuitBreaker
from kill_switch import KillSwitch
import notifier  # has notify(...)
from health_monitor import HealthMonitor


class NotifierAdapter:
    """notifier.notify는 module-level 함수라서 객체로 감싸 줌."""
    async def notify(self, msg, **kwargs):
        return await notifier.notify(msg, **kwargs)


async def main():
    cb = CircuitBreaker(daily_stop_loss=-150.0)
    ks = KillSwitch(data_dir="./data")
    hm = HealthMonitor(
        equity_tracker_path=Path("./equity_tracker.json"),
        circuit_breaker=cb,
        kill_switch=ks,
        notifier=NotifierAdapter(),
        min_exchange_balance=5.0,
        balance_drop_pct=-20.0,
        check_interval_seconds=60,
    )
    await asyncio.gather(hm.run())


asyncio.run(main())
