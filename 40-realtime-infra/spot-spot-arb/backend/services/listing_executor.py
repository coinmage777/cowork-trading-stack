"""상장 자동 숏 실행기 (Listing Executor) — Phase 2: CEX 자동 숏.

`listing_detector.py` 가 Upbit/Bithumb 공지에서 신규 상장을 감지하면,
바이낸스 또는 바이빗 선물(perp) 이 존재할 경우 즉시 해외 선물에 시장가
SHORT 을 오픈한다 (상장 펌프 구간을 캡처).

실행 경로는 세 겹의 잠금으로만 열린다:
    LISTING_EXECUTOR_ENABLED=true
    AND LISTING_EXECUTOR_DRY_RUN=false
    AND LISTING_EXECUTOR_LIVE_CONFIRM=true
그 외에는 모두 dry-run 으로 기록된다 (주문 없음).

Phase 3 (DEX 매수) 는 `add_listener` 로 이 모듈과 병렬로 붙이면 된다 —
detector 이벤트는 모든 리스너에 팬아웃된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import ccxt.async_support as ccxt  # type: ignore

from backend import config
from backend.exchanges import manager as exchange_manager

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# env 헬퍼 (listing_detector 와 동일 컨벤션)
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class ListingExecutorConfig:
    enabled: bool = False
    dry_run: bool = True           # 기본 True — 안전
    live_confirm: bool = False     # 트리플 락 중 3번째
    notional_usd: float = 50.0
    leverage: int = 5
    prefer_exchange: str = 'binance'  # binance > bybit
    daily_cap_usd: float = 300.0
    per_ticker_cooldown_min: int = 60
    max_event_age_sec: int = 300      # 재시작 시 오래된 이벤트 무시
    kill_switch_file: str = 'data/KILL_LISTING'
    jobs_path: str = 'data/listing_hedge_jobs.jsonl'

    @classmethod
    def load(cls) -> 'ListingExecutorConfig':
        return cls(
            enabled=_bool_env('LISTING_EXECUTOR_ENABLED', False),
            dry_run=_bool_env('LISTING_EXECUTOR_DRY_RUN', True),
            live_confirm=_bool_env('LISTING_EXECUTOR_LIVE_CONFIRM', False),
            notional_usd=max(_float_env('LISTING_EXECUTOR_NOTIONAL_USD', 50.0), 0.0),
            leverage=max(_int_env('LISTING_EXECUTOR_LEVERAGE', 5), 1),
            prefer_exchange=_str_env('LISTING_EXECUTOR_PREFER_EXCHANGE', 'binance').lower(),
            daily_cap_usd=max(_float_env('LISTING_EXECUTOR_DAILY_CAP_USD', 300.0), 0.0),
            per_ticker_cooldown_min=max(
                _int_env('LISTING_EXECUTOR_PER_TICKER_COOLDOWN_MIN', 60), 0
            ),
            max_event_age_sec=max(
                _int_env('LISTING_EXECUTOR_MAX_EVENT_AGE_SEC', 300), 0
            ),
            kill_switch_file=_str_env('LISTING_EXECUTOR_KILL_SWITCH_FILE', 'data/KILL_LISTING'),
            jobs_path=_str_env('LISTING_EXECUTOR_JOBS_PATH', 'data/listing_hedge_jobs.jsonl'),
        )


@dataclass
class _ExecutorState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = 0.0
    last_entry_ts_per_ticker: dict[str, float] = field(default_factory=dict)
    # ticker -> {order_id, exchange, symbol, qty, avg_price, ts, mode}
    open_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_detected: int = 0
    total_executed: int = 0
    total_skipped: int = 0
    total_dry_run: int = 0
    total_errors: int = 0
    last_error: str = ''
    last_executed_ts: float = 0.0


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ----------------------------------------------------------------------
# 실행기 본체
# ----------------------------------------------------------------------


class ListingExecutor:
    """listing_detector 구독 → 조건 충족 시 선물 SHORT 진입.

    사용 예:
        executor = ListingExecutor(
            listing_detector=listing_detector,
            hedge_service=hedge_trade_service,
            telegram_service=telegram,
        )
        await executor.start()
        ...
        await executor.stop()

    안전: ENABLED AND (not DRY_RUN) AND LIVE_CONFIRM 일 때만 실주문.
    """

    def __init__(
        self,
        listing_detector: Any,
        hedge_service: Any,
        telegram_service: Any = None,
        cfg: Optional[ListingExecutorConfig] = None,
    ) -> None:
        self.detector = listing_detector
        self.hedge = hedge_service
        self.telegram = telegram_service
        self.cfg = cfg or ListingExecutorConfig.load()
        self.state = _ExecutorState(daily_reset_epoch=_today_midnight_epoch())

        self._running: bool = False
        self._write_lock = asyncio.Lock()
        self._inflight_tickers: set[str] = set()

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if self.detector is None:
            logger.warning('ListingExecutor: detector is None, skipping start')
            return
        self._running = True
        # jsonl 디렉토리 선준비
        try:
            Path(self.cfg.jobs_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('ListingExecutor jobs_path prep failed: %s', exc)

        # detector 에 콜백 등록
        if hasattr(self.detector, 'add_listener'):
            self.detector.add_listener(self._on_listing_event)
            logger.info(
                'ListingExecutor started (enabled=%s dry_run=%s live_confirm=%s '
                'notional=$%.0f leverage=%dx prefer=%s daily_cap=$%.0f)',
                self.cfg.enabled,
                self.cfg.dry_run,
                self.cfg.live_confirm,
                self.cfg.notional_usd,
                self.cfg.leverage,
                self.cfg.prefer_exchange,
                self.cfg.daily_cap_usd,
            )
        else:
            logger.error(
                'ListingExecutor: detector %s has no add_listener; executor inactive',
                type(self.detector).__name__,
            )

    async def stop(self) -> None:
        self._running = False
        if self.detector is not None and hasattr(self.detector, 'remove_listener'):
            try:
                self.detector.remove_listener(self._on_listing_event)
            except Exception:  # noqa: BLE001
                pass
        logger.info('ListingExecutor stopped')

    # ------------------------------------------------------------------
    # 상태 조회
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        live_armed = self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,  # 실주문 가능 상태
            'kill_switch_active': self._kill_switch_active(),
            'prefer_exchange': self.cfg.prefer_exchange,
            'notional_usd': self.cfg.notional_usd,
            'leverage': self.cfg.leverage,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'per_ticker_cooldown_min': self.cfg.per_ticker_cooldown_min,
            'max_event_age_sec': self.cfg.max_event_age_sec,
            'cooldown_tickers': {
                k: max(
                    0,
                    int(self.cfg.per_ticker_cooldown_min * 60 - (time.time() - v)),
                )
                for k, v in self.state.last_entry_ts_per_ticker.items()
                if time.time() - v < self.cfg.per_ticker_cooldown_min * 60
            },
            'open_jobs': {k: dict(v) for k, v in self.state.open_jobs.items()},
            'total_detected': self.state.total_detected,
            'total_executed': self.state.total_executed,
            'total_dry_run': self.state.total_dry_run,
            'total_skipped': self.state.total_skipped,
            'total_errors': self.state.total_errors,
            'last_error': self.state.last_error,
            'last_executed_ts': self.state.last_executed_ts,
            'jobs_path': self.cfg.jobs_path,
            'kill_switch_file': self.cfg.kill_switch_file,
        }

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """`jobs_path` jsonl 에서 최근 N 개 record 를 읽는다 (대시보드 용)."""
        path = Path(self.cfg.jobs_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('ListingExecutor recent_jobs read failed: %s', exc)
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

    # ------------------------------------------------------------------
    # 리스너 엔트리포인트
    # ------------------------------------------------------------------

    def _on_listing_event(self, event: dict[str, Any]) -> Any:
        """detector 가 감지 즉시 호출. async 태스크로 위임 후 return."""
        self.state.total_detected += 1
        if not self._running:
            return None
        # 감지 루프를 블로킹하지 않도록 백그라운드 태스크로 분리
        try:
            return asyncio.create_task(
                self._handle_listing_safe(event),
                name=f'listing_exec_{event.get("ticker", "unknown")}',
            )
        except RuntimeError:
            # 이벤트 루프가 없는 경우 (테스트 등) — 동기 skip
            return None

    async def _handle_listing_safe(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_listing(event)
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'{type(exc).__name__}: {exc}'
            logger.exception('ListingExecutor unexpected error: %s', exc)

    # ------------------------------------------------------------------
    # 핵심 처리 — 게이트 + 실행
    # ------------------------------------------------------------------

    async def _handle_listing(self, event: dict[str, Any]) -> None:
        ticker = str(event.get('ticker') or '').strip().upper()
        origin_exchange = str(event.get('exchange') or '').strip().lower()
        notice_id = str(event.get('id') or event.get('notice_id') or '')
        event_ts = float(event.get('ts') or 0.0)
        binance_perp = bool(event.get('binance_perp'))
        bybit_perp = bool(event.get('bybit_perp'))

        if not ticker:
            logger.debug('[listing-exec] skip: empty ticker')
            self.state.total_skipped += 1
            return

        # ---- 게이트 1: perp 존재 여부
        target = self._select_futures_exchange(binance_perp, bybit_perp)
        if target is None:
            logger.info(
                '[listing-exec] skip %s (%s id=%s): no binance/bybit perp',
                ticker, origin_exchange, notice_id,
            )
            self.state.total_skipped += 1
            return

        # ---- 게이트 2: 이벤트 신선도 (재시작 시 과거 이벤트 무시)
        now_ts = time.time()
        if event_ts > 0 and self.cfg.max_event_age_sec > 0:
            age = now_ts - event_ts
            if age > self.cfg.max_event_age_sec:
                logger.info(
                    '[listing-exec] skip %s: event too old (%.0fs > %ds)',
                    ticker, age, self.cfg.max_event_age_sec,
                )
                self.state.total_skipped += 1
                return

        # ---- 게이트 3: 중복 인플라이트
        if ticker in self._inflight_tickers:
            logger.info('[listing-exec] skip %s: inflight already', ticker)
            self.state.total_skipped += 1
            return

        # ---- 게이트 4: enabled
        if not self.cfg.enabled:
            logger.info('[listing-exec] skip %s: executor disabled', ticker)
            self.state.total_skipped += 1
            return

        # ---- 게이트 5: kill switch
        if self._kill_switch_active():
            logger.warning(
                '[listing-exec] skip %s: kill switch %s present',
                ticker, self.cfg.kill_switch_file,
            )
            self.state.total_skipped += 1
            return

        # ---- 게이트 6: per-ticker cooldown
        if self.cfg.per_ticker_cooldown_min > 0:
            last_ts = self.state.last_entry_ts_per_ticker.get(ticker, 0.0)
            cooldown_sec = self.cfg.per_ticker_cooldown_min * 60
            elapsed = now_ts - last_ts
            if elapsed < cooldown_sec:
                logger.info(
                    '[listing-exec] skip %s: cooldown %ds remaining',
                    ticker, int(cooldown_sec - elapsed),
                )
                self.state.total_skipped += 1
                return

        # ---- 게이트 7: daily cap
        self._maybe_rollover_daily()
        if self.state.daily_spent_usd + self.cfg.notional_usd > self.cfg.daily_cap_usd:
            logger.warning(
                '[listing-exec] skip %s: daily cap ($%.2f + $%.2f > $%.2f)',
                ticker,
                self.state.daily_spent_usd,
                self.cfg.notional_usd,
                self.cfg.daily_cap_usd,
            )
            self.state.total_skipped += 1
            return

        # ---- 게이트 8: 이미 open 인 listing 헷지 잡이 있음
        if ticker in self.state.open_jobs:
            logger.info('[listing-exec] skip %s: open listing job already exists', ticker)
            self.state.total_skipped += 1
            return
        # 그리고 전체 hedge_service 쪽 ticker 에도 열린 잡 있으면 스킵
        if self.hedge is not None and hasattr(self.hedge, 'get_latest_open_job'):
            try:
                existing = self.hedge.get_latest_open_job(ticker)
            except Exception:  # noqa: BLE001
                existing = None
            if existing:
                logger.info(
                    '[listing-exec] skip %s: hedge_service already has open job (id=%s)',
                    ticker, existing.get('id') or existing.get('job_id') or '?',
                )
                self.state.total_skipped += 1
                return

        # ---- inflight 등록
        self._inflight_tickers.add(ticker)
        try:
            # ---- 게이트 9 (트리플 락): LIVE 확정 여부
            live_armed = (
                self.cfg.enabled
                and (not self.cfg.dry_run)
                and self.cfg.live_confirm
            )
            if not live_armed:
                await self._record_dry_run(
                    ticker=ticker,
                    futures_exchange=target,
                    origin_exchange=origin_exchange,
                    notice_id=notice_id,
                    event_ts=event_ts,
                )
                return

            await self._execute_short(
                ticker=ticker,
                futures_exchange=target,
                origin_exchange=origin_exchange,
                notice_id=notice_id,
                event_ts=event_ts,
            )
        finally:
            self._inflight_tickers.discard(ticker)

    def _select_futures_exchange(self, binance: bool, bybit: bool) -> Optional[str]:
        prefer = self.cfg.prefer_exchange
        if prefer == 'bybit':
            if bybit:
                return 'bybit'
            if binance:
                return 'binance'
            return None
        # default: binance > bybit
        if binance:
            return 'binance'
        if bybit:
            return 'bybit'
        return None

    # ------------------------------------------------------------------
    # 실행 경로 — 실주문
    # ------------------------------------------------------------------

    async def _execute_short(
        self,
        ticker: str,
        futures_exchange: str,
        origin_exchange: str,
        notice_id: str,
        event_ts: float,
    ) -> None:
        """선물 거래소에 시장가 SHORT 오픈 (reduceOnly=False).

        hedge_trade_service 의 `_prepare_futures_account` + `_submit_market_order`
        를 재사용한다 (이미 fill polling, set_leverage 구현 포함).
        """
        leverage = self.cfg.leverage
        notional_usd = self.cfg.notional_usd

        if self.hedge is None:
            msg = 'hedge_service is None'
            logger.error('[listing-exec] %s abort: %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            return

        instance = exchange_manager.get_instance(futures_exchange, 'swap')
        if instance is None:
            msg = f'{futures_exchange} swap instance unavailable'
            logger.error('[listing-exec] %s abort: %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, futures_exchange, msg)
            return

        try:
            if not instance.markets:
                await instance.load_markets()
        except Exception as exc:  # noqa: BLE001
            msg = f'load_markets failed: {exc}'
            logger.warning('[listing-exec] %s %s', ticker, msg)
            # 치명 실패는 아님 — 아래 create_order 에서 한번 더 시도됨

        symbol_futures = exchange_manager.get_symbol(
            ticker=ticker,
            market_type='swap',
            exchange_id=futures_exchange,
        )

        # mark/best price 조회 → qty 정규화
        reference_price: Optional[float] = None
        try:
            bbo = await exchange_manager.fetch_bbo(instance, symbol_futures)
            if bbo is not None:
                # SHORT 오픈은 best bid 로 팔아치움 → bid 기준 qty 산출이 안전
                reference_price = float(bbo.bid) if bbo.bid else (
                    float(bbo.ask) if bbo.ask else None
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning('[listing-exec] %s fetch_bbo failed: %s', ticker, exc)

        if reference_price is None or reference_price <= 0:
            msg = f'{futures_exchange} price unavailable for {ticker}'
            logger.error('[listing-exec] %s abort: %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, futures_exchange, msg)
            return

        raw_qty = notional_usd / reference_price
        try:
            qty = self._normalize_amount(instance, symbol_futures, raw_qty)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[listing-exec] %s normalize qty fallback: %s', ticker, exc)
            qty = raw_qty
        if qty <= 0:
            msg = 'normalized qty <= 0'
            logger.error('[listing-exec] %s abort: %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            return

        # 레버리지 설정
        warnings: list[str] = []
        try:
            warnings = await self.hedge._prepare_futures_account(  # type: ignore[attr-defined]
                exchange_instance=instance,
                symbol=symbol_futures,
                leverage=leverage,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'_prepare_futures_account failed: {exc}')
            logger.warning('[listing-exec] %s leverage prep err: %s', ticker, exc)

        # 실주문 — SHORT 오픈 (reduceOnly=False)
        logger.info(
            '[listing-exec] LIVE %s -> SHORT %s via %s qty=%.8f notional=$%.2f lev=%dx',
            ticker, symbol_futures, futures_exchange, qty, notional_usd, leverage,
        )
        try:
            order_result = await self.hedge._submit_market_order(  # type: ignore[attr-defined]
                exchange_instance=instance,
                exchange_name=futures_exchange,
                symbol=symbol_futures,
                side='sell',
                amount=qty,
                market='futures',
                reference_price=reference_price,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f'submit_market_order exception: {exc}'
            logger.exception('[listing-exec] %s %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, futures_exchange, msg)
            return

        filled_qty = float(order_result.get('filled_qty') or 0.0)
        avg_price = order_result.get('avg_price')
        status = str(order_result.get('status') or '').lower()
        err = order_result.get('error')

        if filled_qty <= 0 or err or status not in {'closed', 'filled', 'ok'}:
            msg = f'order failed status={status} filled={filled_qty} err={err}'
            logger.error('[listing-exec] %s %s', ticker, msg)
            self.state.total_errors += 1
            self.state.last_error = msg
            # 일단 시도한 기록은 남긴다
            await self._append_jobs_record({
                'ts': int(time.time()),
                'trade_type': 'listing_short_only',
                'mode': 'live_failed',
                'ticker': ticker,
                'futures_exchange': futures_exchange,
                'listing_event_id': notice_id,
                'listing_origin_exchange': origin_exchange,
                'listing_event_ts': int(event_ts),
                'entry_notional_usd': notional_usd,
                'leverage': leverage,
                'requested_qty': qty,
                'order_result': order_result,
                'warnings': warnings,
                'error': msg,
            })
            await self._telegram_error(ticker, futures_exchange, msg)
            return

        # 성공 — 상태 갱신 + jsonl + telegram
        self.state.daily_spent_usd += notional_usd
        self.state.last_entry_ts_per_ticker[ticker] = time.time()
        self.state.total_executed += 1
        self.state.last_executed_ts = time.time()
        self.state.open_jobs[ticker] = {
            'ticker': ticker,
            'futures_exchange': futures_exchange,
            'symbol': symbol_futures,
            'entry_qty': filled_qty,
            'entry_avg': avg_price,
            'entry_notional_usd': notional_usd,
            'leverage': leverage,
            'listing_event_id': notice_id,
            'listing_origin_exchange': origin_exchange,
            'listing_event_ts': int(event_ts),
            'ts': int(time.time()),
            'order_id': order_result.get('order_id'),
            'mode': 'live',
        }

        await self._append_jobs_record({
            'ts': int(time.time()),
            'trade_type': 'listing_short_only',
            'mode': 'live',
            'ticker': ticker,
            'futures_exchange': futures_exchange,
            'symbol': symbol_futures,
            'entry_qty': filled_qty,
            'entry_avg': avg_price,
            'entry_notional_usd': notional_usd,
            'leverage': leverage,
            'listing_event_id': notice_id,
            'listing_origin_exchange': origin_exchange,
            'listing_event_ts': int(event_ts),
            'order_id': order_result.get('order_id'),
            'warnings': warnings,
        })

        logger.info(
            '[listing-exec] OPEN OK %s @%.8f x%d via %s qty=%.8f',
            ticker, float(avg_price or 0.0), leverage, futures_exchange, filled_qty,
        )
        await self._telegram_open_ok(
            ticker=ticker,
            futures_exchange=futures_exchange,
            avg_price=avg_price,
            leverage=leverage,
            qty=filled_qty,
            notional=notional_usd,
        )

    # ------------------------------------------------------------------
    # 실행 경로 — dry run
    # ------------------------------------------------------------------

    async def _record_dry_run(
        self,
        ticker: str,
        futures_exchange: str,
        origin_exchange: str,
        notice_id: str,
        event_ts: float,
    ) -> None:
        logger.info(
            '[DRY-LISTING] would short %s $%.0f x%d via %s (origin=%s id=%s)',
            ticker,
            self.cfg.notional_usd,
            self.cfg.leverage,
            futures_exchange,
            origin_exchange,
            notice_id,
        )
        self.state.total_dry_run += 1
        # 드라이런도 per-ticker cooldown 적용 (같은 이벤트 재발화 방지)
        self.state.last_entry_ts_per_ticker[ticker] = time.time()
        await self._append_jobs_record({
            'ts': int(time.time()),
            'trade_type': 'listing_short_only',
            'mode': 'dry_run',
            'ticker': ticker,
            'futures_exchange': futures_exchange,
            'entry_notional_usd': self.cfg.notional_usd,
            'leverage': self.cfg.leverage,
            'listing_event_id': notice_id,
            'listing_origin_exchange': origin_exchange,
            'listing_event_ts': int(event_ts),
            'reason_not_live': self._dry_reason(),
        })
        if self.telegram is not None:
            text = (
                f'🧪 [DRY] 상장 숏: {ticker} ${self.cfg.notional_usd:.0f} '
                f'x{self.cfg.leverage} via {futures_exchange} (id={notice_id})'
            )
            await self._send_telegram(text)

    def _dry_reason(self) -> str:
        if not self.cfg.enabled:
            return 'disabled'
        if self.cfg.dry_run:
            return 'dry_run_env'
        if not self.cfg.live_confirm:
            return 'live_confirm_off'
        return 'unknown'

    # ------------------------------------------------------------------
    # 수동 청산
    # ------------------------------------------------------------------

    async def close(self, ticker: str, reason: str = 'manual') -> dict[str, Any]:
        """listing short 포지션을 시장가 reduceOnly BUY 로 청산.

        hedge_service._submit_futures_close_generic_reduce_only 를 재사용.
        """
        ticker = str(ticker or '').strip().upper()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}

        job = self.state.open_jobs.get(ticker)
        if job is None:
            return {
                'ok': False,
                'code': 'NO_OPEN_JOB',
                'message': f'no open listing job for {ticker}',
            }

        mode = str(job.get('mode') or '').lower()
        if mode != 'live':
            # dry-run 이면 상태만 정리
            self.state.open_jobs.pop(ticker, None)
            await self._append_jobs_record({
                'ts': int(time.time()),
                'trade_type': 'listing_short_close',
                'mode': 'dry_run',
                'ticker': ticker,
                'reason': reason,
            })
            return {'ok': True, 'mode': 'dry_run', 'ticker': ticker}

        if self.hedge is None:
            return {'ok': False, 'code': 'UNAVAILABLE', 'message': 'hedge_service unavailable'}

        futures_exchange = str(job.get('futures_exchange') or '').lower()
        symbol = str(job.get('symbol') or '')
        qty = float(job.get('entry_qty') or 0.0)
        entry_avg = float(job.get('entry_avg') or 0.0)
        if not futures_exchange or not symbol or qty <= 0:
            return {
                'ok': False,
                'code': 'JOB_CORRUPT',
                'message': f'job fields missing: {job!r}',
            }

        instance = exchange_manager.get_instance(futures_exchange, 'swap')
        if instance is None:
            return {
                'ok': False,
                'code': 'EXCHANGE_INSTANCE_UNAVAILABLE',
                'message': f'{futures_exchange} swap instance unavailable',
            }
        try:
            if not instance.markets:
                await instance.load_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning('[listing-close] load_markets %s: %s', futures_exchange, exc)

        try:
            # SHORT 청산 = BUY (reduceOnly=True)
            result = await self.hedge._submit_futures_close_generic_reduce_only(  # type: ignore[attr-defined]
                futures_instance=instance,
                exchange_name=futures_exchange,
                symbol=symbol,
                side='buy',
                amount=qty,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('[listing-close] %s close err: %s', ticker, exc)
            return {'ok': False, 'code': 'CLOSE_EXCEPTION', 'message': str(exc)}

        filled_qty = float(result.get('filled_qty') or 0.0)
        avg_close = result.get('avg_price')
        status = str(result.get('status') or '').lower()
        err = result.get('error')
        ok = filled_qty > 0 and status in {'closed', 'filled', 'ok'} and not err

        pnl_usd: Optional[float] = None
        try:
            if avg_close and entry_avg and filled_qty > 0:
                # SHORT: pnl ≈ (entry - close) * qty
                pnl_usd = round((entry_avg - float(avg_close)) * filled_qty, 6)
        except Exception:  # noqa: BLE001
            pnl_usd = None

        await self._append_jobs_record({
            'ts': int(time.time()),
            'trade_type': 'listing_short_close',
            'mode': 'live',
            'ticker': ticker,
            'futures_exchange': futures_exchange,
            'symbol': symbol,
            'close_qty': filled_qty,
            'close_avg': avg_close,
            'close_status': status,
            'close_error': err,
            'entry_avg': entry_avg,
            'entry_qty': qty,
            'pnl_usd': pnl_usd,
            'reason': reason,
        })

        if ok:
            self.state.open_jobs.pop(ticker, None)
            await self._send_telegram(
                f'✅ 상장 숏 청산: {ticker} close@{avg_close} (entry@{entry_avg}) '
                f'pnl=${pnl_usd} reason={reason}'
            )
        else:
            await self._telegram_error(
                ticker, futures_exchange,
                f'close failed status={status} filled={filled_qty} err={err}'
            )

        return {
            'ok': ok,
            'ticker': ticker,
            'futures_exchange': futures_exchange,
            'close_qty': filled_qty,
            'close_avg': avg_close,
            'entry_avg': entry_avg,
            'pnl_usd': pnl_usd,
            'status': status,
            'error': err,
        }

    # ------------------------------------------------------------------
    # 유틸
    # ------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self.state.daily_reset_epoch:
            logger.info(
                '[listing-exec] daily rollover: spent=$%.2f reset',
                self.state.daily_spent_usd,
            )
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    @staticmethod
    def _normalize_amount(
        instance: 'ccxt.Exchange',
        symbol: str,
        amount: float,
    ) -> float:
        """ccxt amount_to_precision 사용 — 실패 시 raw 반환."""
        try:
            if hasattr(instance, 'amount_to_precision'):
                precise = instance.amount_to_precision(symbol, amount)
                return float(precise)
        except Exception:  # noqa: BLE001
            pass
        return float(amount)

    async def _append_jobs_record(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'ListingExecutor jobs append failed (%s): %s', path, exc
                )

    async def _telegram_open_ok(
        self,
        ticker: str,
        futures_exchange: str,
        avg_price: Any,
        leverage: int,
        qty: float,
        notional: float,
    ) -> None:
        if self.telegram is None:
            return
        price_str = f'{float(avg_price):.8f}' if avg_price else '?'
        text = (
            f'🩳 상장 숏 진입: {ticker} @{price_str} x{leverage} via {futures_exchange}\n'
            f'qty={qty:.8f} notional=${notional:.0f}'
        )
        await self._send_telegram(text)

    async def _telegram_error(
        self, ticker: str, futures_exchange: str, msg: str
    ) -> None:
        if self.telegram is None:
            return
        text = f'⚠️ 상장 숏 실패: {ticker} via {futures_exchange}\n{msg}'
        await self._send_telegram(text)

    async def _send_telegram(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is None:
                send = getattr(self.telegram, 'send_message', None)
            if send is None:
                logger.debug('ListingExecutor telegram has no send method')
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('ListingExecutor telegram err: %s', exc)
