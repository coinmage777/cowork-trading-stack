"""Auto-exit 모니터: 열린 hedge 포지션을 감시하여 스프레드 수렴 시 자동 청산 감지.

핵심 설계:
1. 주문 제출은 하지 않음 (안전). 단, 수렴(또는 stale 상태) 감지 후:
   - hedge_service.refresh_latest_job() 호출하여 이미 발생한 체결 fills 스캔
   - status 'closed' 확인되면 최종 PnL 기록 + Telegram 알림
   - 아직 close 체결이 없으면 Telegram으로 "수렴됨 — 수동 청산 권장" 알림
2. 사용자가 frontend 또는 exchange UI로 청산하면 이 서비스가 감지/기록

향후 Phase 2에서 실제 close 주문 자동 제출 로직 추가 가능.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


class AutoExitService:
    def __init__(self, poller, hedge_service, telegram_service=None) -> None:
        self.poller = poller
        self.hedge_service = hedge_service
        self.telegram = telegram_service

        # 역프 진입 후 수렴 기준 (parity=10000). 진입이 gap=9900 기준이면
        # gap이 이 값 이상 회복하면 수익 확정 가능.
        self.convergence_gap = _env_float('AUTO_EXIT_CONVERGENCE_GAP', 10000.0)
        # 진입 후 이 시간 경과하면 status와 무관하게 stale 경고
        self.stale_hours = _env_int('AUTO_EXIT_STALE_HOURS', 24)
        self.poll_interval_sec = _env_int('AUTO_EXIT_POLL_INTERVAL_SEC', 20)
        self.enabled = _env_bool('AUTO_EXIT_ENABLED', True)
        # 실제 close 주문 제출 여부 — 기본 False(감지만). 안전 검증 후 True 전환
        self.auto_submit_close = _env_bool('AUTO_EXIT_SUBMIT_CLOSE', False)

        self._task: Optional[asyncio.Task] = None
        self._running = False

        # 중복 알림 방지: job_id → 마지막 알림 epoch
        self._last_notify_ts: dict[str, float] = {}
        # 알림 쿨다운 (초)
        self.notify_cooldown_sec = 1800  # 30분

        self._total_closes_detected = 0
        self._total_convergence_alerts = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name='auto_exit_loop')
        logger.info(
            '[auto_exit] started | enabled=%s convergence_gap=%.0f stale_hours=%d',
            self.enabled, self.convergence_gap, self.stale_hours,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('[auto_exit] stopped')

    async def _loop(self) -> None:
        while self._running:
            try:
                if self.enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('[auto_exit] tick error: %s', exc, exc_info=True)
            await asyncio.sleep(self.poll_interval_sec)

    async def _tick(self) -> None:
        # 열린 모든 hedge job 수집
        open_jobs = self._list_open_jobs()
        if not open_jobs:
            return

        for job in open_jobs:
            try:
                await self._check_job(job)
            except Exception as exc:
                logger.warning('[auto_exit] check_job failed: %s', exc)

    def _list_open_jobs(self) -> list[dict]:
        """열려 있는 (아직 close 안 된) 모든 hedge job."""
        try:
            jobs = self.hedge_service._jobs.list_jobs(limit=50)
        except Exception:
            return []
        # 상태 필터: hedged / partial_hedged만 대상
        return [
            j for j in jobs
            if (j.get('status') in {'hedged', 'partial_hedged'})
            and j.get('ticker')
        ]

    async def _check_job(self, job: dict) -> None:
        ticker = str(job.get('ticker', '')).upper()
        job_id = str(job.get('job_id', ''))
        futures_exchange = str(job.get('futures_exchange') or 'binance').lower()

        # 현재 gap 읽기
        state = self.poller.state
        gap_result = state.get(ticker)
        if not gap_result:
            return
        ex = gap_result.exchanges.get(futures_exchange)
        if not ex:
            return
        current_gap = ex.futures_gap
        if current_gap is None:
            return

        entry_gap = float(job.get('entry_gap') or 0)
        age_hours = (time.time() - float(job.get('created_at') or time.time())) / 3600

        # 수렴 판정: 진입이 역프(gap<10000)였다면 gap이 convergence_gap 이상으로 복귀 시 수렴
        converged = False
        if entry_gap < 10000 and current_gap >= self.convergence_gap:
            converged = True
        # 김프 진입 케이스 (향후): entry_gap > 10000, 현재 <= convergence
        elif entry_gap > 10000 and current_gap <= (20000 - self.convergence_gap):
            converged = True

        stale = age_hours >= self.stale_hours

        if not (converged or stale):
            return

        # auto_submit_close=True면 수렴 시 실제 close 주문 제출
        if converged and self.auto_submit_close:
            logger.info(
                '[auto_exit] %s converged %.0f→%.0f, submitting close orders',
                ticker, entry_gap, current_gap,
            )
            try:
                close_result = await self.hedge_service.close_job(
                    ticker=ticker, reason='auto_convergence',
                )
            except Exception as exc:
                logger.error('[auto_exit] close_job failed: %s', exc, exc_info=True)
                await self._notify(
                    f'❌ {ticker} auto-close failed: {exc}',
                    job_id=job_id + ':close_fail',
                )
                return

            if close_result.get('ok'):
                closed_job = close_result.get('job') or {}
                status = str(closed_job.get('status') or '')
                pnl_usdt = closed_job.get('final_pnl_usdt') or 0
                pnl_krw = closed_job.get('final_pnl_krw') or 0
                self._total_closes_detected += 1
                await self._notify(
                    f'✅ {ticker} auto-CLOSED\n'
                    f'  entry_gap={entry_gap:.0f} → {current_gap:.0f}\n'
                    f'  status={status} PnL ${pnl_usdt:+.2f} (₩{pnl_krw:+,.0f})',
                    job_id=job_id + ':autoclose',
                    cooldown_override=True,
                )
            return

        # auto_submit_close=False면 기존 detect-only 경로
        try:
            result = await self.hedge_service.refresh_latest_job(
                ticker=ticker,
                exit_spot_exchange=None,
                exit_futures_exchange=None,
            )
        except Exception as exc:
            logger.warning('[auto_exit] refresh failed for %s: %s', ticker, exc)
            return

        refreshed_job = (result or {}).get('job') or {}
        new_status = str(refreshed_job.get('status') or '')

        if new_status == 'closed':
            self._total_closes_detected += 1
            pnl_usdt = refreshed_job.get('final_pnl_usdt') or 0
            pnl_krw = refreshed_job.get('final_pnl_krw') or 0
            await self._notify(
                f'✅ {ticker} CLOSED (detected)\n'
                f'  entry_gap={entry_gap:.0f} → current={current_gap:.0f}\n'
                f'  PnL ${pnl_usdt:+.2f} (₩{pnl_krw:+,.0f})',
                job_id=job_id + ':closed',
                cooldown_override=True,  # 청산 감지는 1회성
            )
            return

        # 수렴됐는데 아직 close 체결 없음 → 사용자에게 수동 청산 알림
        reason_parts = []
        if converged:
            reason_parts.append(f'converged {entry_gap:.0f}→{current_gap:.0f}')
        if stale:
            reason_parts.append(f'stale {age_hours:.1f}h')
        reason = ', '.join(reason_parts)

        self._total_convergence_alerts += 1
        await self._notify(
            f'⚠️ {ticker} hedge open {age_hours:.1f}h\n'
            f'  {reason}\n'
            f'  job_id={job_id} → 수동 청산 검토 권장',
            job_id=job_id + ':conv',
        )

    async def _notify(self, text: str, job_id: str, cooldown_override: bool = False) -> None:
        now = time.time()
        last = self._last_notify_ts.get(job_id, 0.0)
        if not cooldown_override and (now - last) < self.notify_cooldown_sec:
            return
        self._last_notify_ts[job_id] = now
        logger.info('[auto_exit] notify: %s', text.splitlines()[0])
        try:
            if self.telegram is not None:
                await self.telegram._send_message(text)
        except Exception as exc:
            logger.debug('[auto_exit] telegram failed: %s', exc)

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'enabled': self.enabled,
            'auto_submit_close': self.auto_submit_close,
            'convergence_gap': self.convergence_gap,
            'stale_hours': self.stale_hours,
            'poll_interval_sec': self.poll_interval_sec,
            'total_closes_detected': self._total_closes_detected,
            'total_convergence_alerts': self._total_convergence_alerts,
            'open_jobs_count': len(self._list_open_jobs()),
        }
