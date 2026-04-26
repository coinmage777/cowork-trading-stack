"""현-선 역프 자동 진입 오케스트레이터.

Poller.state (실시간 갭) → Safety gate → hedge_trade_service.enter() 호출.
ddari 시그널은 Phase 2에서 추가 (현재 stub).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.exchanges.types import GapResult
from backend.services.auto_trigger_config import AutoTriggerConfig
from backend.services.auto_trigger_safety import SafetyGate

logger = logging.getLogger(__name__)


class AutoTriggerService:
    def __init__(self, poller, hedge_service, telegram_service=None, wide_scanner=None) -> None:
        self.cfg = AutoTriggerConfig.load()
        self.poller = poller
        self.hedge_service = hedge_service
        self.telegram = telegram_service
        self.wide_scanner = wide_scanner  # theddari_scanner (optional, None이면 비활성)
        self.safety = SafetyGate(self.cfg)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_seen_gap_per_ticker: dict[str, float] = {}
        self._total_triggers = 0
        self._total_executes = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name='auto_trigger_loop')
        logger.info(
            '[auto_trigger] started | enabled=%s dry_run=%s watchlist=%s fx=%s gap<%.0f nominal=$%.0f',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.watchlist,
            self.cfg.futures_exchange, self.cfg.gap_enter_threshold, self.cfg.nominal_usd,
        )
        await self._notify(
            f'🎯 Auto-trigger started\n'
            f'enabled={self.cfg.enabled} dry_run={self.cfg.dry_run}\n'
            f'watchlist={",".join(self.cfg.watchlist)}\n'
            f'gap<{self.cfg.gap_enter_threshold:.0f} → enter ${self.cfg.nominal_usd:.0f} x{self.cfg.leverage}',
            alert_key='started_auto_trigger',
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('[auto_trigger] stopped')

    async def _loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('[auto_trigger] tick error: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _tick(self) -> None:
        state = self.poller.state
        # 1) 기본 watchlist 처리
        await self._tick_watchlist(state)
        # 2) 광역 스캐너 — poller.state 전체를 스캔 (config 플래그로 on/off)
        import os
        wide_on = os.getenv('AUTO_TRIGGER_WIDE_SCAN', 'true').strip().lower() in ('1', 'true', 'yes', 'y', 'on')
        if wide_on:
            await self._tick_wide_scanner(state)

    async def _tick_watchlist(self, state) -> None:
        for ticker in self.cfg.watchlist:
            gap_result: Optional[GapResult] = state.get(ticker)
            if not gap_result:
                continue

            # 대상 거래소 데이터
            ex_data = gap_result.exchanges.get(self.cfg.futures_exchange)
            if not ex_data:
                continue
            gap = ex_data.futures_gap
            if gap is None:
                continue

            self._last_seen_gap_per_ticker[ticker] = gap

            # 역프 트리거: gap < threshold
            if gap >= self.cfg.gap_enter_threshold:
                continue

            self._total_triggers += 1

            # 안전 게이트
            ok, reason = self.safety.can_enter(ticker, self.cfg.nominal_usd)
            if not ok:
                logger.info('[auto_trigger] %s gap=%.1f BLOCKED by safety: %s', ticker, gap, reason)
                continue

            # 기존 활성 hedge 체크
            latest = self.hedge_service._jobs.latest_open_job(ticker=ticker)
            if latest:
                logger.debug('[auto_trigger] %s already has active hedge, skip', ticker)
                continue

            # 실행
            logger.info(
                '[auto_trigger] %s gap=%.1f < %.1f → trigger dry_run=%s',
                ticker, gap, self.cfg.gap_enter_threshold, self.cfg.dry_run,
            )
            await self._execute(ticker=ticker, gap=gap, source='watchlist')

    async def _tick_wide_scanner(self, state) -> None:
        """Poller 기반 광역 스캐너 — watchlist 밖 모든 Bithumb 티커에서 극단 gap 발굴.

        Poller는 이미 Bithumb 전체(~452) × 해외 거래소 gap을 3초마다 계산해 state에 저장.
        watchlist에 없는 티커 중 gap < enter_threshold 인 것들을 발견 즉시 후보로 처리.
        동일 safety gate + dry_run 적용.
        """
        watchset = {t.upper() for t in self.cfg.watchlist}
        futures_ex = self.cfg.futures_exchange
        # 극단 후보 수집 — 노이즈 필터 3종
        #  (1) 역프 구간: gap_enter_threshold 이하
        #  (2) 하한선: 5000 (50%+ 역프는 데이터 오류 가능성 높음)
        #  (3) 양측 가격 유효성: bithumb.ask > 0 && futures.bid > 0
        candidates: list[tuple[str, float]] = []
        for ticker, gap_result in state.items():
            tk = str(ticker).upper()
            if tk in watchset:
                continue
            if not gap_result or not gap_result.bithumb:
                continue
            kr_ask = gap_result.bithumb.ask
            if kr_ask is None or kr_ask <= 0:
                continue
            ex_data = gap_result.exchanges.get(futures_ex)
            if not ex_data:
                continue
            futures_bbo = getattr(ex_data, 'futures_bbo', None)
            if futures_bbo is None or not getattr(futures_bbo, 'bid', None) or futures_bbo.bid <= 0:
                continue
            g = ex_data.futures_gap
            if g is None:
                continue
            # (1) 역프 영역 and (2) 상식적 범위 (50% 이상 역프는 가격 오류)
            if g >= self.cfg.gap_enter_threshold or g < 5000:
                continue
            candidates.append((tk, g))

        if not candidates:
            return

        # gap 낮은 순 (역프가 큰 순) 상위 5개만 처리 (과도한 진입 방지)
        candidates.sort(key=lambda x: x[1])
        for ticker, local_gap in candidates[:5]:
            self._total_triggers += 1
            ok, reason = self.safety.can_enter(ticker, self.cfg.nominal_usd)
            if not ok:
                logger.debug('[wide_scanner] %s gap=%.1f BLOCKED: %s', ticker, local_gap, reason)
                continue

            latest = self.hedge_service._jobs.latest_open_job(ticker=ticker)
            if latest:
                continue

            logger.info(
                '[wide_scanner] %s gap=%.1f < %.1f → trigger dry_run=%s',
                ticker, local_gap, self.cfg.gap_enter_threshold, self.cfg.dry_run,
            )
            await self._execute(ticker=ticker, gap=local_gap, source='wide_scanner')

    async def _execute(self, ticker: str, gap: float, source: str = 'watchlist') -> None:
        """dry_run=True → 알림만. False → 실주문 + 기록."""
        decision = {
            'ts': int(time.time()),
            'ticker': ticker,
            'gap': round(gap, 2),
            'source': source,
            'futures_exchange': self.cfg.futures_exchange,
            'nominal_usd': self.cfg.nominal_usd,
            'leverage': self.cfg.leverage,
            'dry_run': self.cfg.dry_run,
        }

        if self.cfg.dry_run:
            decision['result'] = 'dry_run_skip'
            self.safety.state.last_decisions.append(decision)
            self.safety.state.last_decisions[:] = self.safety.state.last_decisions[-20:]
            await self._notify(
                f'🧪 [DRY-RUN] {ticker} gap={gap:.0f} → would enter\n'
                f'  ${self.cfg.nominal_usd:.0f} x{self.cfg.leverage} via {self.cfg.futures_exchange}',
                alert_key='dry_auto_trigger',
            )
            return

        # LIVE
        try:
            result = await self.hedge_service.enter(
                ticker=ticker,
                futures_exchange=self.cfg.futures_exchange,
                nominal_usd=self.cfg.nominal_usd,
                leverage=self.cfg.leverage,
            )
        except Exception as exc:
            logger.error('[auto_trigger] hedge.enter raised: %s', exc, exc_info=True)
            decision['result'] = f'exception: {exc}'
            self.safety.state.last_decisions.append(decision)
            self.safety.state.last_decisions[:] = self.safety.state.last_decisions[-20:]
            await self._notify(f'❌ {ticker} gap={gap:.0f} enter exception: {exc}')
            return

        ok = bool(result.get('ok'))
        decision['result'] = result
        self.safety.state.last_decisions.append(decision)
        self.safety.state.last_decisions[:] = self.safety.state.last_decisions[-20:]

        if ok:
            self.safety.record_entry(ticker, self.cfg.nominal_usd)
            self._total_executes += 1
            job = result.get('job') or {}
            job_id = job.get('job_id', '?')
            await self._notify(
                f'✅ {ticker} gap={gap:.0f} ENTERED\n'
                f'  job_id={job_id} nominal=${self.cfg.nominal_usd:.0f} x{self.cfg.leverage}'
            )
        else:
            code = result.get('code', 'UNKNOWN')
            msg = result.get('message', '')
            logger.warning('[auto_trigger] %s enter failed: %s %s', ticker, code, msg)
            await self._notify(f'⚠️ {ticker} gap={gap:.0f} enter failed: {code} {msg[:120]}')

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        try:
            if self.telegram is not None:
                await self.telegram._send_message(text, alert_key=alert_key)
        except Exception as exc:
            logger.debug('[auto_trigger] telegram send failed: %s', exc)

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'config': {
                'enabled': self.cfg.enabled,
                'dry_run': self.cfg.dry_run,
                'watchlist': self.cfg.watchlist,
                'futures_exchange': self.cfg.futures_exchange,
                'gap_enter_threshold': self.cfg.gap_enter_threshold,
                'nominal_usd': self.cfg.nominal_usd,
                'leverage': self.cfg.leverage,
                'daily_cap_usd': self.cfg.daily_cap_usd,
                'per_ticker_cooldown_min': self.cfg.per_ticker_cooldown_min,
                'poll_interval_sec': self.cfg.poll_interval_sec,
            },
            'safety': self.safety.status(),
            'stats': {
                'total_triggers': self._total_triggers,
                'total_executes': self._total_executes,
                'last_seen_gap': self._last_seen_gap_per_ticker,
            },
            'recent_decisions': self.safety.state.last_decisions[-10:],
            'wide_scanner': (
                self.wide_scanner.status() if self.wide_scanner is not None else None
            ),
        }

    def set_dry_run(self, dry_run: bool) -> None:
        self.cfg.dry_run = bool(dry_run)
        logger.info('[auto_trigger] dry_run=%s', self.cfg.dry_run)

    def set_enabled(self, enabled: bool) -> None:
        self.cfg.enabled = bool(enabled)
        logger.info('[auto_trigger] enabled=%s', self.cfg.enabled)
