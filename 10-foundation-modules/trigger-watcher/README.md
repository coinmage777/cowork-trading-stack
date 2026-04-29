# trigger-watcher

> 한 줄 요약 (One-liner): `triggers/*.trigger` 파일을 폴링하여 restart / reload / close / clear_cb / status를 비동기 콜백으로 호출하는 SIGHUP 대체 메커니즘.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "Windows에서 SIGHUP 핸들러가 안 돌아서 봇 hot-reload가 안 됨. `touch triggers/reload.trigger` 한 줄로 reload 콜백 부르는 watcher 만들어줘. 처리 후엔 파일 자동 삭제. async 콜백."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- watchdog 같은 inotify 라이브러리를 끌어다 씀 → Windows에서 이미 잘 안 돌고, deps 늘어남. 단순 폴링이 더 안정.
- 트리거 처리 중 파일 삭제를 `os.remove`로 안 묶어서, 처리 완료 전에 같은 트리거가 또 fired. 처리 시작 직후 바로 삭제 (consume 패턴) 필수.
- restart.trigger 처리 후 loop를 안 빠져나옴 → 바로 다음 반복에서 새 trigger를 또 처리하다가 race. `break`로 명시 종료.
- `status.trigger` 같은 read-only trigger와 `restart.trigger` 같은 destructive trigger를 구분 안 함. 본 코드는 status는 break 안 함, restart/close만 break.

## 코드 (드롭인 단위)
`trigger_watcher.py` — `TriggerWatcher` 클래스. `on_restart`, `on_reload`, `on_close`, `on_clear_cb`는 모두 async callable. `status_dumper`는 sync (dict 반환).

## 사용 예시 (Usage)

```python
import asyncio
from trigger_watcher import TriggerWatcher

async def graceful_restart():
    await save_state()
    await close_connections()
    sys.exit(0)  # systemd가 재시작

tw = TriggerWatcher(
    base_dir="./",                    # ./triggers/ 자동 생성
    on_restart=graceful_restart,
    on_reload=hot_reload_config,
    on_close=close_all_positions_and_exit,
    on_clear_cb=lambda: cb.clear(),   # circuit_breaker 객체
    status_dumper=lambda: {
        "positions": len(traders),
        "balance": equity_tracker.get_latest(),
    },
    poll_interval=2.0,
)
asyncio.create_task(tw.run())

# 외부 셸에서 봇 제어
# $ touch triggers/reload.trigger      # config 핫 리로드
# $ touch triggers/status.trigger      # ./triggers/status.out 에 dump
# $ touch triggers/clear_cb.trigger    # circuit breaker 해제
# $ touch triggers/close.trigger       # 전체 청산 + 종료
```

## 실전 함정 (Battle-tested gotchas)
- 트리거 디렉토리를 git에 commit하면 push할 때마다 거짓 trigger fired. `.gitignore`에 `triggers/` 통째로 추가.
- Windows PowerShell에서 `New-Item triggers\restart.trigger -ItemType File` 또는 `type nul > triggers\restart.trigger`. `touch`는 git bash 등에서만 동작.
- 콜백이 30초 이상 걸리면 다음 trigger 처리가 지연됨. `on_close`처럼 길어질 수 있는 작업은 트리거 처리 직후 `asyncio.create_task(...)`로 백그라운드화하고 watcher는 즉시 break.
- `status.out`이 안 남았다면 `status_dumper` 콜백이 dict 외 (예: list, str)를 반환했거나 raise했을 가능성. 로그를 보고 default=str 직렬화로 처리되는지 확인.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py` 부팅 직후 `health-monitor`와 함께 `asyncio.gather`로 동시 가동.
- `clear_cb.trigger` -> `circuit-breaker.clear()`, `status.trigger` -> `triple-lock-live.status()` + `kill-switch.list_active()` 조합으로 운영자 SSH에서 한 줄로 봇 상태 확인.
- 콘타보 운영 노트: 새 코드 배포 시 `git pull` 후 `touch triggers/reload.trigger`로 재시작 없이 config 갱신.
