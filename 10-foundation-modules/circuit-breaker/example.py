"""circuit-breaker 사용 데모."""
from circuit_breaker import CircuitBreaker

cb = CircuitBreaker(
    daily_stop_loss=-150.0,        # USD
    max_consecutive_failures=5,
    grace_period_seconds=0,         # 데모용으로 즉시 평가
)

# 거래 시뮬레이션 — PnL 누적
for delta in [-30, -40, -50, -45]:
    cb.record_pnl_delta(delta)
    ok, reason = cb.can_proceed()
    print(f"누적 ${cb.status()['pnl_today']}, ok={ok}, reason={reason!r}")

# -165 → 발동 (이미 -50 누적)
# 이후 record_pnl_delta는 무시되며 can_proceed는 계속 False
print(cb.status())

# 수동 해제
cb.clear()
print("after clear:", cb.can_proceed())
