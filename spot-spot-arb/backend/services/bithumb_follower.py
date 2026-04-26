"""Bithumb 후따리 (Late Follower) — Phase 5.

Upbit 상장이 먼저 터진 뒤, Bithumb 에도 같은 코인이 상장되면 Bithumb 가격이
Upbit 대비 아직 덜 끌어올려진 구간을 노려 Bithumb 에서 KRW 매수한 뒤
갭이 수렴하면(또는 시간 초과/손절 조건) 매도한다.

실제 사용 예 (4/21 CHIP):
- Upbit + Bithumb + Binance 동시 상장
- Upbit 140억원, Bithumb 12억원 — Bithumb 쪽이 펌핑이 느림
- Bithumb 체결 개시 직후 매수 → Upbit 에 수렴할 때까지 보유

데이터 흐름:
  listing_detector (Phase 1)
      └─ add_listener → BithumbFollower._on_event
             └─ _watch_ticker (asyncio task / 티커)
                    ├─ Upbit /orderbook (curl_cffi)
                    ├─ Bithumb /orderbook (httpx)
                    ├─ 조건 충족 → submit_bithumb_spot_order(buy)
                    └─ _monitor_exit → submit_bithumb_spot_order(sell)

안전장치 (Phase 2-4 패턴 준수):
- FOLLOWER_ENABLED / FOLLOWER_DRY_RUN / FOLLOWER_LIVE_CONFIRM 3중 락
- kill switch: data/KILL_FOLLOWER 파일
- per-ticker single-fire (한 티커는 봇 라이프타임 내 1회)
- daily cap (기본 20만원)
- max wait: 60분 (감지 후 Bithumb 체결이 60분 내 안 열리면 포기)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Env helpers (auto_transfer_service / listing_detector 패턴)
# ----------------------------------------------------------------------

def _env(key: str, default: str = '') -> str:
    raw = os.getenv(key)
    return raw.strip() if raw else default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        return float(default)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(float(raw.strip()))
    except ValueError:
        return int(default)


# ----------------------------------------------------------------------
# 상수
# ----------------------------------------------------------------------

_DEFAULT_JOBS_PATH = 'data/follower_jobs.jsonl'
_DEFAULT_KILL_FILE = 'data/KILL_FOLLOWER'
_WATCH_POLL_SEC = 2.0                # 진입 전 Upbit/Bithumb 폴링 주기
_EXIT_POLL_SEC = 2.0                 # 청산 감시 폴링 주기
_UPBIT_LISTING_WAIT_SEC = 300        # Bithumb 이벤트 먼저 받은 경우 Upbit 측도 기다려볼 시간
_GAP_CONVERGED_PCT = 0.5             # 갭 <0.5% 수렴 시 매도
_MIN_UPBIT_REFERENCE_KRW = 0.01      # Upbit bid/ask 가 이보다 작으면 이상 데이터


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------

@dataclass
class FollowerConfig:
    enabled: bool
    dry_run: bool
    live_confirm: bool
    notional_krw: float
    min_gap_pct: float
    min_bithumb_volume_krw: float
    max_wait_min: float
    stop_loss_pct: float
    daily_cap_krw: float
    kill_switch_file: str
    jobs_path: str

    @staticmethod
    def load() -> FollowerConfig:
        return FollowerConfig(
            enabled=_env_bool('FOLLOWER_ENABLED', True),
            dry_run=_env_bool('FOLLOWER_DRY_RUN', True),
            live_confirm=_env_bool('FOLLOWER_LIVE_CONFIRM', False),
            notional_krw=_env_float('FOLLOWER_NOTIONAL_KRW', 50000.0),
            min_gap_pct=_env_float('FOLLOWER_MIN_GAP_PCT', 3.0),
            min_bithumb_volume_krw=_env_float(
                'FOLLOWER_MIN_BITHUMB_VOLUME_KRW', 1_000_000.0
            ),
            max_wait_min=_env_float('FOLLOWER_MAX_WAIT_MIN', 60.0),
            stop_loss_pct=_env_float('FOLLOWER_STOP_LOSS_PCT', -5.0),
            daily_cap_krw=_env_float('FOLLOWER_DAILY_CAP_KRW', 200_000.0),
            kill_switch_file=_env('FOLLOWER_KILL_SWITCH_FILE', _DEFAULT_KILL_FILE),
            jobs_path=_env('FOLLOWER_JOBS_PATH', _DEFAULT_JOBS_PATH),
        )


@dataclass
class WatchJob:
    """티커별 관찰/진입/청산 lifecycle 상태."""

    job_id: str
    ticker: str
    source_exchange: str           # 'upbit' | 'bithumb' (감지 진원지)
    detected_ts: float
    notice_url: str = ''
    # 진입 정보
    entry_ts: float = 0.0
    entry_price_krw: Optional[float] = None   # Bithumb ask
    entry_qty: Optional[float] = None          # 수량 (notional/ask)
    upbit_ref_krw: Optional[float] = None      # 진입 시점 Upbit mid/ask
    entry_gap_pct: Optional[float] = None
    bithumb_buy_order: Optional[dict[str, Any]] = None
    # 청산 정보
    exit_ts: float = 0.0
    exit_price_krw: Optional[float] = None
    exit_reason: str = ''
    bithumb_sell_order: Optional[dict[str, Any]] = None
    pnl_krw: Optional[float] = None
    pnl_pct: Optional[float] = None
    # 메타
    state: str = 'WATCHING'  # WATCHING | ENTERED | EXITED | ABORTED | GIVE_UP
    dry_run: bool = True
    aborted: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'ticker': self.ticker,
            'source_exchange': self.source_exchange,
            'detected_ts': int(self.detected_ts),
            'notice_url': self.notice_url,
            'entry_ts': int(self.entry_ts) if self.entry_ts else 0,
            'entry_price_krw': self.entry_price_krw,
            'entry_qty': self.entry_qty,
            'upbit_ref_krw': self.upbit_ref_krw,
            'entry_gap_pct': self.entry_gap_pct,
            'bithumb_buy_order': self.bithumb_buy_order,
            'exit_ts': int(self.exit_ts) if self.exit_ts else 0,
            'exit_price_krw': self.exit_price_krw,
            'exit_reason': self.exit_reason,
            'bithumb_sell_order': self.bithumb_sell_order,
            'pnl_krw': self.pnl_krw,
            'pnl_pct': self.pnl_pct,
            'state': self.state,
            'dry_run': self.dry_run,
            'aborted': self.aborted,
        }


# ----------------------------------------------------------------------
# Bithumb follower service
# ----------------------------------------------------------------------

class BithumbFollower:
    """Bithumb 후따리 서비스.

    생성 시 `listing_detector.add_listener(self._on_event)` 로 구독하므로,
    Phase 1 detector 가 돌아가는 한 자동으로 이벤트를 받아 처리한다.
    """

    def __init__(
        self,
        listing_detector: Any,
        bithumb_client: Any = None,
        poller: Any = None,
        telegram_service: Any = None,
    ) -> None:
        self.listing_detector = listing_detector
        # bithumb_client: backend.exchanges.bithumb_private 모듈을 그대로 받거나,
        # submit_bithumb_spot_order 를 노출하는 어떤 객체든 허용
        if bithumb_client is None:
            try:
                from backend.exchanges import bithumb_private as _bp  # lazy import
                bithumb_client = _bp
            except Exception as exc:  # noqa: BLE001
                logger.warning('BithumbFollower bithumb_client lazy import failed: %s', exc)
                bithumb_client = None
        self.bithumb_client = bithumb_client
        self.poller = poller  # 현재는 사용하지 않음 (직접 HTTP). 향후 통합용
        self.telegram = telegram_service

        self.cfg = FollowerConfig.load()
        self.jobs_path = Path(self.cfg.jobs_path)

        # 런타임 상태
        self._running: bool = False
        self._session: Optional[CurlAsyncSession] = None
        self._watch_tasks: dict[str, asyncio.Task[Any]] = {}
        self._fired_tickers: set[str] = set()   # per-ticker single-fire
        self._pending_upbit_seen: dict[str, float] = {}   # ticker -> ts (Upbit 먼저 감지)
        self._pending_bithumb_seen: dict[str, float] = {} # ticker -> ts (Bithumb 먼저 감지)
        self._job_store: dict[str, WatchJob] = {}
        self._daily_spent_krw: float = 0.0
        self._daily_reset_ts: float = _today_midnight_epoch()
        self._write_lock = asyncio.Lock()

        self._total_events: int = 0
        self._total_entries: int = 0
        self._total_exits: int = 0

        # 의존성 점검
        self._has_submit = bool(
            self.bithumb_client is not None
            and hasattr(self.bithumb_client, 'submit_bithumb_spot_order')
        )
        if not self._has_submit:
            logger.warning(
                '[follower] bithumb_client lacks submit_bithumb_spot_order → '
                'live execution disabled (dry-run only)'
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if self.listing_detector is None:
            logger.warning('[follower] listing_detector is None; not starting')
            return

        self._running = True
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)

        # curl_cffi 세션: Upbit public orderbook 은 Cloudflare 보호 없음이지만
        # 일관성을 위해 impersonate 사용
        self._session = CurlAsyncSession(impersonate='chrome124', timeout=8.0)

        # listener 등록 (add_listener 가 있으면 사용, 없으면 파일 tail 대체는 미지원)
        add_listener = getattr(self.listing_detector, 'add_listener', None)
        if callable(add_listener):
            add_listener(self._on_event)
        else:
            logger.warning(
                '[follower] listing_detector has no add_listener; follower will stay idle'
            )

        logger.info(
            '[follower] started | enabled=%s dry_run=%s live_confirm=%s '
            'notional=₩%.0f min_gap=%.2f%% daily_cap=₩%.0f kill=%s',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.notional_krw, self.cfg.min_gap_pct,
            self.cfg.daily_cap_krw, self.cfg.kill_switch_file,
        )
        await self._notify(
            '🏃 후따리 서비스 시작\n'
            f'enabled={self.cfg.enabled} dry_run={self.cfg.dry_run} '
            f'live_confirm={self.cfg.live_confirm}\n'
            f'진입: Bithumb<Upbit {self.cfg.min_gap_pct:.1f}%↓ + '
            f'볼륨>{int(self.cfg.min_bithumb_volume_krw):,}원/min\n'
            f'주문 ₩{int(self.cfg.notional_krw):,} / '
            f'일일 캡 ₩{int(self.cfg.daily_cap_krw):,}',
            alert_key='started_follower',
        )

    async def stop(self) -> None:
        self._running = False

        # 리스너 제거 (중복 등록 방지)
        remove_listener = getattr(self.listing_detector, 'remove_listener', None)
        if callable(remove_listener):
            try:
                remove_listener(self._on_event)
            except Exception:  # noqa: BLE001
                pass

        tasks = list(self._watch_tasks.values())
        self._watch_tasks.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        session = self._session
        self._session = None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

        logger.info('[follower] stopped')

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    async def _on_event(self, event: dict[str, Any]) -> None:
        """ListingDetector 로부터 호출되는 비동기 콜백."""
        if not self._running:
            return
        if not self.cfg.enabled:
            logger.debug('[follower] disabled; dropping event %s', event.get('ticker'))
            return

        exchange = str(event.get('exchange', '')).strip().lower()
        ticker = str(event.get('ticker', '')).strip().upper()
        if not ticker or exchange not in {'upbit', 'bithumb'}:
            return

        self._total_events += 1

        # 이미 실행됐거나 관찰 중인 티커면 중복 방지
        if ticker in self._fired_tickers:
            logger.info('[follower] %s already fired lifetime, ignore', ticker)
            return
        if ticker in self._watch_tasks:
            # 다른 쪽 거래소의 후속 이벤트라면 pending 만 갱신하고 기존 task 가 처리
            if exchange == 'bithumb':
                self._pending_bithumb_seen[ticker] = event.get('ts') or time.time()
            else:
                self._pending_upbit_seen[ticker] = event.get('ts') or time.time()
            logger.debug('[follower] %s already watching; noted %s side', ticker, exchange)
            return

        # Upbit-only 감지: Bithumb 공지 (혹은 orderbook live) 를 기다린다
        if exchange == 'upbit':
            self._pending_upbit_seen[ticker] = event.get('ts') or time.time()
        else:
            self._pending_bithumb_seen[ticker] = event.get('ts') or time.time()

        notice_url = str(event.get('url') or '')
        job = WatchJob(
            job_id=uuid.uuid4().hex[:12],
            ticker=ticker,
            source_exchange=exchange,
            detected_ts=float(event.get('ts') or time.time()),
            notice_url=notice_url,
            dry_run=self.cfg.dry_run,
        )
        self._job_store[ticker] = job

        task = asyncio.create_task(
            self._watch_ticker(job),
            name=f'follower_watch_{ticker}',
        )
        self._watch_tasks[ticker] = task
        logger.info(
            '[follower] watch start ticker=%s source=%s job=%s',
            ticker, exchange, job.job_id,
        )

    # ------------------------------------------------------------------
    # Per-ticker watch loop (진입 전)
    # ------------------------------------------------------------------

    async def _watch_ticker(self, job: WatchJob) -> None:
        """Bithumb 거래가 실제로 열리고 갭 조건이 맞을 때까지 감시 → 매수."""
        started = time.monotonic()
        max_wait_sec = max(10.0, self.cfg.max_wait_min * 60.0)

        try:
            while self._running and not job.aborted:
                if (time.monotonic() - started) > max_wait_sec:
                    job.state = 'GIVE_UP'
                    job.exit_reason = 'max_wait_exceeded'
                    await self._persist_job(job)
                    logger.info(
                        '[follower] %s watch give-up after %.0fs',
                        job.ticker, max_wait_sec,
                    )
                    return

                if self._kill_switch_active():
                    job.state = 'ABORTED'
                    job.exit_reason = 'kill_switch'
                    await self._persist_job(job)
                    logger.warning('[follower] %s kill switch active', job.ticker)
                    return

                # 1) Bithumb 가격 + 볼륨 조회
                bithumb_bbo = await self._fetch_bithumb_bbo(job.ticker)
                bithumb_vol = await self._fetch_bithumb_last_minute_vol_krw(job.ticker)
                upbit_bbo = await self._fetch_upbit_bbo(job.ticker)

                if not bithumb_bbo or not upbit_bbo:
                    await asyncio.sleep(_WATCH_POLL_SEC)
                    continue

                bithumb_ask = bithumb_bbo.get('ask')
                upbit_mid = upbit_bbo.get('mid') or upbit_bbo.get('ask')
                if (
                    bithumb_ask is None
                    or upbit_mid is None
                    or upbit_mid < _MIN_UPBIT_REFERENCE_KRW
                ):
                    await asyncio.sleep(_WATCH_POLL_SEC)
                    continue

                # gap% = (upbit - bithumb)/upbit * 100  → 양수 = Bithumb 가 싸다
                gap_pct = ((upbit_mid - float(bithumb_ask)) / float(upbit_mid)) * 100.0

                logger.debug(
                    '[follower] %s watch bith_ask=%.4f upbit_mid=%.4f gap=%.2f%% '
                    'bith_vol1m=%.0f',
                    job.ticker, float(bithumb_ask), float(upbit_mid), gap_pct,
                    bithumb_vol,
                )

                # 2) 진입 조건 확인
                if (
                    gap_pct >= self.cfg.min_gap_pct
                    and bithumb_vol >= self.cfg.min_bithumb_volume_krw
                ):
                    await self._try_enter(
                        job=job,
                        bithumb_ask=float(bithumb_ask),
                        upbit_ref=float(upbit_mid),
                        gap_pct=gap_pct,
                        bithumb_vol=bithumb_vol,
                    )
                    return   # 진입 후에는 _monitor_exit 가 생애주기 인계받음

                await asyncio.sleep(_WATCH_POLL_SEC)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error('[follower] %s watch error: %s', job.ticker, exc, exc_info=True)
            job.state = 'ABORTED'
            job.exit_reason = f'watch_exception: {type(exc).__name__}'
            await self._persist_job(job)
        finally:
            # 감시 단계에서 종료된 경우 task 슬롯 정리 (진입 시에는 monitor_exit 가 재등록)
            if job.state in {'GIVE_UP', 'ABORTED'}:
                self._watch_tasks.pop(job.ticker, None)

    # ------------------------------------------------------------------
    # 진입 (Bithumb market buy)
    # ------------------------------------------------------------------

    async def _try_enter(
        self,
        job: WatchJob,
        bithumb_ask: float,
        upbit_ref: float,
        gap_pct: float,
        bithumb_vol: float,
    ) -> None:
        # daily cap (일일 롤오버 고려)
        self._maybe_rollover_daily()
        notional = float(self.cfg.notional_krw)
        if notional <= 0:
            job.state = 'ABORTED'
            job.exit_reason = 'notional_zero'
            await self._persist_job(job)
            return

        if self._daily_spent_krw + notional > self.cfg.daily_cap_krw:
            job.state = 'ABORTED'
            job.exit_reason = (
                f'daily_cap_exceeded (spent={int(self._daily_spent_krw):,}+'
                f'{int(notional):,}>{int(self.cfg.daily_cap_krw):,})'
            )
            logger.warning('[follower] %s %s', job.ticker, job.exit_reason)
            await self._persist_job(job)
            return

        # per-ticker single-fire 최종 확인
        if job.ticker in self._fired_tickers:
            job.state = 'ABORTED'
            job.exit_reason = 'already_fired'
            await self._persist_job(job)
            return
        self._fired_tickers.add(job.ticker)

        qty = notional / max(bithumb_ask, 1e-9)
        job.entry_price_krw = bithumb_ask
        job.entry_qty = qty
        job.upbit_ref_krw = upbit_ref
        job.entry_gap_pct = gap_pct
        job.entry_ts = time.time()

        # 3중 락: enabled + !dry_run + live_confirm
        live = (
            self.cfg.enabled
            and not self.cfg.dry_run
            and self.cfg.live_confirm
            and self._has_submit
        )

        if not live:
            logger.info(
                '[DRY-FOLLOWER] would buy %s ₩%.0f at gap %.2f%% '
                '(bith_ask=%.4f upbit=%.4f qty=%.6f)',
                job.ticker, notional, gap_pct, bithumb_ask, upbit_ref, qty,
            )
            job.state = 'ENTERED'
            job.bithumb_buy_order = {'dry_run': True, 'simulated_ask': bithumb_ask}
            self._daily_spent_krw += notional
            self._total_entries += 1
            await self._persist_job(job)
            await self._notify(
                f'🏃 [DRY] 후따리 매수 {job.ticker} '
                f'Bithumb @{bithumb_ask:,.4f} '
                f'(Upbit {upbit_ref:,.4f}, gap {gap_pct:.2f}%, '
                f'notional ₩{int(notional):,}, qty {qty:.4f})',
                alert_key='dry_follower',
            )
            # 드라이도 청산 시뮬 모니터링 (실거래 흐름 검증용)
            asyncio.create_task(
                self._monitor_exit(job),
                name=f'follower_exit_{job.ticker}',
            )
            self._watch_tasks[job.ticker] = asyncio.current_task() or asyncio.create_task(
                asyncio.sleep(0)
            )
            return

        # LIVE 실매수 경로
        symbol = f'{job.ticker}/KRW'
        try:
            order = await self.bithumb_client.submit_bithumb_spot_order(
                symbol=symbol,
                side='buy',
                amount=qty,
                reference_price=bithumb_ask,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error('[follower] %s live buy failed: %s', job.ticker, exc)
            job.state = 'ABORTED'
            job.exit_reason = f'buy_exception: {exc}'
            await self._persist_job(job)
            await self._notify(f'❌ 후따리 {job.ticker} 매수 실패: {exc}')
            return

        job.bithumb_buy_order = order if isinstance(order, dict) else {'raw': str(order)}
        job.state = 'ENTERED'
        self._daily_spent_krw += notional
        self._total_entries += 1
        await self._persist_job(job)
        await self._notify(
            f'🏃 후따리 매수: {job.ticker} Bithumb @{bithumb_ask:,.4f} '
            f'(Upbit {upbit_ref:,.4f}, gap {gap_pct:.2f}%, qty {qty:.4f}, '
            f'notional ₩{int(notional):,})'
        )

        # 청산 모니터링은 별도 태스크로 분리
        asyncio.create_task(
            self._monitor_exit(job),
            name=f'follower_exit_{job.ticker}',
        )

    # ------------------------------------------------------------------
    # 청산 모니터링
    # ------------------------------------------------------------------

    async def _monitor_exit(self, job: WatchJob) -> None:
        if job.entry_price_krw is None or job.upbit_ref_krw is None:
            logger.warning('[follower] %s monitor_exit without entry data', job.ticker)
            return
        entry_price = float(job.entry_price_krw)
        entry_upbit = float(job.upbit_ref_krw)
        max_hold_sec = max(60.0, self.cfg.max_wait_min * 60.0)
        started = time.monotonic()

        try:
            while self._running:
                if job.aborted:
                    reason = 'manual_abort'
                    await self._close_position(job, reason)
                    return

                if self._kill_switch_active():
                    await self._close_position(job, 'kill_switch')
                    return

                elapsed = time.monotonic() - started
                if elapsed > max_hold_sec:
                    await self._close_position(job, 'max_hold_exceeded')
                    return

                bithumb_bbo = await self._fetch_bithumb_bbo(job.ticker)
                upbit_bbo = await self._fetch_upbit_bbo(job.ticker)
                if not bithumb_bbo or not upbit_bbo:
                    await asyncio.sleep(_EXIT_POLL_SEC)
                    continue

                bith_bid = bithumb_bbo.get('bid')
                upbit_mid = upbit_bbo.get('mid') or upbit_bbo.get('bid')

                # Upbit 펌프가 꺼지는지 확인 (진입 기준 Upbit 대비 N% 이상 하락)
                if upbit_mid is not None and upbit_mid >= _MIN_UPBIT_REFERENCE_KRW:
                    upbit_change_pct = (
                        (float(upbit_mid) - entry_upbit) / entry_upbit
                    ) * 100.0
                    if upbit_change_pct <= self.cfg.stop_loss_pct:
                        await self._close_position(
                            job,
                            f'upbit_stop_loss ({upbit_change_pct:.2f}%)',
                        )
                        return

                    # 갭 수렴
                    if bith_bid is not None:
                        gap_now = (
                            (float(upbit_mid) - float(bith_bid)) / float(upbit_mid)
                        ) * 100.0
                        if abs(gap_now) < _GAP_CONVERGED_PCT:
                            await self._close_position(
                                job,
                                f'gap_converged ({gap_now:.2f}%)',
                            )
                            return

                # 2차 안전장치: Bithumb bid 자체가 진입가 대비 stop_loss 이상 하락
                if bith_bid is not None:
                    bith_change_pct = (
                        (float(bith_bid) - entry_price) / entry_price
                    ) * 100.0
                    if bith_change_pct <= self.cfg.stop_loss_pct:
                        await self._close_position(
                            job,
                            f'bithumb_stop_loss ({bith_change_pct:.2f}%)',
                        )
                        return

                await asyncio.sleep(_EXIT_POLL_SEC)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error('[follower] %s monitor error: %s', job.ticker, exc, exc_info=True)
            await self._close_position(job, f'monitor_exception: {type(exc).__name__}')

    async def _close_position(self, job: WatchJob, reason: str) -> None:
        """Bithumb 시장가 매도 (또는 dry-run 로그)."""
        if job.state in {'EXITED', 'ABORTED'}:
            return
        qty = float(job.entry_qty or 0.0)
        if qty <= 0:
            job.state = 'EXITED'
            job.exit_reason = f'{reason} (no_qty)'
            job.exit_ts = time.time()
            await self._persist_job(job)
            self._watch_tasks.pop(job.ticker, None)
            return

        bithumb_bbo = await self._fetch_bithumb_bbo(job.ticker)
        exit_price = None
        if bithumb_bbo:
            exit_price = bithumb_bbo.get('bid') or bithumb_bbo.get('ask')

        live = (
            self.cfg.enabled
            and not self.cfg.dry_run
            and self.cfg.live_confirm
            and self._has_submit
        )

        if not live:
            job.state = 'EXITED'
            job.exit_reason = reason
            job.exit_ts = time.time()
            job.exit_price_krw = float(exit_price) if exit_price else job.entry_price_krw
            job.bithumb_sell_order = {'dry_run': True, 'simulated_bid': exit_price}
            if job.entry_price_krw and job.exit_price_krw and job.entry_qty:
                job.pnl_krw = (job.exit_price_krw - job.entry_price_krw) * job.entry_qty
                job.pnl_pct = (
                    (job.exit_price_krw - job.entry_price_krw) / job.entry_price_krw
                ) * 100.0
            await self._persist_job(job)
            self._total_exits += 1
            self._watch_tasks.pop(job.ticker, None)
            logger.info(
                '[DRY-FOLLOWER] would sell %s qty=%.6f @%.4f (%s) pnl=₩%.0f (%.2f%%)',
                job.ticker, qty, float(exit_price or 0.0), reason,
                float(job.pnl_krw or 0.0), float(job.pnl_pct or 0.0),
            )
            await self._notify(
                f'🏁 [DRY] 후따리 매도 {job.ticker} reason={reason} '
                f'@{float(exit_price or 0.0):,.4f} '
                f'PnL ₩{int(job.pnl_krw or 0):,} ({float(job.pnl_pct or 0):+.2f}%)',
                alert_key='dry_follower',
            )
            return

        symbol = f'{job.ticker}/KRW'
        try:
            order = await self.bithumb_client.submit_bithumb_spot_order(
                symbol=symbol,
                side='sell',
                amount=qty,
                reference_price=exit_price,  # sell에는 ord_type=market (reference_price 미사용)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error('[follower] %s live sell failed: %s', job.ticker, exc)
            job.state = 'ABORTED'
            job.exit_reason = f'{reason} → sell_exception: {exc}'
            job.exit_ts = time.time()
            await self._persist_job(job)
            await self._notify(f'❌ 후따리 {job.ticker} 매도 실패: {exc}')
            self._watch_tasks.pop(job.ticker, None)
            return

        job.bithumb_sell_order = order if isinstance(order, dict) else {'raw': str(order)}
        job.state = 'EXITED'
        job.exit_reason = reason
        job.exit_ts = time.time()
        job.exit_price_krw = float(exit_price) if exit_price else job.entry_price_krw
        if job.entry_price_krw and job.exit_price_krw and job.entry_qty:
            job.pnl_krw = (job.exit_price_krw - job.entry_price_krw) * job.entry_qty
            job.pnl_pct = (
                (job.exit_price_krw - job.entry_price_krw) / job.entry_price_krw
            ) * 100.0
        self._total_exits += 1
        await self._persist_job(job)
        self._watch_tasks.pop(job.ticker, None)

        await self._notify(
            f'🏁 후따리 매도 {job.ticker} reason={reason} '
            f'@{float(job.exit_price_krw or 0):,.4f} '
            f'PnL ₩{int(job.pnl_krw or 0):,} ({float(job.pnl_pct or 0):+.2f}%)'
        )

    # ------------------------------------------------------------------
    # 가격/볼륨 조회 helpers
    # ------------------------------------------------------------------

    async def _fetch_bithumb_bbo(self, ticker: str) -> Optional[dict[str, float]]:
        """Bithumb 공개 orderbook 에서 best bid/ask 조회."""
        url = f'https://api.bithumb.com/public/orderbook/{ticker}_KRW?count=1'
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[follower] bithumb bbo fetch %s failed: %s', ticker, exc)
            return None
        if not isinstance(data, dict) or data.get('status') != '0000':
            return None
        payload = data.get('data')
        if not isinstance(payload, dict):
            return None
        bids = payload.get('bids') or []
        asks = payload.get('asks') or []
        try:
            bid = float(bids[0]['price']) if bids else None
            ask = float(asks[0]['price']) if asks else None
        except (KeyError, TypeError, ValueError):
            return None
        if bid is None and ask is None:
            return None
        return {'bid': bid, 'ask': ask}

    async def _fetch_bithumb_last_minute_vol_krw(self, ticker: str) -> float:
        """Bithumb 분봉(1분) API 로 최근 1분 KRW 거래대금 조회.

        endpoint: /public/candlestick/{TICKER}_KRW/1m
        실패하거나 아직 캔들이 없으면 0.0 반환 (상장 직후엔 정상 흐름).
        """
        url = f'https://api.bithumb.com/public/candlestick/{ticker}_KRW/1m'
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[follower] bithumb candles fetch %s failed: %s', ticker, exc)
            return 0.0
        if not isinstance(data, dict) or data.get('status') != '0000':
            return 0.0
        rows = data.get('data')
        if not isinstance(rows, list) or not rows:
            return 0.0
        # 각 row: [timestamp_ms, open, close, high, low, volume]  (volume = coin qty)
        last = rows[-1]
        if not isinstance(last, list) or len(last) < 6:
            return 0.0
        try:
            close = float(last[2])
            vol_coin = float(last[5])
        except (TypeError, ValueError):
            return 0.0
        vol_krw = close * vol_coin
        return max(0.0, vol_krw)

    async def _fetch_upbit_bbo(self, ticker: str) -> Optional[dict[str, float]]:
        """Upbit 공개 orderbook — bid/ask/mid 반환."""
        market = f'KRW-{ticker}'
        url = 'https://api.upbit.com/v1/orderbook'
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params={'markets': market})
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[follower] upbit bbo fetch %s failed: %s', ticker, exc)
            return None
        if not isinstance(data, list) or not data:
            return None
        entry = data[0]
        if not isinstance(entry, dict):
            return None
        units = entry.get('orderbook_units') or []
        if not units:
            return None
        top = units[0]
        try:
            bid = float(top.get('bid_price') or 0.0) or None
            ask = float(top.get('ask_price') or 0.0) or None
        except (TypeError, ValueError):
            return None
        mid: Optional[float] = None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        elif ask is not None:
            mid = ask
        elif bid is not None:
            mid = bid
        return {'bid': bid, 'ask': ask, 'mid': mid}

    # ------------------------------------------------------------------
    # 안전장치
    # ------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self._daily_reset_ts:
            logger.info(
                '[follower] daily rollover: spent_krw=%.0f reset',
                self._daily_spent_krw,
            )
            self._daily_spent_krw = 0.0
            self._daily_reset_ts = today

    # ------------------------------------------------------------------
    # 영속화
    # ------------------------------------------------------------------

    async def _persist_job(self, job: WatchJob) -> None:
        async with self._write_lock:
            try:
                self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
                with self.jobs_path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(job.to_json(), ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('[follower] persist job %s failed: %s', job.ticker, exc)

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'running': self._running,
            'has_submit_method': self._has_submit,
            'kill_switch_active': self._kill_switch_active(),
            'kill_switch_file': self.cfg.kill_switch_file,
            'config': {
                'notional_krw': self.cfg.notional_krw,
                'min_gap_pct': self.cfg.min_gap_pct,
                'min_bithumb_volume_krw': self.cfg.min_bithumb_volume_krw,
                'max_wait_min': self.cfg.max_wait_min,
                'stop_loss_pct': self.cfg.stop_loss_pct,
                'daily_cap_krw': self.cfg.daily_cap_krw,
                'jobs_path': self.cfg.jobs_path,
            },
            'stats': {
                'total_events': self._total_events,
                'total_entries': self._total_entries,
                'total_exits': self._total_exits,
                'daily_spent_krw': round(self._daily_spent_krw, 2),
                'fired_tickers_count': len(self._fired_tickers),
                'fired_tickers': sorted(self._fired_tickers),
                'active_watches': sorted(self._watch_tasks.keys()),
            },
            'active_jobs': [
                self._job_store[t].to_json()
                for t in self._watch_tasks.keys()
                if t in self._job_store
            ],
        }

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0 or not self.jobs_path.exists():
            return []
        try:
            with self.jobs_path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[follower] recent_jobs read: %s', exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out

    def abort_ticker(self, ticker: str) -> dict[str, Any]:
        """진행 중인 watch/monitor 를 수동으로 강제 청산 신호.

        - 진입 전이면 watch task cancel 후 GIVE_UP 처리
        - 진입 후이면 job.aborted = True → monitor 가 즉시 시장가 매도
        """
        normalized = (ticker or '').strip().upper()
        if not normalized:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
        job = self._job_store.get(normalized)
        task = self._watch_tasks.get(normalized)
        if job is None and task is None:
            return {'ok': False, 'code': 'NOT_FOUND', 'message': 'no active job'}
        if job is not None and job.state == 'ENTERED':
            job.aborted = True
            return {'ok': True, 'code': 'ABORT_REQUESTED', 'ticker': normalized}
        # 진입 전이면 watch task cancel
        if task is not None and not task.done():
            task.cancel()
        if job is not None:
            job.aborted = True
            job.state = 'ABORTED'
            job.exit_reason = 'manual_abort'
            # best-effort persistence
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._persist_job(job))
            except RuntimeError:
                pass
        self._watch_tasks.pop(normalized, None)
        return {'ok': True, 'code': 'WATCH_CANCELLED', 'ticker': normalized}

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is not None:
                # TelegramAlertService._send_message supports alert_key kwarg
                try:
                    result = send(text, alert_key=alert_key)
                except TypeError:
                    result = send(text)
            else:
                send = getattr(self.telegram, 'send_message', None)
                if send is None:
                    return
                result = send(text)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.debug('[follower] telegram send failed: %s', exc)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()
