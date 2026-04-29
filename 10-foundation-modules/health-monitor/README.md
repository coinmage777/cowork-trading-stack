# health-monitor

> 한 줄 요약 (One-liner): equity_tracker.json을 60초마다 폴링하여 PnL/잔고 이상을 감지하고 circuit-breaker / kill-switch / telegram-notifier에 위임하는 orchestrator.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only (orchestration만)
- 선택: `circuit-breaker`, `kill-switch`, `telegram-notifier` 모듈 (없으면 로깅만)

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "60초마다 equity_tracker.json 읽고 — 일일 손실은 circuit_breaker에 주입, 잔고 < $5는 kill_switch 발동, peak 대비 -20% 지속 드롭은 텔레그램 알림. 모든 결정 로직은 외부 모듈에 위임 (의존성 주입)."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 한 클래스에 PnL 계산 + Telegram 보내기 + 거래소 disable 로직을 다 박아둠 → 단위 테스트 불가, 재사용 불가. 본 모듈은 모두 **위임**만 함.
- 잔고 history에 $0 스냅샷을 그대로 쌓음 → API 일시 실패로 0이 한 번 찍히면 peak 대비 -100%로 잘못 발동. `bal <= 0.01` 가드 필수.
- "급락 감지"를 첫 1-2 샘플로 즉시 발동 → 포지션 오픈/클로즈 사이클로 잔고 -50% → +0% 진동 시 false alarm. recent N(=5)개 샘플이 모두 threshold 이하인 sustained drop만 트립.
- HIP-3 / mark-to-market 노이즈 큰 거래소를 일반 거래소와 똑같이 취급 → 정상 변동에 alarm. `excluded_from_drop` 화이트리스트 필요.

## 코드 (드롭인 단위)
`health_monitor.py` — `HealthMonitor` 클래스. equity_tracker.json 포맷:
```json
[{"timestamp": "2026-04-27T10:00:00+00:00", "exchanges": {"lighter": 100.5, "nado": 80.2}}, ...]
```
`circuit_breaker`, `kill_switch`, `notifier` 객체를 주입받음. 모두 None이면 로깅만.

## 사용 예시 (Usage)

```python
from pathlib import Path
import asyncio
from health_monitor import HealthMonitor
from circuit_breaker import CircuitBreaker
from kill_switch import KillSwitch

class NotifierAdapter:
    async def notify(self, msg, **kw):
        from notifier import notify
        return await notify(msg, **kw)

cb = CircuitBreaker(daily_stop_loss=-150.0)
ks = KillSwitch(data_dir="./data")

hm = HealthMonitor(
    equity_tracker_path=Path("./equity_tracker.json"),
    circuit_breaker=cb,
    kill_switch=ks,
    notifier=NotifierAdapter(),
    min_exchange_balance=5.0,
    balance_drop_pct=-20.0,
    excluded_from_drop={"hyena", "hyena_2"},  # HIP-3는 mark 노이즈 큼
)
asyncio.run(hm.run())
```

## 실전 함정 (Battle-tested gotchas)
- equity_tracker.json은 다른 프로세스가 동시에 쓰면서 파일이 잠깐 비거나 잘릴 수 있음 → `json.loads` 실패는 그냥 skip하고 다음 60초 사이클 대기. 절대 raise 하지 말 것.
- `baseline_from_start=True`가 기본 — 봇 재시작 시 PnL이 0부터 다시 계산됨 (의도). 24시간 누적이 필요하면 외부 cron에서 `equity_tracker.json`을 갈아끼워야 함.
- 콘타보 운영 노트: `excluded_from_drop`에 HIP-3 venues(`hyena`, `hyena_2`)를 넣지 않으면 매일 false alarm 1-2건 옴.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py`에서 부팅 시 `circuit-breaker` + `kill-switch` + `telegram-notifier`를 만들고 이 monitor에 주입.
- `trigger-watcher`의 `clear_cb.trigger`는 monitor가 아니라 주입된 `cb.clear()`를 호출 — orchestrator는 stateless에 가깝게 유지.
- 본래 한 파일이었던 `health_monitor.py` (590줄, weekly CB / funding stale / WS fallback 등 다수)를 핵심 3개 책임만 추렸음. 잘라낸 것들은 각각 별도 모듈로 분리 가능.
