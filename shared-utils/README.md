# shared-utils

봇 운영 공통 유틸리티 — Telegram 알림, subprocess wrapper (격리 venv), HealthMonitor, TriggerWatcher (file-based control), state/equity tracker.

## 모듈 한눈

| 파일 | 용도 |
|------|------|
| `notifier.py` | Telegram bot으로 알림. dedup throttling 내장. `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 미설정 시 no-op |
| `subprocess_wrapper.py` | 격리 venv 거래소 (Lighter/GRVT/Reya/Bulk) subprocess 관리. JSON-RPC stdin/stdout. 좀비 자동 kill + 재시작 |
| `exchange_bridge.py` | subprocess_wrapper의 자식 측 entry. `python -m strategies.exchange_bridge --exchange lighter` |
| `health_monitor.py` | 60초 주기 헬스체크. Circuit breaker / 거래소 auto-disable / 잔고 급락 알림 / WS fallback 빈도 |
| `trigger_watcher.py` | `triggers/` 디렉토리 파일 감시. `restart.trigger` / `reload.trigger` / `close.trigger` / `clear_cb.trigger` / `status.trigger` |
| `state_manager.py` | 봇 재시작 시 포지션 복원 위해 `trader_state.json` 저장/로드 |
| `equity_tracker.py` | 10분 주기 거래소별 잔고 스냅샷 → `equity_tracker.json` |
| `health_monitor.py` (sync_zero_balance_at_startup) | 봇 시작 시 잔고 0인 거래소 auto-disable 자동 등록 |

## 핵심 함수

### notifier.py
```python
# 환경변수 자동 로드. 미설정 시 no-op
await notify(message: str, dedup_key: str = None, dedup_seconds: int = 600, silent: bool = False)
is_enabled() -> bool
```

### subprocess_wrapper.py
```python
class SubprocessExchangeWrapper:
    def __init__(self, name: str, account: str, venv_python: str, ...)
    async def start(self) -> None       # subprocess 시작 + ready 대기
    async def call(self, method: str, **kwargs) -> Any  # JSON-RPC 호출
    async def stop(self) -> None        # graceful close
    async def _auto_restart(self) -> None  # 3회 연속 timeout 시 자동 재시작
```

### trigger_watcher.py
```python
class TriggerWatcher:
    async def run(self) -> None
        # restart.trigger → on_restart() callback (graceful_shutdown + os._exit(0))
        # reload.trigger → on_reload() (config 핫 리로드)
        # close.trigger → on_close() (전체 청산 + 종료)
        # clear_cb.trigger → on_clear_cb() (circuit breaker 해제)
        # status.trigger → status.out 파일 dump
```

### health_monitor.py
```python
class HealthMonitor:
    async def run(self) -> None
        # 1) Daily PnL circuit breaker (default $-150)
        # 2) 거래소 auto-disable (잔고 < $5 또는 일일 -30%)
        # 3) 잔고 급락 알림 (30분 -20%)
        # 4) HL WS fallback 빈도 (분당 10회 초과 시 알림)
        # 5) Funding feed staleness (30분 미수집 시 알림)

    async def sync_zero_balance_at_startup(self, wait_seconds=60) -> None
        # 봇 시작 후 60초 대기 후 잔고 0 거래소 auto_disabled 자동 등록
```

## 사용 예시

```python
import asyncio
from notifier import notify
from health_monitor import HealthMonitor
from trigger_watcher import TriggerWatcher

async def main():
    # 알림
    await notify("<b>BOT START</b>", dedup_key="bot_start", silent=True)

    # 헬스 monitor + trigger watcher 동시 가동
    hm = HealthMonitor(equity_tracker=..., daily_stop_loss=-150)
    tw = TriggerWatcher(
        base_dir=".",
        on_restart=graceful_shutdown_callback,
        on_reload=hot_reload_callback,
    )

    await asyncio.gather(hm.run(), tw.run())

asyncio.run(main())
```

## 환경변수

```env
TELEGRAM_BOT_TOKEN=<...>
TELEGRAM_CHAT_ID=<...>

# HealthMonitor 튜닝
HEALTH_DAILY_STOP_LOSS=-150
HEALTH_MIN_EXCHANGE_BALANCE=5
HEALTH_BALANCE_DROP_PCT=-0.20
HEALTH_BALANCE_DROP_WINDOW_MIN=30
HEALTH_FUNDING_STALE_SECONDS=1800
```

## Trigger 사용

```bash
# 외부에서 봇 제어
touch triggers/restart.trigger        # graceful restart (systemd auto-restart)
touch triggers/reload.trigger          # config hot reload (재시작 없이)
touch triggers/close.trigger           # 전체 청산 + 종료
touch triggers/clear_cb.trigger        # circuit breaker 해제
touch triggers/status.trigger          # status.out에 현 상태 dump

# 또는 telegram_commander로 같은 효과
```

## 의존성

- `aiohttp` (Telegram, RPC)
- `psutil` (process lock)
- 표준 라이브러리 (json, asyncio, signal)
