"""Auto-trigger 안전장치: kill switch, 일일 cap, per-ticker cooldown, 서킷브레이커.

In-memory state. 봇 재시작 시 cap/cooldown/halt 모두 리셋됨 → 보수적 설계 목적.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SafetyState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = field(default_factory=lambda: _today_midnight_epoch())
    last_entry_ts_per_ticker: dict[str, float] = field(default_factory=dict)
    consecutive_losses: int = 0
    halt_until_epoch: float = 0.0
    last_decisions: list[dict] = field(default_factory=list)  # debug: 최근 20개 decision


def _today_midnight_epoch() -> float:
    """오늘 자정(로컬) epoch."""
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


class SafetyGate:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.state = SafetyState()

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self.state.daily_reset_epoch:
            logger.info('[auto_trigger] daily rollover: spent_usd=%.2f reset', self.state.daily_spent_usd)
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    def kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:
            return False

    def halted(self) -> tuple[bool, str]:
        if self.state.halt_until_epoch > time.time():
            remain = int(self.state.halt_until_epoch - time.time())
            return True, f'halted {remain}s (consecutive losses={self.state.consecutive_losses})'
        return False, ''

    def can_enter(self, ticker: str, nominal_usd: float) -> tuple[bool, str]:
        """진입 가능 여부 검사. 모든 게이트 통과해야 True."""
        if not self.cfg.enabled:
            return False, 'disabled'
        if self.kill_switch_active():
            return False, f'kill_switch {self.cfg.kill_switch_file}'
        halted, reason = self.halted()
        if halted:
            return False, reason

        self._maybe_rollover_daily()
        if self.state.daily_spent_usd + nominal_usd > self.cfg.daily_cap_usd:
            return False, f'daily_cap ({self.state.daily_spent_usd:.2f}+{nominal_usd:.2f}>{self.cfg.daily_cap_usd:.2f})'

        last_ts = self.state.last_entry_ts_per_ticker.get(ticker.upper(), 0.0)
        cooldown_sec = self.cfg.per_ticker_cooldown_min * 60
        elapsed = time.time() - last_ts
        if elapsed < cooldown_sec:
            return False, f'cooldown {int(cooldown_sec - elapsed)}s remaining on {ticker}'

        return True, 'ok'

    def record_entry(self, ticker: str, nominal_usd: float) -> None:
        self._maybe_rollover_daily()
        self.state.daily_spent_usd += nominal_usd
        self.state.last_entry_ts_per_ticker[ticker.upper()] = time.time()

    def record_close(self, pnl_usd: Optional[float]) -> None:
        """포지션 청산 결과 반영. pnl_usd >= 0 → 연패 카운터 리셋, < 0 → 증가."""
        if pnl_usd is None:
            return
        if pnl_usd >= 0:
            if self.state.consecutive_losses > 0:
                logger.info('[auto_trigger] win reset: consecutive_losses=%d -> 0', self.state.consecutive_losses)
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1
            logger.warning('[auto_trigger] loss: consecutive=%d pnl=%.2f', self.state.consecutive_losses, pnl_usd)
            if self.state.consecutive_losses >= self.cfg.consecutive_loss_halt:
                self.state.halt_until_epoch = time.time() + self.cfg.halt_duration_min * 60
                logger.error(
                    '[auto_trigger] CIRCUIT BREAKER: halting %dmin after %d consecutive losses',
                    self.cfg.halt_duration_min,
                    self.state.consecutive_losses,
                )

    def status(self) -> dict:
        self._maybe_rollover_daily()
        halted, halt_reason = self.halted()
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'kill_switch_active': self.kill_switch_active(),
            'halted': halted,
            'halt_reason': halt_reason,
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'consecutive_losses': self.state.consecutive_losses,
            'cooldown_tickers': {
                k: max(0, int(self.cfg.per_ticker_cooldown_min * 60 - (time.time() - v)))
                for k, v in self.state.last_entry_ts_per_ticker.items()
                if time.time() - v < self.cfg.per_ticker_cooldown_min * 60
            },
        }
