# circuit-breaker

> 한 줄 요약 (One-liner): 일일 PnL stop + 연속 실패 cap을 합친 단일 객체. 발동 시 `can_proceed() = False`. UTC 자정 자동 리셋.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "거래봇 circuit breaker. 일일 손실이 -$150 도달하면 신규 진입 차단. 또는 주문 5번 연속 실패하면 차단. UTC 자정에 자동 리셋. equity_tracker 같은 외부 의존 없이 호출자가 PnL을 주입할 수 있게."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 부팅 직후 변동성으로 PnL이 출렁이며 grace period 없이 즉시 트립. `grace_period_seconds`로 첫 N초간 평가 skip해야 함.
- "UTC 자정에 자동 리셋"을 cron이나 scheduler로 구현 — 의존성 폭발. trip된 시점의 `date`만 기억하고 호출 시 비교하면 끝.
- `record_failure()` 후 한 번 성공해도 카운터 안 리셋해서 누적되어 결국 트립. `record_success()`로 카운터 0으로.
- pnl threshold를 `>=`로 비교 — `daily_stop_loss=-150`, `pnl=-150`일 때 발동/미발동 모호. `<=` 명확.

## 코드 (드롭인 단위)
`circuit_breaker.py` — `CircuitBreaker` 클래스. PnL은 `record_pnl_delta(delta)` 또는 `set_pnl_today(total)`로 주입. 실패는 `record_failure()` / 성공은 `record_success()`. 상태 확인은 `can_proceed()`.

## 사용 예시 (Usage)

```python
from circuit_breaker import CircuitBreaker

cb = CircuitBreaker(daily_stop_loss=-150.0, max_consecutive_failures=5)

# 거래 루프 안에서
async def execute_trade(...):
    ok, reason = cb.can_proceed()
    if not ok:
        logger.warning(f"[CB] 차단: {reason}")
        return
    try:
        result = await place_order(...)
        cb.record_success()
        cb.record_pnl_delta(result.realized_pnl)
    except OrderError as e:
        cb.record_failure(str(e))

# 또는 equity_tracker 같은 외부 소스에서 통째로 주입
cb.set_pnl_today(equity_tracker.get_pnl_today_usd())

# 운영자 trigger로 수동 해제
cb.clear()
```

## 실전 함정 (Battle-tested gotchas)
- 봇을 UTC 23:50에 시작하면 grace_period(5분) 끝나고 곧바로 자정이 와서 의미 없는 리셋. 의도된 동작이지만, `_started_at`도 같이 리셋되므로 다음 grace는 0시 5분까지.
- `record_pnl_delta`로만 누적하면 부팅 시 0부터 시작 — 어제 종료 시점 대비 손실은 카운팅 안 됨. equity_tracker 같은 절대값 주입(`set_pnl_today`)이 더 안전.
- 연속 실패 cap이 너무 낮으면 (3 등) 일시적 네트워크 글리치로 트립. 실전에서 5-7이 적당. 진짜 문제는 다른 시그널 (latency 급증 등)도 같이 봐야 함.

## 응용 예시 (Real-world usage in this repo)
- `health-monitor`가 `equity_tracker` 데이터를 `set_pnl_today`로 주입하고, 거래 루프는 매번 `can_proceed()` 체크.
- 주문 실패는 `multi-perp-dex/strategies/main.py`의 retry 핸들러에서 `record_failure()` 호출.
- `trigger-watcher`의 `clear_cb.trigger`가 떨어지면 `cb.clear()` 호출 → 운영자가 수동으로 풀 수 있음.
