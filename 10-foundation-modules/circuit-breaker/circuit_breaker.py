"""Circuit Breaker — 일일 PnL stop + 연속 실패 cap.

두 가지 발동 조건을 합친 단일 객체:
  1) 일일 누적 손실이 threshold 도달 (USD 기준)
  2) 연속 실패 (주문 실패/거래소 에러 등) N회 도달

발동되면 `can_proceed() -> False`. UTC 자정에 자동 리셋, 또는 수동 `clear()`.

equity_tracker 같은 외부 데이터 소스에 의존하지 않도록, 손익은 호출자가
`record_pnl_delta(usd)` 또는 `set_pnl_today(usd)`로 직접 주입.
연속 실패는 `record_failure()` / `record_success()`.

사용법:
    cb = CircuitBreaker(daily_stop_loss=-150.0, max_consecutive_failures=5)
    cb.record_pnl_delta(-30)      # 거래 1건 -$30
    cb.record_failure()           # 주문 실패 1회
    if not cb.can_proceed()[0]:
        return
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        *,
        daily_stop_loss: float = -150.0,
        max_consecutive_failures: int = 5,
        grace_period_seconds: int = 300,
    ):
        self.daily_stop_loss = daily_stop_loss
        self.max_consecutive_failures = max_consecutive_failures
        self.grace_period_seconds = grace_period_seconds

        self._started_at: float = time.time()
        self._tripped: bool = False
        self._tripped_at: float = 0.0
        self._tripped_reason: str = ""
        self._tripped_day: Optional[datetime] = None

        self._pnl_today: float = 0.0
        self._consecutive_failures: int = 0

    # ---------- public API ----------
    def can_proceed(self) -> tuple[bool, str]:
        """진입/주문 가능 여부. (ok, reason)."""
        # UTC 자정 자동 리셋
        if self._tripped and self._tripped_day is not None:
            now_day = datetime.now(tz=timezone.utc).date()
            if now_day > self._tripped_day:
                logger.info("[circuit_breaker] UTC 자정 자동 리셋")
                self._reset_internal()

        if self._tripped:
            return False, self._tripped_reason
        return True, ""

    def record_pnl_delta(self, delta_usd: float) -> None:
        """이번 거래의 PnL 증분 (음수=손실)."""
        self._pnl_today += float(delta_usd)
        self._evaluate_pnl()

    def set_pnl_today(self, pnl_usd: float) -> None:
        """오늘 누적 PnL을 통째로 주입 (equity_tracker 같은 외부 소스에서)."""
        self._pnl_today = float(pnl_usd)
        self._evaluate_pnl()

    def record_failure(self, reason: str = "") -> None:
        """주문/API 실패 1건. max에 도달하면 발동."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.max_consecutive_failures and not self._tripped:
            self._trip(
                f"consecutive_failures={self._consecutive_failures} "
                f">= {self.max_consecutive_failures}"
                + (f" ({reason})" if reason else "")
            )

    def record_success(self) -> None:
        """성공 1건. 연속 실패 카운터 리셋."""
        self._consecutive_failures = 0

    def clear(self) -> None:
        """수동 해제 (운영자/trigger-watcher에서)."""
        if self._tripped:
            logger.info("[circuit_breaker] 수동 해제")
            self._started_at = time.time()
        self._reset_internal()

    def status(self) -> dict:
        return {
            "tripped": self._tripped,
            "tripped_at": self._tripped_at,
            "tripped_reason": self._tripped_reason,
            "pnl_today": round(self._pnl_today, 2),
            "consecutive_failures": self._consecutive_failures,
            "daily_stop_loss": self.daily_stop_loss,
            "max_consecutive_failures": self.max_consecutive_failures,
        }

    # ---------- internal ----------
    def _evaluate_pnl(self) -> None:
        if self._tripped:
            return
        if time.time() - self._started_at < self.grace_period_seconds:
            return
        if self._pnl_today <= self.daily_stop_loss:
            self._trip(
                f"daily_pnl=${self._pnl_today:.2f} <= ${self.daily_stop_loss}"
            )

    def _trip(self, reason: str) -> None:
        self._tripped = True
        self._tripped_at = time.time()
        self._tripped_reason = reason
        self._tripped_day = datetime.now(tz=timezone.utc).date()
        logger.error(f"[circuit_breaker] TRIPPED: {reason}")

    def _reset_internal(self) -> None:
        self._tripped = False
        self._tripped_at = 0.0
        self._tripped_reason = ""
        self._tripped_day = None
        self._pnl_today = 0.0
        self._consecutive_failures = 0
