"""MarginSellArb — Bybit 마진셀 + Binance perp 롱 arbitrage service.

배경 (Pika_Kim + pannpunch + plusevdeal 상위 3개 HIGH 카탈로그):
  - Lombard (Pika msg 271-274): 계정당 12% 확정 수익. Bybit 마진 캡 ~8,000토큰.
  - BARD: 새벽 12% 확정 확인.
  - 전략: Bybit 에서 토큰 차입 → Bybit spot 시장가 매도 → Binance perp 동일 수량 롱.
    네거티브 펀딩 종료 또는 가격 수렴 시 청산.

설계 원칙 (wallet_tracker / dex_trader 동일 패턴):
  - quad-lock + kill switch.
  - DRY_RUN 기본 true. AUTO 기본 false (수동 트리거).
  - 주문/차입/상환은 _write_lock 으로 직렬화.
  - Telegram 예외 무시 (조용히 실패).
  - CCXT 마진 API 는 거래소별로 시그니처가 많이 달라 LIVE branch 는 stub.
    opportunity scanner + dry-run 시뮬레이션 + jsonl 영속 + 수동 API 는 전부 동작.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.exchanges import manager as exchange_manager

logger = logging.getLogger(__name__)


# ======================================================================
# env 헬퍼 — dex_trader / wallet_tracker 과 동일 컨벤션
# ======================================================================


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        return float(default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(raw.strip())
    except ValueError:
        return int(default)


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [t.strip().upper() for t in raw.split(',') if t.strip()]


# ======================================================================
# 설정
# ======================================================================


@dataclass
class MarginArbConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False       # triple-lock
    auto_enabled: bool = False       # fourth-lock (수동 vs 자동)

    # 스캐너 설정
    scan_interval_sec: int = 30
    min_spread_pct: float = 3.0      # Bybit spot - Binance perp spread 최소치 (%)
    dedup_sec: int = 3600            # ticker별 opportunity dedup (1h)
    top_n_alert: int = 3

    # 자본 가드
    max_notional_usd: float = 500.0
    daily_cap_usd: float = 2000.0
    per_ticker_cooldown_sec: int = 7200  # 2h

    # 파일 경로
    jobs_path: str = 'data/margin_arb_jobs.jsonl'
    kill_switch_file: str = 'data/KILL_MARGIN_ARB'

    watchlist: list[str] = field(
        default_factory=lambda: ['BARD', 'LOMBARD', 'LRC', 'VENUS', 'DOT']
    )

    # 디폴트 레그
    default_borrow_exchange: str = 'bybit'
    default_perp_exchange: str = 'binance'
    default_leverage: int = 3

    @classmethod
    def load(cls) -> 'MarginArbConfig':
        return cls(
            enabled=_bool_env('MARGIN_ARB_ENABLED', True),
            dry_run=_bool_env('MARGIN_ARB_DRY_RUN', True),
            live_confirm=_bool_env('MARGIN_ARB_LIVE_CONFIRM', False),
            auto_enabled=_bool_env('MARGIN_ARB_AUTO_ENABLED', False),
            scan_interval_sec=max(_int_env('MARGIN_ARB_SCAN_INTERVAL_SEC', 30), 5),
            min_spread_pct=_float_env('MARGIN_ARB_MIN_SPREAD_PCT', 3.0),
            dedup_sec=max(_int_env('MARGIN_ARB_DEDUP_SEC', 3600), 60),
            top_n_alert=max(_int_env('MARGIN_ARB_TOP_N_ALERT', 3), 1),
            max_notional_usd=max(_float_env('MARGIN_ARB_MAX_NOTIONAL_USD', 500.0), 0.0),
            daily_cap_usd=max(_float_env('MARGIN_ARB_DAILY_CAP_USD', 2000.0), 0.0),
            per_ticker_cooldown_sec=max(_int_env('MARGIN_ARB_COOLDOWN_SEC', 7200), 0),
            jobs_path=_str_env('MARGIN_ARB_JOBS_PATH', 'data/margin_arb_jobs.jsonl'),
            kill_switch_file=_str_env('MARGIN_ARB_KILL_SWITCH_FILE', 'data/KILL_MARGIN_ARB'),
            watchlist=_csv_env('MARGIN_ARB_WATCHLIST', ['BARD', 'LOMBARD', 'LRC', 'VENUS', 'DOT']),
            default_borrow_exchange=_str_env('MARGIN_ARB_BORROW_EXCHANGE', 'bybit').lower(),
            default_perp_exchange=_str_env('MARGIN_ARB_PERP_EXCHANGE', 'binance').lower(),
            default_leverage=max(_int_env('MARGIN_ARB_LEVERAGE', 3), 1),
        )


# ======================================================================
# 상태
# ======================================================================


@dataclass
class _ArbState:
    total_scans: int = 0
    total_opportunities: int = 0
    total_auto_entries: int = 0
    total_manual_entries: int = 0
    total_exits: int = 0
    total_dry_run: int = 0
    total_errors: int = 0
    last_error: Optional[str] = None
    last_scan_ts: float = 0.0

    # ticker -> last opp alert ts (dedup)
    last_opp_ts: dict[str, float] = field(default_factory=dict)
    # ticker -> last entry ts (cooldown)
    last_entry_ts: dict[str, float] = field(default_factory=dict)
    # ticker -> open job_id
    open_jobs: dict[str, str] = field(default_factory=dict)
    # open job cache (job_id -> job dict)
    open_job_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # recent opportunities ring buffer (last 20)
    recent_opps: deque = field(default_factory=lambda: deque(maxlen=20))

    # 일일 notional 사용량
    daily_spent_usd: float = 0.0
    daily_reset_epoch: int = 0


# ======================================================================
# 본체
# ======================================================================


class MarginSellArb:
    """Bybit 마진 차입 + Bybit spot 시장가 매도 + Binance perp 롱 진입.

    수익 원천:
      1) 스팟/펄프 가격 수렴 (시간 지남 → spread 좁아짐)
      2) 네거티브 펀딩 수취 (롱이 펀딩 받음)
      3) 차입 이자 < 스프레드 수익
    """

    def __init__(
        self,
        hedge_service: Any = None,
        telegram_service: Any = None,
        cfg: Optional[MarginArbConfig] = None,
    ) -> None:
        self.hedge = hedge_service
        self.telegram = telegram_service
        self.cfg = cfg or MarginArbConfig.load()
        self.state = _ArbState()

        self._running: bool = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._write_lock = asyncio.Lock()
        self._enter_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        try:
            Path(self.cfg.jobs_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('MarginSellArb jobs_path prep failed: %s', exc)

        if not self.cfg.enabled:
            logger.info('MarginSellArb started (disabled via MARGIN_ARB_ENABLED=false)')
            return

        # 스캐너 태스크 기동
        self._tasks.append(
            asyncio.create_task(self._scanner_loop(), name='margin_arb_scanner')
        )

        logger.info(
            'MarginSellArb started (dry_run=%s live_confirm=%s auto=%s watchlist=%d)',
            self.cfg.dry_run, self.cfg.live_confirm, self.cfg.auto_enabled,
            len(self.cfg.watchlist),
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info('MarginSellArb stopped')

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and (not self._kill_switch_active())
        )
        auto_armed = live_armed and self.cfg.auto_enabled
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'auto_enabled': self.cfg.auto_enabled,
            'live_armed': live_armed,
            'auto_armed': auto_armed,
            'kill_switch_active': self._kill_switch_active(),
            'watchlist': list(self.cfg.watchlist),
            'default_borrow_exchange': self.cfg.default_borrow_exchange,
            'default_perp_exchange': self.cfg.default_perp_exchange,
            'default_leverage': self.cfg.default_leverage,
            'min_spread_pct': self.cfg.min_spread_pct,
            'max_notional_usd': self.cfg.max_notional_usd,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'per_ticker_cooldown_sec': self.cfg.per_ticker_cooldown_sec,
            'scan_interval_sec': self.cfg.scan_interval_sec,
            'total_scans': self.state.total_scans,
            'total_opportunities': self.state.total_opportunities,
            'total_auto_entries': self.state.total_auto_entries,
            'total_manual_entries': self.state.total_manual_entries,
            'total_exits': self.state.total_exits,
            'total_dry_run': self.state.total_dry_run,
            'total_errors': self.state.total_errors,
            'last_error': self.state.last_error,
            'last_scan_ts': self.state.last_scan_ts,
            'open_jobs_count': len(self.state.open_jobs),
            'jobs_path': self.cfg.jobs_path,
            'kill_switch_file': self.cfg.kill_switch_file,
        }

    def open_jobs(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self.state.open_job_cache.values()]

    def recent_opportunities(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items = list(self.state.recent_opps)[-limit:]
        return list(reversed(items))

    # ------------------------------------------------------------------
    # 수동/자동 진입 엔트리 포인트
    # ------------------------------------------------------------------

    async def enter_arb(
        self,
        ticker: str,
        borrow_exchange: str = 'bybit',
        perp_exchange: str = 'binance',
        borrow_qty: float = 0.0,
        leverage: Optional[int] = None,
        origin: str = 'manual',
    ) -> dict[str, Any]:
        """수동 또는 자동 트리거로 arb 진입.

        origin: 'manual' | 'auto' (상태 카운터/메세지에서 사용)
        """
        ticker = str(ticker or '').strip().upper()
        borrow_exchange = str(borrow_exchange or self.cfg.default_borrow_exchange).strip().lower()
        perp_exchange = str(perp_exchange or self.cfg.default_perp_exchange).strip().lower()
        lev = int(leverage or self.cfg.default_leverage)
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
        if borrow_qty <= 0:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'borrow_qty must be > 0'}

        # quad-lock
        if not self.cfg.enabled:
            return {'ok': False, 'code': 'DISABLED', 'message': 'MARGIN_ARB_ENABLED=false'}
        if self._kill_switch_active():
            return {'ok': False, 'code': 'KILL_SWITCH', 'message': f'kill-switch {self.cfg.kill_switch_file} exists'}

        # 쿨다운/중복 진입 체크
        now = time.time()
        last = self.state.last_entry_ts.get(ticker)
        if last is not None and (now - last) < self.cfg.per_ticker_cooldown_sec:
            wait = int(self.cfg.per_ticker_cooldown_sec - (now - last))
            return {
                'ok': False,
                'code': 'COOLDOWN',
                'message': f'{ticker} cooldown {wait}s 남음',
            }
        if ticker in self.state.open_jobs:
            return {
                'ok': False,
                'code': 'ALREADY_OPEN',
                'message': f'{ticker} already open job={self.state.open_jobs[ticker]}',
            }

        # daily cap 사전 체크 (대략치 — 실 mark price 는 진입 flow 내부에서 한번 더 계산)
        self._rollover_daily()
        if self.state.daily_spent_usd >= self.cfg.daily_cap_usd:
            return {
                'ok': False,
                'code': 'DAILY_CAP',
                'message': f'daily_cap ${self.cfg.daily_cap_usd:.0f} 도달',
            }

        async with self._enter_lock:
            return await self._enter_arb_locked(
                ticker=ticker,
                borrow_exchange=borrow_exchange,
                perp_exchange=perp_exchange,
                borrow_qty=float(borrow_qty),
                leverage=lev,
                origin=origin,
            )

    async def _enter_arb_locked(
        self,
        ticker: str,
        borrow_exchange: str,
        perp_exchange: str,
        borrow_qty: float,
        leverage: int,
        origin: str,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now_ms = int(time.time() * 1000)

        # 1) 대출 가능량 조회
        try:
            max_loan, loan_info = await self._fetch_loanable_amount(borrow_exchange, ticker)
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'loanable: {exc}'
            return {'ok': False, 'code': 'LOAN_LOOKUP_FAIL', 'message': str(exc)}

        # dry-run 또는 LIVE 모두 loanable 체크
        if max_loan is not None and borrow_qty > max_loan:
            return {
                'ok': False,
                'code': 'LOAN_CAP_EXCEEDED',
                'message': f'borrow_qty={borrow_qty} > max_loan={max_loan}',
                'loan_info': loan_info,
            }

        # 2) spot/perp 가격 사전 스냅
        try:
            spot_price = await self._fetch_spot_price(borrow_exchange, ticker)
            perp_price = await self._fetch_perp_price(perp_exchange, ticker)
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'prices: {exc}'
            return {'ok': False, 'code': 'PRICE_FETCH_FAIL', 'message': str(exc)}

        notional_usd = borrow_qty * (spot_price or 0.0)
        if self.cfg.max_notional_usd > 0 and notional_usd > self.cfg.max_notional_usd:
            return {
                'ok': False,
                'code': 'NOTIONAL_CAP',
                'message': f'notional ${notional_usd:.2f} > cap ${self.cfg.max_notional_usd:.0f}',
            }

        # 3) LIVE vs DRY
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and (not self._kill_switch_active())
        )

        job: dict[str, Any] = {
            'job_id': job_id,
            'event': 'enter',
            'origin': origin,
            'ticker': ticker,
            'borrow_exchange': borrow_exchange,
            'perp_exchange': perp_exchange,
            'borrow_qty': borrow_qty,
            'leverage': leverage,
            'spot_price_ref': spot_price,
            'perp_price_ref': perp_price,
            'notional_usd': round(notional_usd, 4),
            'created_at_ms': now_ms,
            'status': 'open',
            'mode': 'live' if live_armed else 'dry_run',
        }

        if not live_armed:
            # DRY 시뮬레이션: 즉시 체결 가정
            job['dry_borrow'] = {'status': 'simulated', 'qty': borrow_qty}
            job['dry_sell'] = {
                'status': 'simulated', 'qty': borrow_qty,
                'avg_price': spot_price, 'symbol': f'{ticker}/USDT',
            }
            job['dry_long'] = {
                'status': 'simulated', 'qty': borrow_qty,
                'avg_price': perp_price, 'symbol': f'{ticker}/USDT:USDT',
            }
            self.state.total_dry_run += 1
        else:
            # LIVE: asyncio.gather 로 3 레그 병렬
            try:
                borrow_res, sell_res, long_res = await asyncio.gather(
                    self._exec_borrow(borrow_exchange, ticker, borrow_qty),
                    self._exec_spot_sell(borrow_exchange, ticker, borrow_qty),
                    self._exec_perp_long(perp_exchange, ticker, borrow_qty, leverage),
                    return_exceptions=True,
                )
            except Exception as exc:  # noqa: BLE001
                self.state.total_errors += 1
                self.state.last_error = f'enter_gather: {exc}'
                return {'ok': False, 'code': 'EXEC_FAIL', 'message': str(exc)}

            job['borrow'] = self._leg_to_dict(borrow_res)
            job['sell'] = self._leg_to_dict(sell_res)
            job['long'] = self._leg_to_dict(long_res)

        # 4) 상태 갱신 + 영속
        self.state.last_entry_ts[ticker] = time.time()
        self.state.open_jobs[ticker] = job_id
        self.state.open_job_cache[job_id] = job
        if origin == 'auto':
            self.state.total_auto_entries += 1
        else:
            self.state.total_manual_entries += 1
        self.state.daily_spent_usd += notional_usd

        await self._append_jobs_record(job)

        await self._send_telegram(
            f'🩲 마진셀 arb 진입 ({origin}): {ticker} qty={borrow_qty} '
            f'@ {borrow_exchange} spot≈${spot_price} + {perp_exchange} long≈${perp_price} '
            f'notional=${notional_usd:.2f} mode={job["mode"]}'
        )

        return {'ok': True, 'job_id': job_id, 'job': job}

    # ------------------------------------------------------------------
    # 청산
    # ------------------------------------------------------------------

    async def exit_arb(self, job_id: str) -> dict[str, Any]:
        job_id = str(job_id or '').strip()
        if not job_id:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
        job = self.state.open_job_cache.get(job_id)
        if job is None:
            return {'ok': False, 'code': 'NOT_FOUND', 'message': f'job {job_id} not open'}

        async with self._enter_lock:
            return await self._exit_arb_locked(job)

    async def _exit_arb_locked(self, job: dict[str, Any]) -> dict[str, Any]:
        ticker = str(job.get('ticker') or '').upper()
        borrow_exchange = str(job.get('borrow_exchange') or '').lower()
        perp_exchange = str(job.get('perp_exchange') or '').lower()
        borrow_qty = float(job.get('borrow_qty') or 0.0)

        if ticker == '' or borrow_qty <= 0:
            return {'ok': False, 'code': 'INVALID_JOB', 'message': 'missing ticker/qty'}

        # 최신 가격 스냅
        spot_cover_price: float | None = None
        perp_close_price: float | None = None
        try:
            spot_cover_price = await self._fetch_spot_price(borrow_exchange, ticker)
            perp_close_price = await self._fetch_perp_price(perp_exchange, ticker)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[margin-arb] exit price fetch failed: %s', exc)

        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and (not self._kill_switch_active())
        )

        cover_qty = borrow_qty * 1.005  # 0.5% buffer (이자 커버)

        exit_record: dict[str, Any] = {
            'job_id': job['job_id'],
            'event': 'exit',
            'ticker': ticker,
            'borrow_exchange': borrow_exchange,
            'perp_exchange': perp_exchange,
            'borrow_qty': borrow_qty,
            'cover_qty': cover_qty,
            'spot_cover_price': spot_cover_price,
            'perp_close_price': perp_close_price,
            'closed_at_ms': int(time.time() * 1000),
            'mode': 'live' if live_armed else 'dry_run',
        }

        if not live_armed:
            exit_record['dry_perp_close'] = {'status': 'simulated', 'qty': borrow_qty, 'avg_price': perp_close_price}
            exit_record['dry_spot_buy'] = {'status': 'simulated', 'qty': cover_qty, 'avg_price': spot_cover_price}
            exit_record['dry_repay'] = {'status': 'simulated', 'qty': borrow_qty}
            self.state.total_dry_run += 1
        else:
            # 순서 주의: (1) perp 롱 청산 먼저 (가격 노출 제거) → (2) spot BUY → (3) repay
            try:
                perp_close_res = await self._exec_perp_close(perp_exchange, ticker, borrow_qty)
            except Exception as exc:  # noqa: BLE001
                perp_close_res = {'status': 'failed', 'error': str(exc)}
            exit_record['perp_close'] = self._leg_to_dict(perp_close_res)

            try:
                spot_buy_res = await self._exec_spot_buy(borrow_exchange, ticker, cover_qty)
            except Exception as exc:  # noqa: BLE001
                spot_buy_res = {'status': 'failed', 'error': str(exc)}
            exit_record['spot_buy'] = self._leg_to_dict(spot_buy_res)

            try:
                repay_res = await self._exec_repay(borrow_exchange, ticker, borrow_qty)
            except Exception as exc:  # noqa: BLE001
                repay_res = {'status': 'failed', 'error': str(exc)}
            exit_record['repay'] = self._leg_to_dict(repay_res)

        # P&L 근사치 (dry-run 기준 이론치, LIVE 는 avg_price 필드가 있을 때 사용)
        entry_spot = float(job.get('spot_price_ref') or 0.0)
        entry_perp = float(job.get('perp_price_ref') or 0.0)
        exit_spot = float(spot_cover_price or 0.0)
        exit_perp = float(perp_close_price or 0.0)
        # short 수익(spot sell -> buy back), long 수익(perp)
        pnl_short = (entry_spot - exit_spot) * borrow_qty
        pnl_long = (exit_perp - entry_perp) * borrow_qty
        interest_est = float(job.get('notional_usd') or 0.0) * 0.0005  # 0.05% 가정
        fee_est = float(job.get('notional_usd') or 0.0) * 0.0006       # 3 legs * ~0.02%
        pnl_gross = pnl_short + pnl_long
        pnl_net = pnl_gross - interest_est - fee_est
        exit_record['pnl_breakdown'] = {
            'pnl_short': round(pnl_short, 4),
            'pnl_long': round(pnl_long, 4),
            'interest_est': round(interest_est, 4),
            'fee_est': round(fee_est, 4),
            'pnl_gross': round(pnl_gross, 4),
            'pnl_net': round(pnl_net, 4),
        }

        # 상태 정리
        self.state.open_jobs.pop(ticker, None)
        self.state.open_job_cache.pop(job['job_id'], None)
        self.state.total_exits += 1

        await self._append_jobs_record(exit_record)

        await self._send_telegram(
            f'🟢 마진셀 arb 청산: {ticker} qty={borrow_qty} '
            f'pnl_net≈${pnl_net:+.2f} (short {pnl_short:+.2f} / long {pnl_long:+.2f})'
        )

        return {'ok': True, 'job_id': job['job_id'], 'exit': exit_record}

    # ------------------------------------------------------------------
    # 스캐너 루프
    # ------------------------------------------------------------------

    async def _scanner_loop(self) -> None:
        logger.info('[margin-arb] scanner start (interval=%ds)', self.cfg.scan_interval_sec)
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.state.total_errors += 1
                self.state.last_error = f'scan: {exc}'
                logger.warning('[margin-arb] scan err: %s', exc)
            await asyncio.sleep(self.cfg.scan_interval_sec)

    async def _scan_once(self) -> None:
        self.state.total_scans += 1
        self.state.last_scan_ts = time.time()
        self._rollover_daily()

        opps: list[dict[str, Any]] = []
        for ticker in self.cfg.watchlist:
            try:
                opp = await self._evaluate_ticker(ticker)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[margin-arb] evaluate %s err: %s', ticker, exc)
                continue
            if opp is None:
                continue
            opps.append(opp)

        if not opps:
            return

        # 정렬: spread desc. funding < 0 은 약간 가점
        opps.sort(
            key=lambda o: (
                o['spread_pct'] + (1.0 if (o.get('funding_rate') or 0) < 0 else 0.0)
            ),
            reverse=True,
        )

        now = time.time()
        alerts: list[dict[str, Any]] = []
        for opp in opps[: self.cfg.top_n_alert]:
            last = self.state.last_opp_ts.get(opp['ticker'])
            if last is not None and (now - last) < self.cfg.dedup_sec:
                continue
            self.state.last_opp_ts[opp['ticker']] = now
            self.state.total_opportunities += 1
            self.state.recent_opps.append(opp)
            alerts.append(opp)

        if alerts:
            await self._emit_opp_alerts(alerts)

        # 자동 트리거
        if not self.cfg.auto_enabled:
            return
        for opp in alerts:
            try:
                res = await self.enter_arb(
                    ticker=opp['ticker'],
                    borrow_exchange=opp['borrow_exchange'],
                    perp_exchange=opp['perp_exchange'],
                    borrow_qty=opp['suggested_qty'],
                    leverage=self.cfg.default_leverage,
                    origin='auto',
                )
                logger.info('[margin-arb] auto-enter %s -> %s', opp['ticker'], res.get('code') or 'OK')
            except Exception as exc:  # noqa: BLE001
                logger.warning('[margin-arb] auto-enter %s err: %s', opp['ticker'], exc)

    async def _evaluate_ticker(self, ticker: str) -> dict[str, Any] | None:
        borrow_ex = self.cfg.default_borrow_exchange
        perp_ex = self.cfg.default_perp_exchange

        # 1) 대출 가능량
        try:
            max_loan, _ = await self._fetch_loanable_amount(borrow_ex, ticker)
        except Exception:
            max_loan = None
        if max_loan is None or max_loan <= 0:
            return None

        # 2) 가격
        try:
            spot = await self._fetch_spot_price(borrow_ex, ticker)
            perp = await self._fetch_perp_price(perp_ex, ticker)
        except Exception:
            return None
        if not spot or not perp or spot <= 0 or perp <= 0:
            return None

        spread_pct = (spot - perp) / perp * 100.0
        if spread_pct < self.cfg.min_spread_pct:
            return None

        # 3) 펀딩 (bonus — 실패해도 opp 성립)
        funding = None
        try:
            funding = await self._fetch_perp_funding(perp_ex, ticker)
        except Exception:
            funding = None

        # 제안 수량: max_notional_usd / spot, loanable cap
        if spot > 0 and self.cfg.max_notional_usd > 0:
            suggested_qty = min(max_loan, self.cfg.max_notional_usd / spot)
        else:
            suggested_qty = max_loan
        suggested_qty = round(suggested_qty, 6)

        return {
            'ticker': ticker,
            'borrow_exchange': borrow_ex,
            'perp_exchange': perp_ex,
            'spot_price': spot,
            'perp_price': perp,
            'spread_pct': round(spread_pct, 4),
            'funding_rate': funding,
            'max_loan': max_loan,
            'suggested_qty': suggested_qty,
            'est_notional_usd': round(suggested_qty * spot, 2),
            'ts': time.time(),
        }

    async def _emit_opp_alerts(self, opps: list[dict[str, Any]]) -> None:
        lines = ['🔎 마진셀 arb 기회:']
        for o in opps:
            fund = (
                f" funding={o['funding_rate']:+.4%}"
                if isinstance(o.get('funding_rate'), (int, float))
                else ''
            )
            lines.append(
                f"  · {o['ticker']}: spread={o['spread_pct']:+.2f}%"
                f" spot=${o['spot_price']:.4f} perp=${o['perp_price']:.4f}"
                f" max_loan={o['max_loan']} sug_qty={o['suggested_qty']}{fund}"
            )
        await self._send_telegram('\n'.join(lines), alert_key='margin_dry')

    # ------------------------------------------------------------------
    # CCXT / 거래소 어댑터 (조회)
    # ------------------------------------------------------------------

    async def _fetch_loanable_amount(
        self, exchange: str, ticker: str
    ) -> tuple[float | None, dict[str, Any]]:
        """거래소별 대출 가능 수량 조회. 실패 시 (None, {}).

        Bybit v5: /v5/spot-margin-trade/interest-rate-history + /v5/crypto-loan/loanable-data
          또는 UTA 라면 /v5/account/borrow-history. CCXT 에서 privateGetV5...
          하지만 ticker별 수량은 계정 컨텍스트 + VIP 티어에 의존해서 보수적으로
          None 반환 후 live branch 에서 실제 조회하도록 한다.
        Binance: sapiV1GetMarginMaxBorrowable(asset=)
        """
        exchange = (exchange or '').lower()
        ticker = ticker.upper()

        try:
            if exchange == 'bybit':
                inst = exchange_manager.get_instance('bybit', 'spot')
                if inst is None:
                    return None, {'error': 'bybit_spot not initialized'}
                # v5 공개 엔드포인트로 대출 가능한 코인 리스트 체크 (수량은 계정별이라 보수적)
                try:
                    payload = await inst.publicGetV5CryptoLoanCommonLoanableData()
                except Exception as exc:
                    return None, {'error': f'bybit loanable list: {exc}'}
                rows = (payload or {}).get('result', {}).get('list', []) or []
                found: dict[str, Any] | None = None
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    coin = str(row.get('currency') or row.get('loanCurrency') or '').upper()
                    if coin == ticker:
                        found = row
                        break
                if found is None:
                    return 0.0, {'supported': False}
                # maxLoanAmount 또는 maxLoanQty 필드가 존재하면 사용
                for key in ('maxLoanAmount', 'maxLoanQty', 'maxBorrowingAmount'):
                    raw = found.get(key)
                    try:
                        if raw is not None:
                            return float(raw), {'supported': True, 'raw': found}
                    except (TypeError, ValueError):
                        continue
                # 수량은 못 가져왔지만 '지원' 은 확인. 8000 (Pika 참고치) fallback.
                return 8000.0, {'supported': True, 'fallback_cap': 8000, 'raw': found}

            if exchange == 'binance':
                inst = exchange_manager.get_instance('binance', 'spot')
                if inst is None:
                    return None, {'error': 'binance_spot not initialized'}
                try:
                    payload = await inst.sapiGetMarginMaxBorrowable({'asset': ticker})
                except Exception as exc:
                    return None, {'error': f'binance maxBorrowable: {exc}'}
                amount = payload.get('amount') if isinstance(payload, dict) else None
                try:
                    return float(amount) if amount is not None else None, {'raw': payload}
                except (TypeError, ValueError):
                    return None, {'raw': payload}

            return None, {'error': f'unsupported borrow exchange: {exchange}'}
        except Exception as exc:  # noqa: BLE001
            return None, {'error': str(exc)}

    async def _fetch_spot_price(self, exchange: str, ticker: str) -> float | None:
        inst = exchange_manager.get_instance(exchange, 'spot')
        if inst is None:
            return None
        symbol = exchange_manager.get_symbol(ticker, 'spot', exchange)
        try:
            bbo = await exchange_manager.fetch_bbo(inst, symbol)
            if bbo is None:
                return None
            # spot sell 기준 best bid 가 현실적 체결가
            return float(bbo.bid or bbo.ask or 0) or None
        except Exception:
            try:
                t = await inst.fetch_ticker(symbol)
                last = t.get('last') or t.get('close') or t.get('bid') or t.get('ask')
                return float(last) if last else None
            except Exception:
                return None

    async def _fetch_perp_price(self, exchange: str, ticker: str) -> float | None:
        inst = exchange_manager.get_instance(exchange, 'swap')
        if inst is None:
            return None
        symbol = exchange_manager.get_symbol(ticker, 'swap')
        try:
            bbo = await exchange_manager.fetch_bbo(inst, symbol)
            if bbo is None:
                return None
            # perp long 기준 best ask 가 현실적 체결가
            return float(bbo.ask or bbo.bid or 0) or None
        except Exception:
            try:
                t = await inst.fetch_ticker(symbol)
                last = t.get('last') or t.get('close') or t.get('ask') or t.get('bid')
                return float(last) if last else None
            except Exception:
                return None

    async def _fetch_perp_funding(self, exchange: str, ticker: str) -> float | None:
        inst = exchange_manager.get_instance(exchange, 'swap')
        if inst is None:
            return None
        symbol = exchange_manager.get_symbol(ticker, 'swap')
        try:
            if not inst.has.get('fetchFundingRate'):
                return None
            data = await inst.fetch_funding_rate(symbol)
            rate = data.get('fundingRate') if isinstance(data, dict) else None
            return float(rate) if rate is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # LIVE 주문 실행 — stub 들. 실제 배포 시 Phase X.1 에서 구현.
    # ------------------------------------------------------------------

    async def _exec_borrow(self, exchange: str, ticker: str, qty: float) -> dict[str, Any]:
        """Bybit v5 /v5/spot-margin-trade/loan 또는 Binance sapi/v1/margin/loan."""
        raise NotImplementedError(
            'margin sell live (borrow leg) — Phase X.1: Bybit v5/spot-margin-trade/loan 서명 필요'
        )

    async def _exec_spot_sell(self, exchange: str, ticker: str, qty: float) -> dict[str, Any]:
        """Spot 시장가 SELL — 차입한 토큰 즉시 매각. hedge_service._submit_market_order 재사용 가능."""
        raise NotImplementedError(
            'margin sell live (spot sell leg) — Phase X.1: hedge_service._submit_market_order 재사용'
        )

    async def _exec_spot_buy(self, exchange: str, ticker: str, qty: float) -> dict[str, Any]:
        """Cover — spot 시장가 BUY."""
        raise NotImplementedError(
            'margin sell live (spot buy leg) — Phase X.1: hedge_service._submit_market_order 재사용'
        )

    async def _exec_perp_long(
        self, exchange: str, ticker: str, qty: float, leverage: int
    ) -> dict[str, Any]:
        """Perp 롱 진입. leverage 설정 + market BUY."""
        raise NotImplementedError(
            'margin sell live (perp long leg) — Phase X.1: set_leverage + create_order BUY'
        )

    async def _exec_perp_close(self, exchange: str, ticker: str, qty: float) -> dict[str, Any]:
        """Perp 롱 청산 — market SELL reduceOnly."""
        raise NotImplementedError(
            'margin sell live (perp close leg) — Phase X.1: create_order SELL reduceOnly=True'
        )

    async def _exec_repay(self, exchange: str, ticker: str, qty: float) -> dict[str, Any]:
        """Bybit v5 /v5/spot-margin-trade/repay 또는 Binance margin/repay."""
        raise NotImplementedError(
            'margin sell live (repay leg) — Phase X.1: Bybit v5/spot-margin-trade/repay 서명 필요'
        )

    # ------------------------------------------------------------------
    # 유틸 — kill switch / daily rollover / jsonl / telegram
    # ------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:
            return False

    def _rollover_daily(self) -> None:
        today = int(time.time() // 86400)
        if today > self.state.daily_reset_epoch:
            if self.state.daily_spent_usd > 0:
                logger.info(
                    '[margin-arb] daily rollover: spent=$%.2f reset',
                    self.state.daily_spent_usd,
                )
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    @staticmethod
    def _leg_to_dict(res: Any) -> dict[str, Any]:
        if isinstance(res, BaseException):
            return {'status': 'failed', 'error': str(res)}
        if isinstance(res, dict):
            return res
        return {'status': 'ok', 'raw': str(res)}

    async def _append_jobs_record(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        if 'ts' not in payload:
            payload['ts'] = time.time()
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('MarginSellArb jobs append failed (%s): %s', path, exc)

    async def _send_telegram(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is not None:
                try:
                    await send(text, alert_key=alert_key)
                    return
                except TypeError:
                    await send(text)
                    return
            send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('MarginSellArb telegram err: %s', exc)
