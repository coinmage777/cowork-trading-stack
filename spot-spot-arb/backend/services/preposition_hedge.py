"""현-선 암살 (Pre-position hedge) 서비스.

Pika_Kim msg 316 (KAT case: "현선 1.5%에 잡고 15%에 풀기") 기반.

핵심 아이디어:
- 빗썸 입금 재개 / 토큰 언락 / 상장 발표가 임박하면, 발표 전에는 현-선 gap이 0 근처로 수렴한 상태
- 이때 미리 spot BUY + perp SHORT 헷지 포지션을 걸어둠 (neutral entry)
- 발표가 나오면 gap이 5~20%로 벌어짐 → 양 레그 모두 청산하여 확정 수익

기존 `auto_trigger`와의 차이:
- `auto_trigger`: gap이 **이미** 역프 구간(< 9900)일 때 진입 (reactive)
- `preposition_hedge`: gap이 **neutral** (~10000) 구간일 때 진입 (proactive, 미래 베팅)

실행 흐름:
1. 수동 API (`POST /api/auto/preposition-enter`) 또는 listing_detector 콜백으로 진입
2. 진입 전 gap이 neutral zone (기본 9950~10050) 인지 검증
3. `hedge_service.enter()` 호출 → Bithumb spot BUY + 해외 perp SHORT
4. 백그라운드 루프 (30s) 가 현재 gap을 모니터링
5. 다음 중 하나 발생 시 청산:
   - target exit gap 도달 (예: entry 10000 → target 9500, 5% 역프 벌어짐 = 청산)
   - max_hold_hours 초과 → 강제 timeout close
   - gap이 반대로 3% 이상 벌어짐 → stop-loss close

삼중 락 (triple-lock):
- `PREPOSITION_ENABLED` env
- `PREPOSITION_DRY_RUN` env (기본 True)
- `PREPOSITION_LIVE_CONFIRM` env (기본 False — True 필수)
- 추가: kill switch file `data/KILL_PREPOSITION`, max_open, daily_cap
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend import config as backend_config

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 환경변수 헬퍼
# ----------------------------------------------------------------------


def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except (ValueError, TypeError):
        return default


# ----------------------------------------------------------------------
# 저장 경로
# ----------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
_DATA_DIR.mkdir(parents=True, exist_ok=True)

PREPOSITION_JOBS_FILE = _DATA_DIR / 'preposition_jobs.jsonl'
PREPOSITION_WATCHLIST_FILE = _DATA_DIR / 'preposition_watchlist.json'


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class PrePositionConfig:
    enabled: bool = True
    dry_run: bool = True              # 기본 True — 절대 실자금 나가지 않게
    live_confirm: bool = False        # 두 번째 키 (False → dry_run 강제)
    auto_on_listing: bool = False     # listing_detector 이벤트 자동 진입 여부

    # neutral gap zone (진입 허용 구간)
    neutral_gap_min: float = 9950.0   # -0.5%
    neutral_gap_max: float = 10050.0  # +0.5%

    notional_usd: float = 50.0
    max_open: int = 3
    daily_cap_usd: float = 200.0

    # 청산 트리거
    stop_loss_gap_drift_pct: float = 3.0   # 반대 방향 3% 이상 드리프트 시 손절
    poll_interval_sec: int = 30

    kill_switch_file: str = 'data/KILL_PREPOSITION'

    @classmethod
    def load(cls) -> 'PrePositionConfig':
        return cls(
            enabled=_env_bool('PREPOSITION_ENABLED', True),
            dry_run=_env_bool('PREPOSITION_DRY_RUN', True),
            live_confirm=_env_bool('PREPOSITION_LIVE_CONFIRM', False),
            auto_on_listing=_env_bool('PREPOSITION_AUTO_ON_LISTING', False),
            neutral_gap_min=_env_float('PREPOSITION_NEUTRAL_GAP_MIN', 9950.0),
            neutral_gap_max=_env_float('PREPOSITION_NEUTRAL_GAP_MAX', 10050.0),
            notional_usd=_env_float('PREPOSITION_NOTIONAL_USD', 50.0),
            max_open=_env_int('PREPOSITION_MAX_OPEN', 3),
            daily_cap_usd=_env_float('PREPOSITION_DAILY_CAP_USD', 200.0),
            stop_loss_gap_drift_pct=_env_float(
                'PREPOSITION_STOP_LOSS_GAP_DRIFT_PCT', 3.0,
            ),
            poll_interval_sec=_env_int('PREPOSITION_POLL_INTERVAL_SEC', 30),
            kill_switch_file=_env(
                'PREPOSITION_KILL_SWITCH_FILE', 'data/KILL_PREPOSITION',
            ),
        )

    @property
    def live_armed(self) -> bool:
        """dry_run=False + live_confirm=True 둘 다 만족해야 실주문."""
        return (not self.dry_run) and self.live_confirm


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------


@dataclass
class PrePositionJob:
    """pre-position 기록. hedge_service.enter 가 생성한 hedge_job 과 1:1 연결."""

    job_id: str                        # preposition job 자체 id (uuid)
    hedge_job_id: Optional[str]        # hedge_trade_service.enter 가 반환한 job_id
    ticker: str
    target_exchange: str
    notional_usd: float
    entry_gap: float                   # 진입 시점 gap (예: 10010)
    target_exit_gap: float             # 이 gap 이하 도달 시 청산 (역프 벌어짐 방향)
    stop_loss_gap: float               # 반대 드리프트 시 손절 기준
    max_hold_ts: int                   # epoch sec, 이 시간 초과 시 강제 청산
    trigger_reason: str                # 'manual' | 'listing_detector' | 'unlock' 등
    created_at: int
    status: str = 'open'               # open | closing | closed_win | closed_stop | closed_timeout | closed_err
    closed_at: Optional[int] = None
    close_reason: Optional[str] = None
    exit_gap: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'hedge_job_id': self.hedge_job_id,
            'ticker': self.ticker,
            'target_exchange': self.target_exchange,
            'notional_usd': self.notional_usd,
            'entry_gap': self.entry_gap,
            'target_exit_gap': self.target_exit_gap,
            'stop_loss_gap': self.stop_loss_gap,
            'max_hold_ts': self.max_hold_ts,
            'trigger_reason': self.trigger_reason,
            'created_at': self.created_at,
            'status': self.status,
            'closed_at': self.closed_at,
            'close_reason': self.close_reason,
            'exit_gap': self.exit_gap,
        }


# ----------------------------------------------------------------------
# Watchlist
# ----------------------------------------------------------------------


@dataclass
class WatchlistEntry:
    ticker: str
    expected_gap_widening_pct: float    # 예: 15.0 = 15% 역프 벌어질 것으로 기대
    max_hold_hours: float
    note: str = ''

    def to_json(self) -> dict[str, Any]:
        return {
            'ticker': self.ticker,
            'expected_gap_widening_pct': self.expected_gap_widening_pct,
            'max_hold_hours': self.max_hold_hours,
            'note': self.note,
        }


def _load_watchlist() -> list[WatchlistEntry]:
    """data/preposition_watchlist.json 로드. 없으면 빈 리스트."""
    if not PREPOSITION_WATCHLIST_FILE.exists():
        return []
    try:
        with PREPOSITION_WATCHLIST_FILE.open('r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning('[preposition] watchlist load failed: %s', exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[WatchlistEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get('ticker') or '').strip().upper()
        if not ticker:
            continue
        try:
            widening = float(item.get('expected_gap_widening_pct') or 0.0)
            hold = float(item.get('max_hold_hours') or 0.0)
        except (TypeError, ValueError):
            continue
        if widening <= 0 or hold <= 0:
            continue
        out.append(
            WatchlistEntry(
                ticker=ticker,
                expected_gap_widening_pct=widening,
                max_hold_hours=hold,
                note=str(item.get('note') or ''),
            )
        )
    return out


def _save_watchlist(entries: list[WatchlistEntry]) -> None:
    payload = [e.to_json() for e in entries]
    tmp = PREPOSITION_WATCHLIST_FILE.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(PREPOSITION_WATCHLIST_FILE)


# ----------------------------------------------------------------------
# 메인 서비스
# ----------------------------------------------------------------------


class PrePositionHedge:
    """현-선 암살 사전 헷지 오케스트레이터.

    Flow:
        await service.start()
        # 수동 진입
        res = await service.enter_preposition('KAT', 'binance', 50, 15.0, 48)
        # or listing_detector hook (auto_on_listing=True 일 때만)
        # 모니터 루프가 백그라운드에서 exit 조건 감시
        await service.stop()
    """

    def __init__(
        self,
        poller,
        hedge_service,
        listing_detector=None,
        telegram_service=None,
    ) -> None:
        self.cfg = PrePositionConfig.load()
        self.poller = poller
        self.hedge_service = hedge_service
        self.listing_detector = listing_detector
        self.telegram = telegram_service

        self._jobs: dict[str, PrePositionJob] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # 일일 cap 추적 (in-memory, 봇 재시작 시 리셋 — 보수적 설계)
        self._daily_spent_usd: float = 0.0
        self._daily_reset_epoch: float = _today_midnight_epoch()

        # watchlist (파일 기반, 재시작 후에도 유지)
        self._watchlist: list[WatchlistEntry] = _load_watchlist()

        # listing_detector 콜백 등록 (등록만 하고 auto_on_listing 플래그로 게이트)
        self._listener_registered = False

        # 재시작 시 open job 복구
        self._reload_open_jobs()

        self._total_enters = 0
        self._total_wins = 0
        self._total_stops = 0
        self._total_timeouts = 0

    # ------------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self.listing_detector is not None and not self._listener_registered:
            try:
                self.listing_detector.add_listener(self._on_listing_event)
                self._listener_registered = True
                logger.info('[preposition] registered listing_detector listener')
            except Exception as exc:
                logger.warning('[preposition] add_listener failed: %s', exc)

        self._task = asyncio.create_task(self._monitor_loop(), name='preposition_monitor')
        logger.info(
            '[preposition] started | enabled=%s dry_run=%s live_confirm=%s '
            'auto_on_listing=%s open=%d watchlist=%d neutral=[%.0f..%.0f] notional=$%.0f',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.auto_on_listing, len(self._open_jobs()), len(self._watchlist),
            self.cfg.neutral_gap_min, self.cfg.neutral_gap_max, self.cfg.notional_usd,
        )
        await self._notify(
            f'🎯 Pre-position hedge started\n'
            f'enabled={self.cfg.enabled} dry_run={self.cfg.dry_run} live_armed={self.cfg.live_armed}\n'
            f'neutral_zone=[{self.cfg.neutral_gap_min:.0f}..{self.cfg.neutral_gap_max:.0f}] '
            f'notional=${self.cfg.notional_usd:.0f}',
            alert_key='preposition_started',
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('[preposition] stopped')

    # ------------------------------------------------------------------
    # 퍼블릭 API: 진입/청산
    # ------------------------------------------------------------------

    async def enter_preposition(
        self,
        ticker: str,
        target_exchange: str = 'binance',
        notional_usd: Optional[float] = None,
        expected_gap_widening_pct: float = 5.0,
        max_hold_hours: float = 48.0,
        trigger_reason: str = 'manual',
    ) -> dict[str, Any]:
        """현-선 gap이 neutral zone일 때 spot+perp 헷지 진입.

        Entry validation:
        - gap < neutral_min: 이미 역프 → auto_trigger 영역이므로 defer
        - gap > neutral_max: 김프 → preposition 전략과 방향 다름, 거부
        - neutral_min <= gap <= neutral_max: 진입 허용

        주의: 실주문은 `dry_run=False AND live_confirm=True` 둘 다 필요.
        """
        ticker = str(ticker or '').strip().upper()
        target_exchange = str(target_exchange or 'binance').strip().lower()

        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}

        # 유효한 futures 거래소인지 확인 (hedge_service 가 재검증하지만, 조기 실패)
        if target_exchange not in getattr(backend_config, 'EXCHANGES_WITH_FUTURES', set()):
            return {
                'ok': False,
                'code': 'INVALID_EXCHANGE',
                'message': f'{target_exchange} not in EXCHANGES_WITH_FUTURES',
            }

        if notional_usd is None or notional_usd <= 0:
            notional_usd = self.cfg.notional_usd
        notional_usd = float(notional_usd)

        if expected_gap_widening_pct <= 0:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': 'expected_gap_widening_pct must be > 0',
            }
        if max_hold_hours <= 0:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': 'max_hold_hours must be > 0',
            }

        async with self._lock:
            # 1) 안전 게이트
            gate_ok, gate_reason = self._can_enter(ticker, notional_usd)
            if not gate_ok:
                logger.info('[preposition] %s BLOCKED: %s', ticker, gate_reason)
                return {'ok': False, 'code': 'SAFETY_BLOCK', 'message': gate_reason}

            # 2) 현재 gap 조회 + neutral zone 검증
            current_gap = self._read_current_gap(ticker, target_exchange)
            if current_gap is None:
                return {
                    'ok': False,
                    'code': 'NO_GAP_DATA',
                    'message': f'poller has no {ticker}/{target_exchange} gap',
                }

            if current_gap < self.cfg.neutral_gap_min:
                return {
                    'ok': False,
                    'code': 'ALREADY_REVERSED',
                    'message': (
                        f'gap {current_gap:.0f} < {self.cfg.neutral_gap_min:.0f}: '
                        f'already in 역프 zone, defer to auto_trigger'
                    ),
                }
            if current_gap > self.cfg.neutral_gap_max:
                return {
                    'ok': False,
                    'code': 'KIMCHI_PREMIUM',
                    'message': (
                        f'gap {current_gap:.0f} > {self.cfg.neutral_gap_max:.0f}: '
                        f'김프 구간 — preposition 전략은 gap widening (역프) 방향 베팅'
                    ),
                }

            # 3) hedge_service 를 통한 실제 진입 (또는 dry_run)
            target_exit_gap = current_gap - (expected_gap_widening_pct * 100.0)
            # stop_loss: 반대 드리프트 (김프 방향)
            stop_loss_gap = current_gap + (self.cfg.stop_loss_gap_drift_pct * 100.0)
            max_hold_ts = int(time.time() + max_hold_hours * 3600)

            pre_job_id = f'pre_{uuid.uuid4().hex[:10]}'

            hedge_job_id: Optional[str] = None
            if self.cfg.live_armed:
                try:
                    result = await self.hedge_service.enter(
                        ticker=ticker,
                        futures_exchange=target_exchange,
                        nominal_usd=notional_usd,
                    )
                except Exception as exc:
                    logger.error(
                        '[preposition] %s hedge.enter raised: %s',
                        ticker, exc, exc_info=True,
                    )
                    return {
                        'ok': False,
                        'code': 'HEDGE_ENTER_EXC',
                        'message': f'{type(exc).__name__}: {exc}',
                    }
                if not result.get('ok'):
                    logger.warning(
                        '[preposition] %s hedge.enter failed: %s',
                        ticker, result.get('code'),
                    )
                    return {
                        'ok': False,
                        'code': 'HEDGE_ENTER_FAIL',
                        'message': result.get('message', 'hedge enter failed'),
                        'hedge_result': result,
                    }
                hedge_job = result.get('job') or {}
                hedge_job_id = str(hedge_job.get('job_id') or '') or None
            else:
                logger.info(
                    '[preposition] %s DRY-RUN would enter gap=%.0f → target=%.0f stop=%.0f',
                    ticker, current_gap, target_exit_gap, stop_loss_gap,
                )

            # 4) job 기록
            job = PrePositionJob(
                job_id=pre_job_id,
                hedge_job_id=hedge_job_id,
                ticker=ticker,
                target_exchange=target_exchange,
                notional_usd=notional_usd,
                entry_gap=current_gap,
                target_exit_gap=target_exit_gap,
                stop_loss_gap=stop_loss_gap,
                max_hold_ts=max_hold_ts,
                trigger_reason=trigger_reason,
                created_at=int(time.time()),
                status='open',
            )
            self._jobs[pre_job_id] = job
            self._append_jsonl(job.to_json())

            self._maybe_rollover_daily()
            self._daily_spent_usd += notional_usd
            self._total_enters += 1

            mode = 'LIVE' if self.cfg.live_armed else 'DRY-RUN'
            await self._notify(
                f'🎯 [{mode}] {ticker} preposition ENTER\n'
                f'  gap={current_gap:.0f} → target={target_exit_gap:.0f} stop={stop_loss_gap:.0f}\n'
                f'  ex={target_exchange} ${notional_usd:.0f} hold<={max_hold_hours:.1f}h reason={trigger_reason}'
            )
            return {
                'ok': True,
                'code': 'OK',
                'job': job.to_json(),
                'mode': mode.lower(),
            }

    async def exit_preposition(
        self,
        job_id: str,
        reason: str = 'manual_exit',
    ) -> dict[str, Any]:
        """특정 pre-position job을 청산."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {'ok': False, 'code': 'NOT_FOUND', 'message': f'no job {job_id}'}
            if job.status != 'open':
                return {
                    'ok': False,
                    'code': 'NOT_OPEN',
                    'message': f'status={job.status}',
                }
            return await self._close_locked(job, reason=reason, final_status='closing')

    # ------------------------------------------------------------------
    # 모니터 루프
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """백그라운드: 열린 pre-position 들의 exit 조건 감시."""
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('[preposition] tick error: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _tick(self) -> None:
        async with self._lock:
            open_jobs = self._open_jobs()
        for job in open_jobs:
            try:
                await self._check_job(job)
            except Exception as exc:
                logger.warning(
                    '[preposition] check_job %s error: %s',
                    job.job_id, exc, exc_info=True,
                )

    async def _check_job(self, job: PrePositionJob) -> None:
        now = int(time.time())
        # timeout 최우선
        if now >= job.max_hold_ts:
            async with self._lock:
                if job.status != 'open':
                    return
                self._total_timeouts += 1
                await self._close_locked(job, reason='timeout', final_status='closed_timeout')
            return

        current_gap = self._read_current_gap(job.ticker, job.target_exchange)
        if current_gap is None:
            return

        # win: gap 이 target 아래로 내려감 = 역프 벌어짐 = 수익 확정
        if current_gap <= job.target_exit_gap:
            async with self._lock:
                if job.status != 'open':
                    return
                job.exit_gap = current_gap
                self._total_wins += 1
                await self._close_locked(
                    job, reason='target_hit', final_status='closed_win',
                )
            return

        # stop-loss: 반대 방향으로 벌어짐 (김프 전환)
        if current_gap >= job.stop_loss_gap:
            async with self._lock:
                if job.status != 'open':
                    return
                job.exit_gap = current_gap
                self._total_stops += 1
                await self._close_locked(
                    job, reason='stop_loss', final_status='closed_stop',
                )
            return

    async def _close_locked(
        self,
        job: PrePositionJob,
        reason: str,
        final_status: str,
    ) -> dict[str, Any]:
        """`self._lock` 내부에서 호출. hedge_service.close_job 후 job 상태 갱신."""
        job.status = 'closing'

        hedge_close_result: Optional[dict] = None
        if self.cfg.live_armed and job.hedge_job_id:
            try:
                hedge_close_result = await self.hedge_service.close_job(
                    ticker=job.ticker,
                    reason=f'preposition_{reason}',
                )
            except Exception as exc:
                logger.error(
                    '[preposition] close_job %s raised: %s',
                    job.ticker, exc, exc_info=True,
                )
                job.status = 'closed_err'
                job.close_reason = f'{reason}|err:{type(exc).__name__}'
                job.closed_at = int(time.time())
                self._append_jsonl(job.to_json())
                await self._notify(
                    f'❌ {job.ticker} preposition close EXC: {exc}'
                )
                return {'ok': False, 'code': 'CLOSE_EXC', 'message': str(exc)}

            if hedge_close_result and not hedge_close_result.get('ok'):
                logger.warning(
                    '[preposition] %s hedge.close failed: %s',
                    job.ticker, hedge_close_result.get('code'),
                )
                # close 실패여도 pre-position job 상태는 기록 (오퍼레이터 수동 개입 필요)
                job.status = 'closed_err'
                job.close_reason = f'{reason}|hedge_close_failed'
                job.closed_at = int(time.time())
                self._append_jsonl(job.to_json())
                await self._notify(
                    f'⚠️ {job.ticker} preposition close failed '
                    f'({hedge_close_result.get("code")}): manual intervention needed'
                )
                return {
                    'ok': False,
                    'code': 'CLOSE_HEDGE_FAIL',
                    'message': hedge_close_result.get('message'),
                    'hedge_result': hedge_close_result,
                }

        job.status = final_status
        job.close_reason = reason
        job.closed_at = int(time.time())
        self._append_jsonl(job.to_json())

        mode = 'LIVE' if self.cfg.live_armed else 'DRY-RUN'
        await self._notify(
            f'✅ [{mode}] {job.ticker} preposition CLOSE | reason={reason} '
            f'entry={job.entry_gap:.0f} exit={(job.exit_gap or 0):.0f}'
        )
        return {'ok': True, 'code': 'OK', 'job': job.to_json()}

    # ------------------------------------------------------------------
    # listing_detector 이벤트 훅
    # ------------------------------------------------------------------

    def _on_listing_event(self, event: dict) -> Any:
        """ListingDetector.add_listener 콜백.

        `auto_on_listing=True` 인 경우에만 watchlist 교집합에서 자동 진입.
        """
        if not self.cfg.auto_on_listing:
            return None
        ticker = str(event.get('ticker') or '').strip().upper()
        if not ticker:
            return None

        wl_entry = next(
            (w for w in self._watchlist if w.ticker == ticker),
            None,
        )
        if wl_entry is None:
            # watchlist 에 없으면 무시 (오발 방지)
            return None

        logger.info(
            '[preposition] listing event match: %s (widening=%.1f%% hold=%.1fh)',
            ticker, wl_entry.expected_gap_widening_pct, wl_entry.max_hold_hours,
        )

        # 백그라운드 task 로 실행 (listing_detector 루프 블로킹 방지)
        return asyncio.create_task(
            self.enter_preposition(
                ticker=ticker,
                target_exchange='binance',
                notional_usd=self.cfg.notional_usd,
                expected_gap_widening_pct=wl_entry.expected_gap_widening_pct,
                max_hold_hours=wl_entry.max_hold_hours,
                trigger_reason='listing_detector',
            ),
            name=f'preposition_auto_{ticker}',
        )

    # ------------------------------------------------------------------
    # Watchlist API
    # ------------------------------------------------------------------

    def set_watchlist(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Watchlist 전체 교체 (API POST)."""
        new_list: list[WatchlistEntry] = []
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get('ticker') or '').strip().upper()
            if not ticker:
                continue
            try:
                widening = float(item.get('expected_gap_widening_pct') or 0.0)
                hold = float(item.get('max_hold_hours') or 0.0)
            except (TypeError, ValueError):
                continue
            if widening <= 0 or hold <= 0:
                continue
            new_list.append(
                WatchlistEntry(
                    ticker=ticker,
                    expected_gap_widening_pct=widening,
                    max_hold_hours=hold,
                    note=str(item.get('note') or ''),
                )
            )
        self._watchlist = new_list
        try:
            _save_watchlist(self._watchlist)
        except Exception as exc:
            logger.error('[preposition] watchlist save failed: %s', exc)
            return {'ok': False, 'code': 'SAVE_FAIL', 'message': str(exc)}
        return {'ok': True, 'count': len(self._watchlist)}

    def remove_watchlist(self, ticker: str) -> dict[str, Any]:
        ticker = str(ticker or '').strip().upper()
        before = len(self._watchlist)
        self._watchlist = [w for w in self._watchlist if w.ticker != ticker]
        if len(self._watchlist) == before:
            return {'ok': False, 'code': 'NOT_FOUND', 'message': ticker}
        try:
            _save_watchlist(self._watchlist)
        except Exception as exc:
            logger.error('[preposition] watchlist save failed: %s', exc)
            return {'ok': False, 'code': 'SAVE_FAIL', 'message': str(exc)}
        return {'ok': True, 'removed': ticker, 'count': len(self._watchlist)}

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        open_jobs = self._open_jobs()
        return {
            'running': self._running,
            'config': {
                'enabled': self.cfg.enabled,
                'dry_run': self.cfg.dry_run,
                'live_confirm': self.cfg.live_confirm,
                'live_armed': self.cfg.live_armed,
                'auto_on_listing': self.cfg.auto_on_listing,
                'neutral_gap_min': self.cfg.neutral_gap_min,
                'neutral_gap_max': self.cfg.neutral_gap_max,
                'notional_usd': self.cfg.notional_usd,
                'max_open': self.cfg.max_open,
                'daily_cap_usd': self.cfg.daily_cap_usd,
                'stop_loss_gap_drift_pct': self.cfg.stop_loss_gap_drift_pct,
                'poll_interval_sec': self.cfg.poll_interval_sec,
                'kill_switch_file': self.cfg.kill_switch_file,
            },
            'kill_switch_active': self._kill_switch_active(),
            'daily_spent_usd': round(self._daily_spent_usd, 2),
            'open_positions': [j.to_json() for j in open_jobs],
            'watchlist': [w.to_json() for w in self._watchlist],
            'stats': {
                'total_enters': self._total_enters,
                'total_wins': self._total_wins,
                'total_stops': self._total_stops,
                'total_timeouts': self._total_timeouts,
            },
        }

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _open_jobs(self) -> list[PrePositionJob]:
        return [j for j in self._jobs.values() if j.status == 'open']

    def _read_current_gap(self, ticker: str, target_exchange: str) -> Optional[float]:
        """poller.state 에서 ticker/exchange 의 futures_gap 을 안전하게 조회."""
        try:
            state = self.poller.state
        except Exception:
            return None
        gap_result = state.get(ticker) if isinstance(state, dict) else None
        if not gap_result:
            return None
        ex = gap_result.exchanges.get(target_exchange)
        if not ex:
            return None
        gap = getattr(ex, 'futures_gap', None)
        if gap is None:
            return None
        try:
            g = float(gap)
        except (TypeError, ValueError):
            return None
        # 비상식 값 필터 (5000~15000 = -50%~+50% 밖이면 데이터 오류)
        if g < 5000 or g > 15000:
            return None
        return g

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:
            return False

    def _can_enter(self, ticker: str, notional_usd: float) -> tuple[bool, str]:
        """진입 전 게이트. 모두 통과해야 True."""
        if not self.cfg.enabled:
            return False, 'disabled'
        if self._kill_switch_active():
            return False, f'kill_switch {self.cfg.kill_switch_file}'
        open_jobs = self._open_jobs()
        if len(open_jobs) >= self.cfg.max_open:
            return False, f'max_open={self.cfg.max_open} reached'
        # 같은 티커 중복 방지
        if any(j.ticker == ticker for j in open_jobs):
            return False, f'{ticker} already has open preposition'
        # hedge_service 쪽 활성 hedge 도 체크 (auto_trigger 와 충돌 방지)
        try:
            existing = self.hedge_service._jobs.latest_open_job(ticker=ticker)
        except Exception:
            existing = None
        if existing:
            return False, f'{ticker} has active hedge in hedge_service'
        self._maybe_rollover_daily()
        if self._daily_spent_usd + notional_usd > self.cfg.daily_cap_usd:
            return False, (
                f'daily_cap ({self._daily_spent_usd:.2f}+{notional_usd:.2f}'
                f'>{self.cfg.daily_cap_usd:.2f})'
            )
        return True, 'ok'

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self._daily_reset_epoch:
            logger.info(
                '[preposition] daily rollover: spent=%.2f reset',
                self._daily_spent_usd,
            )
            self._daily_spent_usd = 0.0
            self._daily_reset_epoch = today

    def _append_jsonl(self, payload: dict[str, Any]) -> None:
        """매 상태 변경 시 jsonl append (append-only audit log)."""
        try:
            with PREPOSITION_JOBS_FILE.open('a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception as exc:
            logger.warning('[preposition] jsonl append failed: %s', exc)

    def _reload_open_jobs(self) -> None:
        """재시작 시 jsonl 에서 마지막 상태가 open 인 job 복구."""
        if not PREPOSITION_JOBS_FILE.exists():
            return
        try:
            with PREPOSITION_JOBS_FILE.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:
            logger.warning('[preposition] jsonl reload failed: %s', exc)
            return
        # 각 job_id 별 마지막 레코드만 유지
        latest: dict[str, dict] = {}
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            jid = rec.get('job_id')
            if not jid:
                continue
            latest[jid] = rec
        for jid, rec in latest.items():
            if rec.get('status') != 'open':
                continue
            try:
                job = PrePositionJob(
                    job_id=jid,
                    hedge_job_id=rec.get('hedge_job_id'),
                    ticker=str(rec.get('ticker') or '').upper(),
                    target_exchange=str(rec.get('target_exchange') or 'binance'),
                    notional_usd=float(rec.get('notional_usd') or 0.0),
                    entry_gap=float(rec.get('entry_gap') or 10000.0),
                    target_exit_gap=float(rec.get('target_exit_gap') or 9500.0),
                    stop_loss_gap=float(rec.get('stop_loss_gap') or 10300.0),
                    max_hold_ts=int(rec.get('max_hold_ts') or 0),
                    trigger_reason=str(rec.get('trigger_reason') or 'manual'),
                    created_at=int(rec.get('created_at') or int(time.time())),
                    status='open',
                )
            except (TypeError, ValueError) as exc:
                logger.warning('[preposition] reload skip %s: %s', jid, exc)
                continue
            self._jobs[jid] = job
        if self._jobs:
            logger.info('[preposition] reloaded %d open jobs', len(self._open_jobs()))

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        try:
            if self.telegram is not None:
                send = getattr(self.telegram, '_send_message', None)
                if send is not None:
                    try:
                        await send(text, alert_key=alert_key)
                    except TypeError:
                        await send(text)
        except Exception as exc:
            logger.debug('[preposition] telegram send failed: %s', exc)


# ----------------------------------------------------------------------
# 모듈 레벨 유틸
# ----------------------------------------------------------------------


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()
