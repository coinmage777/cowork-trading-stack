"""Cross-Listing Follow Long — 견상(옆상) 선제 진입 전략 (Pika_Kim #5).

배경:
    Bithumb 단독 상장 공지가 먼저 터지면, 그 뒤 수시간~수일 내 Upbit 도 같은
    티커를 "견상(follow-listing)" 할 때가 있다. Upbit 공지가 터지는 순간
    현물이 폭발적으로 펌프되므로, Binance perp 에서 미리 롱을 잡아두면
    Upbit 공지 터질 때 고점 익절이 가능하다.

    과거 사례 (Pika msg 181, 204, 249):
        - BREV : Bithumb 단독 → Upbit 견상 → perp 롱 큰 수익
        - SENT : 동일 패턴

동작:
    1. listing_detector.add_listener() 로 구독
    2. event['exchange'] == 'bithumb' 이고 Upbit KRW 마켓에 아직 없는 티커만 필터
    3. follow-listing 확률 점수 계산 (Phase 1: hard-coded heuristics; Phase 2+: ML)
        - ticker 길이 3-5: +0.1
        - Binance futures 존재: +0.2
        - CoinGecko market cap > $50M: +0.3
        - 24h 거래대금 > $10M: +0.2
        - Binance 핫월렛 자산 잔고 < $1M (출금 제한 신호): +0.1
        - Binance alpha / pre-market 존재: +0.1
    4. score >= CROSS_LONG_MIN_SCORE (기본 0.5) 이면 후보 확정
    5. CROSS_LONG_OBSERVE_SEC (기본 10분) 동안 Bithumb 가격 관찰
        - 10분 내 +20% 이상 펌프면 'too late' — skip
        - 그 외에는 Binance perp 시장가 롱 오픈 (notional $100, x5)
    6. 백그라운드 모니터:
        - Upbit 같은 티커 공지 감지 → win, 시장가 익절
        - 6시간 내 공지 없으면 → timeout, 손실 컷
        - 진입가 대비 -10% → stop loss

트리플 락 (실자금):
    CROSS_LONG_ENABLED=true
    AND CROSS_LONG_DRY_RUN=false
    AND CROSS_LONG_LIVE_CONFIRM=true
추가: kill switch (data/KILL_CROSS_LONG), per-ticker single-fire(24h), daily cap $300, max open 3.
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

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# env 헬퍼 (oracle_divergence_short / bithumb_follower 동일 컨벤션)
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
# 저장 경로
# ----------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
try:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass

_DEFAULT_JOBS_PATH = str(_DATA_DIR / 'cross_listing_jobs.jsonl')
_DEFAULT_KILL_FILE = 'data/KILL_CROSS_LONG'


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class CrossListingLongConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False

    min_score: float = 0.5
    notional_usd: float = 100.0
    leverage: int = 5
    observe_sec: int = 600                # 10분 관망
    stop_loss_pct: float = -10.0          # 진입 대비 -10%
    max_pump_before_entry_pct: float = 20.0  # 관망 중 Bithumb +20% 이상이면 skip
    timeout_hours: float = 6.0
    daily_cap_usd: float = 300.0
    max_open: int = 3
    per_ticker_cooldown_hours: float = 24.0

    # 헤뤼스틱 임계값
    min_market_cap_usd: float = 50_000_000.0
    min_volume_24h_usd: float = 10_000_000.0
    hot_wallet_low_threshold_usd: float = 1_000_000.0

    # 네트워크 타임아웃
    http_timeout_sec: float = 8.0

    jobs_path: str = _DEFAULT_JOBS_PATH
    kill_switch_file: str = _DEFAULT_KILL_FILE

    # Upbit KRW 마켓 캐시 TTL (300초 = 5분)
    upbit_market_cache_ttl_sec: int = 300

    @classmethod
    def load(cls) -> 'CrossListingLongConfig':
        return cls(
            enabled=_env_bool('CROSS_LONG_ENABLED', True),
            dry_run=_env_bool('CROSS_LONG_DRY_RUN', True),
            live_confirm=_env_bool('CROSS_LONG_LIVE_CONFIRM', False),
            min_score=_env_float('CROSS_LONG_MIN_SCORE', 0.5),
            notional_usd=max(_env_float('CROSS_LONG_NOTIONAL_USD', 100.0), 0.0),
            leverage=max(_env_int('CROSS_LONG_LEVERAGE', 5), 1),
            observe_sec=max(_env_int('CROSS_LONG_OBSERVE_SEC', 600), 0),
            stop_loss_pct=_env_float('CROSS_LONG_STOP_LOSS_PCT', -10.0),
            max_pump_before_entry_pct=_env_float(
                'CROSS_LONG_MAX_PUMP_BEFORE_ENTRY_PCT', 20.0,
            ),
            timeout_hours=max(_env_float('CROSS_LONG_TIMEOUT_HOURS', 6.0), 0.1),
            daily_cap_usd=max(_env_float('CROSS_LONG_DAILY_CAP_USD', 300.0), 0.0),
            max_open=max(_env_int('CROSS_LONG_MAX_OPEN', 3), 1),
            per_ticker_cooldown_hours=max(
                _env_float('CROSS_LONG_PER_TICKER_COOLDOWN_HOURS', 24.0), 0.0,
            ),
            min_market_cap_usd=_env_float('CROSS_LONG_MIN_MARKET_CAP_USD', 50_000_000.0),
            min_volume_24h_usd=_env_float('CROSS_LONG_MIN_VOLUME_24H_USD', 10_000_000.0),
            hot_wallet_low_threshold_usd=_env_float(
                'CROSS_LONG_HOT_WALLET_LOW_USD', 1_000_000.0,
            ),
            http_timeout_sec=max(_env_float('CROSS_LONG_HTTP_TIMEOUT_SEC', 8.0), 1.0),
            jobs_path=_env('CROSS_LONG_JOBS_PATH', _DEFAULT_JOBS_PATH),
            kill_switch_file=_env('CROSS_LONG_KILL_SWITCH_FILE', _DEFAULT_KILL_FILE),
            upbit_market_cache_ttl_sec=max(
                _env_int('CROSS_LONG_UPBIT_CACHE_TTL_SEC', 300), 30,
            ),
        )

    @property
    def live_armed(self) -> bool:
        return self.enabled and (not self.dry_run) and self.live_confirm


# ----------------------------------------------------------------------
# Job 모델
# ----------------------------------------------------------------------


@dataclass
class CrossListingJob:
    """견상 long job lifecycle.

    상태 전이:
        CANDIDATE → OBSERVING → ENTERED → CLOSED_{WIN|STOP|TIMEOUT|SKIP|ERR}
    """

    job_id: str
    ticker: str
    mode: str                              # 'live' | 'dry_run'
    source_notice_id: str
    source_notice_url: str
    detected_ts: int

    # 점수
    score: float
    score_breakdown: dict[str, float]
    # 보조 메타 (CoinGecko / Binance)
    has_binance_perp: bool = False
    has_binance_alpha: bool = False
    market_cap_usd: float = 0.0
    volume_24h_usd: float = 0.0
    hot_wallet_balance_usd: Optional[float] = None

    # 관망 구간
    observe_start_ts: int = 0
    observe_start_bithumb_price: Optional[float] = None
    observe_end_ts: int = 0

    # 진입
    entered: bool = False
    entry_ts: int = 0
    entry_price_usd: Optional[float] = None
    entry_qty: float = 0.0
    binance_symbol: str = ''
    binance_order_id: Optional[str] = None

    # 종료
    closed: bool = False
    closed_ts: int = 0
    close_reason: str = ''
    close_price_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None

    status: str = 'CANDIDATE'
    warnings: list[str] = field(default_factory=list)
    last_updated: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'ticker': self.ticker,
            'mode': self.mode,
            'source_notice_id': self.source_notice_id,
            'source_notice_url': self.source_notice_url,
            'detected_ts': self.detected_ts,
            'score': round(self.score, 4),
            'score_breakdown': {k: round(v, 4) for k, v in self.score_breakdown.items()},
            'has_binance_perp': self.has_binance_perp,
            'has_binance_alpha': self.has_binance_alpha,
            'market_cap_usd': self.market_cap_usd,
            'volume_24h_usd': self.volume_24h_usd,
            'hot_wallet_balance_usd': self.hot_wallet_balance_usd,
            'observe_start_ts': self.observe_start_ts,
            'observe_start_bithumb_price': self.observe_start_bithumb_price,
            'observe_end_ts': self.observe_end_ts,
            'entered': self.entered,
            'entry_ts': self.entry_ts,
            'entry_price_usd': self.entry_price_usd,
            'entry_qty': self.entry_qty,
            'binance_symbol': self.binance_symbol,
            'binance_order_id': self.binance_order_id,
            'closed': self.closed,
            'closed_ts': self.closed_ts,
            'close_reason': self.close_reason,
            'close_price_usd': self.close_price_usd,
            'pnl_usd': self.pnl_usd,
            'pnl_pct': self.pnl_pct,
            'status': self.status,
            'warnings': list(self.warnings),
            'last_updated': self.last_updated,
        }


# ----------------------------------------------------------------------
# 헬퍼
# ----------------------------------------------------------------------


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ----------------------------------------------------------------------
# 메인 서비스
# ----------------------------------------------------------------------


class CrossListingLong:
    """Bithumb 단독 상장 → Upbit 견상 가능성 heuristic 롱 서비스.

    사용::

        service = CrossListingLong(
            listing_detector=listing_detector,
            hedge_service=hedge_trade_service,
            poller=poller,
            telegram_service=tg,
        )
        await service.start()
        ...
        await service.stop()
    """

    def __init__(
        self,
        listing_detector: Any = None,
        hedge_service: Any = None,
        poller: Any = None,
        telegram_service: Any = None,
        cfg: Optional[CrossListingLongConfig] = None,
    ) -> None:
        self.cfg = cfg or CrossListingLongConfig.load()
        self.listing_detector = listing_detector
        self.hedge_service = hedge_service
        self.poller = poller
        self.telegram = telegram_service

        # lifecycle
        self._running: bool = False
        self._http: Any = None           # aiohttp session (lazy)
        self._curl: Any = None           # curl_cffi AsyncSession (Upbit/Bithumb 공통)

        # Upbit KRW 마켓 캐시
        self._upbit_markets_cache: set[str] = set()
        self._upbit_markets_cache_ts: float = 0.0

        # Job store
        self._jobs: dict[str, CrossListingJob] = {}
        self._fired_tickers_24h: dict[str, float] = {}  # ticker -> last_fire_ts
        self._inflight_tickers: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

        # daily cap
        self._daily_spent_usd: float = 0.0
        self._daily_reset_epoch: float = _today_midnight_epoch()

        # stats
        self._total_events: int = 0
        self._total_candidates: int = 0
        self._total_skipped: int = 0
        self._total_entries: int = 0
        self._total_wins: int = 0
        self._total_stops: int = 0
        self._total_timeouts: int = 0
        self._total_errors: int = 0
        self._last_error: str = ''

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if self.listing_detector is None:
            logger.warning('[cross-long] listing_detector is None; not starting')
            return
        self._running = True

        # lazy HTTP clients
        try:
            import aiohttp  # type: ignore
            timeout = aiohttp.ClientTimeout(total=self.cfg.http_timeout_sec)
            self._http = aiohttp.ClientSession(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[cross-long] aiohttp unavailable: %s', exc)
            self._http = None

        try:
            from curl_cffi.requests import AsyncSession as CurlAsyncSession  # type: ignore
            self._curl = CurlAsyncSession(
                impersonate='chrome124', timeout=self.cfg.http_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('[cross-long] curl_cffi unavailable: %s', exc)
            self._curl = None

        # listener 등록
        add_listener = getattr(self.listing_detector, 'add_listener', None)
        if callable(add_listener):
            add_listener(self._on_listing_event)
        else:
            logger.warning(
                '[cross-long] listing_detector has no add_listener; stays idle',
            )

        logger.info(
            '[cross-long] started | enabled=%s dry_run=%s live_confirm=%s '
            'min_score=%.2f notional=$%.0f lev=%dx observe=%ds max_pump=%.1f%% '
            'timeout=%.1fh SL=%.1f%% daily_cap=$%.0f max_open=%d',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.min_score, self.cfg.notional_usd, self.cfg.leverage,
            self.cfg.observe_sec, self.cfg.max_pump_before_entry_pct,
            self.cfg.timeout_hours, self.cfg.stop_loss_pct,
            self.cfg.daily_cap_usd, self.cfg.max_open,
        )

    async def stop(self) -> None:
        self._running = False

        # listener 제거 (중복 등록 방지)
        remove_listener = getattr(self.listing_detector, 'remove_listener', None)
        if callable(remove_listener):
            try:
                remove_listener(self._on_listing_event)
            except Exception:  # noqa: BLE001
                pass

        tasks = list(self._active_tasks.values())
        self._active_tasks.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        if self._http is not None:
            try:
                await self._http.close()
            except Exception:  # noqa: BLE001
                pass
            self._http = None

        if self._curl is not None:
            try:
                await self._curl.close()
            except Exception:  # noqa: BLE001
                pass
            self._curl = None

        logger.info('[cross-long] stopped')

    # ------------------------------------------------------------------
    # listener — in-process 팬아웃
    # ------------------------------------------------------------------

    async def _on_listing_event(self, event: dict[str, Any]) -> None:
        """listing_detector 에서 호출되는 비동기 콜백."""
        if not self._running or not self.cfg.enabled:
            return
        try:
            self._total_events += 1
            self._maybe_rollover_daily()

            exchange = str(event.get('exchange') or '').strip().lower()
            ticker = str(event.get('ticker') or '').strip().upper()
            notice_id = str(event.get('id') or '').strip()
            notice_url = str(event.get('url') or '').strip()

            if not ticker:
                return

            # 1) Upbit 이벤트 — 같은 티커의 open job 이 있으면 WIN 클로즈 트리거
            if exchange == 'upbit':
                await self._maybe_close_on_upbit_listing(ticker, notice_url)
                return

            # 2) Bithumb 외는 무시
            if exchange != 'bithumb':
                return

            # per-ticker 24h cooldown
            if self._in_cooldown(ticker):
                logger.debug('[cross-long] %s in cooldown, skip', ticker)
                return

            # 중복 실행 방지
            if ticker in self._inflight_tickers or ticker in self._active_tasks:
                logger.debug('[cross-long] %s already inflight, skip', ticker)
                return

            # Upbit 에 이미 있으면 견상이 아님 — skip
            if await self._is_upbit_listed(ticker):
                logger.debug('[cross-long] %s already on upbit, skip', ticker)
                self._total_skipped += 1
                return

            self._inflight_tickers.add(ticker)
            task = asyncio.create_task(
                self._handle_bithumb_candidate(event, ticker, notice_id, notice_url),
                name=f'cross_long_{ticker}',
            )
            self._active_tasks[ticker] = task

            def _cleanup(t: asyncio.Task[Any], tkr: str = ticker) -> None:
                self._active_tasks.pop(tkr, None)
                self._inflight_tickers.discard(tkr)
                # 태스크 예외가 있으면 로그로 수거
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        logger.error(
                            '[cross-long] task %s finished with exc: %s', tkr, exc,
                        )
            task.add_done_callback(_cleanup)

        except Exception as exc:  # noqa: BLE001
            self._total_errors += 1
            self._last_error = f'{type(exc).__name__}: {exc}'
            logger.exception('[cross-long] on_event err: %s', exc)

    # ------------------------------------------------------------------
    # 후보 처리 — score → observe → entry → monitor
    # ------------------------------------------------------------------

    async def _handle_bithumb_candidate(
        self,
        event: dict[str, Any],
        ticker: str,
        notice_id: str,
        notice_url: str,
    ) -> None:
        """Bithumb 단독 공지 이벤트 단일 티커 처리. (async task)"""
        # score
        score, breakdown, meta = await self._upbit_follow_probability_score(event, ticker)
        self._total_candidates += 1

        job = CrossListingJob(
            job_id=f'clong_{uuid.uuid4().hex[:10]}',
            ticker=ticker,
            mode='live' if self.cfg.live_armed else 'dry_run',
            source_notice_id=notice_id,
            source_notice_url=notice_url,
            detected_ts=int(event.get('ts') or time.time()),
            score=score,
            score_breakdown=breakdown,
            has_binance_perp=bool(meta.get('has_binance_perp')),
            has_binance_alpha=bool(meta.get('has_binance_alpha')),
            market_cap_usd=float(meta.get('market_cap_usd') or 0.0),
            volume_24h_usd=float(meta.get('volume_24h_usd') or 0.0),
            hot_wallet_balance_usd=meta.get('hot_wallet_balance_usd'),
            status='CANDIDATE',
            last_updated=int(time.time()),
        )
        async with self._lock:
            self._jobs[job.job_id] = job
        await self._append_jsonl(job.to_json())

        # score 임계 미달 — 기록만 하고 종료
        if score < self.cfg.min_score:
            logger.info(
                '[cross-long] %s score=%.3f < %.3f, skip (breakdown=%s)',
                ticker, score, self.cfg.min_score, breakdown,
            )
            job.status = 'SKIPPED_SCORE'
            job.closed = True
            job.closed_ts = int(time.time())
            job.close_reason = f'score_below_threshold({score:.3f}<{self.cfg.min_score:.3f})'
            job.last_updated = job.closed_ts
            await self._append_jsonl(job.to_json())
            self._total_skipped += 1
            await self._send_telegram(
                f'ℹ️ [cross-long] {ticker} score={score:.2f} < {self.cfg.min_score:.2f}\n'
                f'  breakdown={breakdown}',
                alert_key='cross_long_block',
            )
            return

        # 게이트 통과했으니 관망 시작
        # 사전 게이트 체크
        ok, reason = self._can_enter(self.cfg.notional_usd)
        if not ok:
            logger.info(
                '[cross-long] %s gate blocked: %s (score=%.3f)',
                ticker, reason, score,
            )
            job.status = 'SKIPPED_GATE'
            job.closed = True
            job.closed_ts = int(time.time())
            job.close_reason = f'gate:{reason}'
            job.last_updated = job.closed_ts
            await self._append_jsonl(job.to_json())
            self._total_skipped += 1
            await self._send_telegram(
                f'ℹ️ [cross-long] {ticker} BLOCKED: {reason}',
                alert_key='cross_long_block',
            )
            return

        await self._send_telegram(
            f'🔎 [cross-long] 견상 후보 감지\n'
            f'  {ticker} score={score:.2f} (min={self.cfg.min_score:.2f})\n'
            f'  source={notice_url or notice_id}\n'
            f'  observe={self.cfg.observe_sec}s (Bithumb pump <{self.cfg.max_pump_before_entry_pct:.0f}% 대기)'
        )

        # 관망 시작
        job.status = 'OBSERVING'
        job.observe_start_ts = int(time.time())
        job.observe_start_bithumb_price = await self._fetch_bithumb_last_price(ticker)
        job.last_updated = job.observe_start_ts
        await self._append_jsonl(job.to_json())

        # 관망 후 진입 판단
        await self._observe_then_enter(job)

    async def _observe_then_enter(self, job: CrossListingJob) -> None:
        """관망 창 동안 Bithumb 가격 추적. 급등 시 skip, 아니면 진입."""
        start_price = job.observe_start_bithumb_price
        end_ts = job.observe_start_ts + self.cfg.observe_sec

        # observe 중 중간 샘플링 — 5회 정도
        samples = max(5, self.cfg.observe_sec // 60)
        interval = max(1.0, self.cfg.observe_sec / samples)

        late_pump_detected = False
        while time.time() < end_ts and self._running:
            # Upbit 공지가 관망 중 터지면 즉시 진입 (기회 놓치기 싫음)
            if await self._is_upbit_listed(job.ticker):
                logger.info(
                    '[cross-long] %s appeared on upbit during observe — entering immediately',
                    job.ticker,
                )
                break
            try:
                current = await self._fetch_bithumb_last_price(job.ticker)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[cross-long] observe price fetch err %s: %s', job.ticker, exc)
                current = None
            if current and start_price and start_price > 0:
                pump_pct = (current - start_price) / start_price * 100.0
                if pump_pct >= self.cfg.max_pump_before_entry_pct:
                    late_pump_detected = True
                    logger.info(
                        '[cross-long] %s pump %.2f%% >= %.1f%%, too late, skip',
                        job.ticker, pump_pct, self.cfg.max_pump_before_entry_pct,
                    )
                    break
            await asyncio.sleep(interval)

        job.observe_end_ts = int(time.time())
        if late_pump_detected:
            job.status = 'SKIPPED_PUMP'
            job.closed = True
            job.closed_ts = job.observe_end_ts
            job.close_reason = 'bithumb_pump_above_threshold'
            job.last_updated = job.closed_ts
            await self._append_jsonl(job.to_json())
            self._total_skipped += 1
            self._fired_tickers_24h[job.ticker] = time.time()
            await self._send_telegram(
                f'🟠 [cross-long] {job.ticker} skip: Bithumb pump > '
                f'{self.cfg.max_pump_before_entry_pct:.0f}%'
            )
            return

        # 진입 실행
        await self._enter_and_monitor(job)

    async def _enter_and_monitor(self, job: CrossListingJob) -> None:
        """Binance perp 롱 진입 → 백그라운드 exit 모니터."""
        # 재검증 — 사용자가 중간에 disable 했을 수도 있음
        ok, reason = self._can_enter(self.cfg.notional_usd)
        if not ok:
            job.status = 'SKIPPED_GATE'
            job.closed = True
            job.closed_ts = int(time.time())
            job.close_reason = f'gate_at_entry:{reason}'
            job.last_updated = job.closed_ts
            await self._append_jsonl(job.to_json())
            self._total_skipped += 1
            return

        if not self.cfg.live_armed:
            await self._record_dry_entry(job)
            await self._monitor_loop(job)
            return

        # LIVE 경로 — Binance perp 시장가 롱
        success = await self._execute_live_long(job)
        if not success:
            # execute_live_long 이 job 상태를 이미 기록
            return

        self._fired_tickers_24h[job.ticker] = time.time()
        self._daily_spent_usd += self.cfg.notional_usd
        self._total_entries += 1
        await self._monitor_loop(job)

    # ------------------------------------------------------------------
    # Dry-run 진입
    # ------------------------------------------------------------------

    async def _record_dry_entry(self, job: CrossListingJob) -> None:
        mark = await self._fetch_binance_mark_price(job.ticker)
        entry_px = mark or 0.0
        qty = (self.cfg.notional_usd / entry_px) if entry_px > 0 else 0.0
        job.entered = True
        job.entry_ts = int(time.time())
        job.entry_price_usd = entry_px if entry_px > 0 else None
        job.entry_qty = qty
        job.binance_symbol = f'{job.ticker}/USDT:USDT'
        job.status = 'ENTERED'
        job.warnings.append('dry_run')
        job.last_updated = job.entry_ts
        self._fired_tickers_24h[job.ticker] = time.time()
        self._daily_spent_usd += self.cfg.notional_usd
        await self._append_jsonl(job.to_json())
        logger.info(
            '[DRY-cross-long] would LONG %s @%.6f qty=%.6f $%.0f x%d score=%.2f',
            job.ticker, entry_px, qty, self.cfg.notional_usd, self.cfg.leverage, job.score,
        )
        await self._send_telegram(
            f'🧪 [DRY] cross-long 진입\n'
            f'  {job.ticker} @{entry_px:.6f} qty={qty:.4f}\n'
            f'  ${self.cfg.notional_usd:.0f} x{self.cfg.leverage} score={job.score:.2f}',
            alert_key='cross_long_dry',
        )

    # ------------------------------------------------------------------
    # Live 진입 — Binance perp 시장가 롱
    # ------------------------------------------------------------------

    async def _execute_live_long(self, job: CrossListingJob) -> bool:
        if self.hedge_service is None:
            msg = 'hedge_service is None'
            logger.error('[cross-long] %s abort: %s', job.ticker, msg)
            await self._close_job_err(job, msg)
            return False

        try:
            from backend.exchanges import manager as exchange_manager  # type: ignore
        except Exception as exc:  # noqa: BLE001
            await self._close_job_err(job, f'exchange_manager import: {exc}')
            return False

        instance = exchange_manager.get_instance('binance', 'swap')
        if instance is None:
            await self._close_job_err(job, 'binance swap instance unavailable')
            return False

        try:
            if not getattr(instance, 'markets', None):
                await instance.load_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning('[cross-long] %s load_markets: %s', job.ticker, exc)

        try:
            symbol = exchange_manager.get_symbol(
                ticker=job.ticker, market_type='swap', exchange_id='binance',
            )
        except Exception:  # noqa: BLE001
            symbol = f'{job.ticker}/USDT:USDT'

        # 레퍼런스 가격 (best ask 기반 qty 산정)
        reference_price: Optional[float] = None
        try:
            bbo = await exchange_manager.fetch_bbo(instance, symbol)
            if bbo is not None:
                # LONG 시장가 매수 → ask 에 체결 → qty 계산 시 ask 사용이 안전
                reference_price = float(bbo.ask) if bbo.ask else (
                    float(bbo.bid) if bbo.bid else None
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning('[cross-long] %s fetch_bbo: %s', job.ticker, exc)

        if reference_price is None or reference_price <= 0:
            await self._close_job_err(job, 'binance reference price unavailable')
            return False

        raw_qty = self.cfg.notional_usd / reference_price
        try:
            if hasattr(instance, 'amount_to_precision'):
                qty = float(instance.amount_to_precision(symbol, raw_qty))
            else:
                qty = raw_qty
        except Exception:  # noqa: BLE001
            qty = raw_qty

        if qty <= 0:
            await self._close_job_err(job, f'normalized qty<=0 ref={reference_price}')
            return False

        # 레버리지 설정 (best-effort)
        warnings: list[str] = []
        try:
            prepare = getattr(self.hedge_service, '_prepare_futures_account', None)
            if callable(prepare):
                warnings = await prepare(
                    exchange_instance=instance,
                    symbol=symbol,
                    leverage=self.cfg.leverage,
                ) or []
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'leverage prep: {exc}')
            logger.warning('[cross-long] %s leverage prep: %s', job.ticker, exc)

        # LONG 시장가 오픈
        try:
            submit = getattr(self.hedge_service, '_submit_market_order', None)
            if not callable(submit):
                raise RuntimeError('hedge_service._submit_market_order unavailable')
            order_result = await submit(
                exchange_instance=instance,
                exchange_name='binance',
                symbol=symbol,
                side='buy',
                amount=qty,
                market='futures',
                reference_price=reference_price,
            )
        except Exception as exc:  # noqa: BLE001
            await self._close_job_err(job, f'submit_market_order exc: {exc}')
            return False

        filled_qty = float(order_result.get('filled_qty') or 0.0)
        avg_price = order_result.get('avg_price')
        status = str(order_result.get('status') or '').lower()
        err = order_result.get('error')

        if filled_qty <= 0 or err or status not in {'closed', 'filled', 'ok'}:
            msg = f'order failed status={status} filled={filled_qty} err={err}'
            await self._close_job_err(job, msg)
            return False

        job.entered = True
        job.entry_ts = int(time.time())
        job.entry_price_usd = float(avg_price) if avg_price else reference_price
        job.entry_qty = filled_qty
        job.binance_symbol = symbol
        job.binance_order_id = str(order_result.get('order_id') or '') or None
        job.status = 'ENTERED'
        job.warnings.extend(warnings)
        job.last_updated = job.entry_ts
        await self._append_jsonl(job.to_json())

        logger.info(
            '[cross-long] LIVE LONG %s @%.6f qty=%.6f $%.0f x%d score=%.2f',
            job.ticker, job.entry_price_usd or 0.0, filled_qty,
            self.cfg.notional_usd, self.cfg.leverage, job.score,
        )
        entry_px_fmt = (
            f'{job.entry_price_usd:.6f}' if job.entry_price_usd else '?'
        )
        await self._send_telegram(
            f'🟢 cross-long 진입\n'
            f'  {job.ticker} @{entry_px_fmt}\n'
            f'  qty={filled_qty:.4f} ${self.cfg.notional_usd:.0f} x{self.cfg.leverage}\n'
            f'  score={job.score:.2f} mode=LIVE'
        )
        return True

    async def _close_job_err(self, job: CrossListingJob, msg: str) -> None:
        logger.error('[cross-long] %s err: %s', job.ticker, msg)
        self._total_errors += 1
        self._last_error = msg
        job.warnings.append(msg)
        job.status = 'CLOSED_ERR'
        job.closed = True
        job.closed_ts = int(time.time())
        job.close_reason = msg[:200]
        job.last_updated = job.closed_ts
        self._fired_tickers_24h[job.ticker] = time.time()
        await self._append_jsonl(job.to_json())
        await self._send_telegram(f'⚠️ cross-long {job.ticker} err: {msg}')

    # ------------------------------------------------------------------
    # Exit monitor — win/stop/timeout
    # ------------------------------------------------------------------

    async def _monitor_loop(self, job: CrossListingJob) -> None:
        """진입 후 종료 조건 주기 체크."""
        if not job.entered:
            return
        deadline = job.entry_ts + int(self.cfg.timeout_hours * 3600)
        poll_sec = 10.0

        while self._running and not job.closed:
            now = int(time.time())
            try:
                # 1) Upbit 공지 감지 → win
                if await self._is_upbit_listed(job.ticker):
                    await self._close_job_and_exit(job, 'upbit_listed', 'CLOSED_WIN')
                    return

                # 2) 가격 기반 stop loss
                if job.entry_price_usd and job.entry_price_usd > 0:
                    mark = await self._fetch_binance_mark_price(job.ticker)
                    if mark and mark > 0:
                        pnl_pct = (mark - job.entry_price_usd) / job.entry_price_usd * 100.0
                        if pnl_pct <= self.cfg.stop_loss_pct:
                            await self._close_job_and_exit(
                                job,
                                f'stop_loss_{pnl_pct:.2f}pct',
                                'CLOSED_STOP',
                                close_mark=mark,
                            )
                            return

                # 3) timeout
                if now >= deadline:
                    await self._close_job_and_exit(job, 'timeout', 'CLOSED_TIMEOUT')
                    return

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('[cross-long] %s monitor loop err: %s', job.ticker, exc)

            await asyncio.sleep(poll_sec)

    async def _close_job_and_exit(
        self,
        job: CrossListingJob,
        reason: str,
        final_status: str,
        close_mark: Optional[float] = None,
    ) -> None:
        """포지션 청산 + job 상태 갱신 + 이벤트 로깅."""
        if job.closed:
            return
        close_price = close_mark
        pnl_usd: Optional[float] = None

        if job.mode == 'live' and self.hedge_service is not None and job.entered:
            try:
                res = await self._live_close_long(job)
                filled = float((res or {}).get('filled_qty') or 0.0)
                avg = (res or {}).get('avg_price')
                if filled > 0 and avg:
                    close_price = float(avg)
                    if job.entry_price_usd:
                        pnl_usd = round(
                            (float(avg) - job.entry_price_usd) * filled, 6,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception('[cross-long] live close %s err: %s', job.ticker, exc)
                job.warnings.append(f'close_exc: {exc}')

        # 가상 PnL 보완 — dry 경로 또는 위에서 실패한 경우
        if pnl_usd is None and close_price and close_price > 0 \
                and job.entry_price_usd and job.entry_price_usd > 0 and job.entry_qty > 0:
            pnl_usd = round((close_price - job.entry_price_usd) * job.entry_qty, 6)

        pnl_pct: Optional[float] = None
        if pnl_usd is not None and self.cfg.notional_usd > 0:
            pnl_pct = round(pnl_usd / self.cfg.notional_usd * 100.0, 4)

        async with self._lock:
            job.closed = True
            job.closed_ts = int(time.time())
            job.close_reason = reason
            job.close_price_usd = close_price
            job.pnl_usd = pnl_usd
            job.pnl_pct = pnl_pct
            job.status = final_status
            job.last_updated = job.closed_ts

        await self._append_jsonl(job.to_json())
        self._fired_tickers_24h[job.ticker] = time.time()

        if final_status == 'CLOSED_WIN':
            self._total_wins += 1
            emoji = '✅'
        elif final_status == 'CLOSED_STOP':
            self._total_stops += 1
            emoji = '🛑'
        elif final_status == 'CLOSED_TIMEOUT':
            self._total_timeouts += 1
            emoji = '🟠'
        else:
            emoji = 'ℹ️'

        logger.info(
            '[cross-long] CLOSE %s %s reason=%s entry=%s close=%s pnl=%s',
            job.job_id, job.ticker, reason,
            job.entry_price_usd, close_price, pnl_usd,
        )
        await self._send_telegram(
            f'{emoji} cross-long CLOSE {job.ticker}\n'
            f'  reason={reason} status={final_status}\n'
            f'  entry={job.entry_price_usd} close={close_price} pnl={pnl_usd}'
        )

    async def _live_close_long(self, job: CrossListingJob) -> dict[str, Any]:
        """Binance perp LONG 청산 = SELL reduceOnly."""
        try:
            from backend.exchanges import manager as exchange_manager  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f'exchange_manager import: {exc}') from exc

        instance = exchange_manager.get_instance('binance', 'swap')
        if instance is None:
            raise RuntimeError('binance swap instance unavailable')
        try:
            if not getattr(instance, 'markets', None):
                await instance.load_markets()
        except Exception:  # noqa: BLE001
            pass

        symbol = job.binance_symbol or f'{job.ticker}/USDT:USDT'
        submit_close = getattr(
            self.hedge_service, '_submit_futures_close_generic_reduce_only', None,
        )
        if not callable(submit_close):
            raise RuntimeError(
                'hedge_service._submit_futures_close_generic_reduce_only unavailable'
            )
        return await submit_close(
            futures_instance=instance,
            exchange_name='binance',
            symbol=symbol,
            side='sell',
            amount=job.entry_qty,
        )

    async def _maybe_close_on_upbit_listing(
        self, ticker: str, notice_url: str,
    ) -> None:
        """Upbit 공지 팬아웃 — 동일 티커 open job 있으면 즉시 WIN 클로즈."""
        async with self._lock:
            targets = [
                j for j in self._jobs.values()
                if j.ticker == ticker and j.entered and not j.closed
            ]
        for job in targets:
            logger.info(
                '[cross-long] upbit listing detected for %s — closing job %s',
                ticker, job.job_id,
            )
            await self._close_job_and_exit(
                job, reason=f'upbit_notice:{notice_url[:120]}', final_status='CLOSED_WIN',
            )

    # ------------------------------------------------------------------
    # Heuristics — follow-listing score
    # ------------------------------------------------------------------

    async def upbit_follow_probability_score(
        self, ticker: str, event: Optional[dict[str, Any]] = None,
    ) -> float:
        """외부에서도 호출 가능한 얇은 래퍼. 점수만 반환."""
        score, _, _ = await self._upbit_follow_probability_score(event or {}, ticker)
        return score

    async def _upbit_follow_probability_score(
        self, event: dict[str, Any], ticker: str,
    ) -> tuple[float, dict[str, float], dict[str, Any]]:
        """점수 0-1 + 구성 요소 + 수집된 메타데이터 반환."""
        breakdown: dict[str, float] = {}
        meta: dict[str, Any] = {
            'has_binance_perp': False,
            'has_binance_alpha': False,
            'market_cap_usd': 0.0,
            'volume_24h_usd': 0.0,
            'hot_wallet_balance_usd': None,
        }

        # 1) ticker 길이 3-5
        length_bonus = 0.1 if 3 <= len(ticker) <= 5 else 0.0
        breakdown['ticker_length_3_5'] = length_bonus

        # 2) Binance futures 존재 (listing_detector 이미 조회한 값 사용)
        has_binance_perp = bool(event.get('binance_perp'))
        if not has_binance_perp:
            # event 에 없으면 best-effort 로 재조회 (listing_detector 가 cold start 한 경우)
            has_binance_perp = await self._binance_perp_exists(ticker)
        meta['has_binance_perp'] = has_binance_perp
        binance_perp_bonus = 0.2 if has_binance_perp else 0.0
        breakdown['binance_perp'] = binance_perp_bonus

        # 3) CoinGecko market cap / volume
        cg = await self._coingecko_stats(ticker)
        market_cap = float(cg.get('market_cap_usd') or 0.0)
        volume_24h = float(cg.get('volume_24h_usd') or 0.0)
        meta['market_cap_usd'] = market_cap
        meta['volume_24h_usd'] = volume_24h
        mc_bonus = 0.3 if market_cap > self.cfg.min_market_cap_usd else 0.0
        vol_bonus = 0.2 if volume_24h > self.cfg.min_volume_24h_usd else 0.0
        breakdown['market_cap_gt_50m'] = mc_bonus
        breakdown['volume_24h_gt_10m'] = vol_bonus

        # 4) Binance 핫월렛 잔고 < $1M (데이터 부재 시 bonus 0)
        hw_balance = await self._binance_hot_wallet_balance_usd(ticker)
        meta['hot_wallet_balance_usd'] = hw_balance
        hw_bonus = 0.0
        if hw_balance is not None and 0 <= hw_balance < self.cfg.hot_wallet_low_threshold_usd:
            hw_bonus = 0.1
        breakdown['hot_wallet_low'] = hw_bonus

        # 5) Binance alpha / pre-market
        has_alpha = await self._binance_alpha_exists(ticker)
        meta['has_binance_alpha'] = has_alpha
        alpha_bonus = 0.1 if has_alpha else 0.0
        breakdown['binance_alpha'] = alpha_bonus

        total = round(
            length_bonus + binance_perp_bonus + mc_bonus
            + vol_bonus + hw_bonus + alpha_bonus,
            4,
        )
        # cap to [0, 1]
        total = max(0.0, min(1.0, total))
        return total, breakdown, meta

    async def _coingecko_stats(self, ticker: str) -> dict[str, Any]:
        """CoinGecko 에서 market_cap_usd + volume_24h_usd 조회. 실패 시 {}."""
        if self._http is None:
            return {}
        # 1) search → coin id
        try:
            url = f'https://api.coingecko.com/api/v3/search?query={ticker}'
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] coingecko search %s: %s', ticker, exc)
            return {}
        coins = []
        if isinstance(data, dict):
            coins = data.get('coins') or []
        if not coins:
            return {}
        # symbol 정확히 매칭하는 첫번째 항목
        coin_id: Optional[str] = None
        for c in coins:
            if not isinstance(c, dict):
                continue
            sym = str(c.get('symbol') or '').upper()
            if sym == ticker.upper():
                coin_id = str(c.get('id') or '').strip() or None
                if coin_id:
                    break
        if not coin_id:
            return {}
        try:
            url = (
                f'https://api.coingecko.com/api/v3/simple/price'
                f'?ids={coin_id}&vs_currencies=usd'
                f'&include_market_cap=true&include_24hr_vol=true'
            )
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] coingecko price %s: %s', coin_id, exc)
            return {}
        row = (data or {}).get(coin_id) or {}
        try:
            return {
                'market_cap_usd': float(row.get('usd_market_cap') or 0.0),
                'volume_24h_usd': float(row.get('usd_24h_vol') or 0.0),
            }
        except (TypeError, ValueError):
            return {}

    async def _binance_perp_exists(self, ticker: str) -> bool:
        """Binance USDT-M perp 가 존재하는가 (best-effort)."""
        if self._http is None:
            return False
        try:
            url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] binance perp list: %s', exc)
            return False
        symbols = (data or {}).get('symbols') or []
        tgt = f'{ticker.upper()}USDT'
        for s in symbols:
            try:
                if str(s.get('symbol') or '').upper() == tgt:
                    status = str(s.get('status') or '').upper()
                    return status in {'TRADING', ''}
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _binance_alpha_exists(self, ticker: str) -> bool:
        """Binance alpha / pre-market 존재 여부. 공식 API 스펙이 없어 best-effort.

        BINANCE_ALPHA_URL 환경변수로 override 가능. 응답은 JSON 또는 목록 형태.
        미설정 시 False.
        """
        override = _env('BINANCE_ALPHA_URL')
        if not override or self._http is None:
            return False
        url = override.replace('{symbol}', ticker).replace('{SYMBOL}', ticker.upper())
        try:
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return False
                text = await resp.text()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] binance alpha probe: %s', exc)
            return False
        return ticker.upper() in text.upper()

    async def _binance_hot_wallet_balance_usd(
        self, ticker: str,
    ) -> Optional[float]:
        """Binance 핫월렛의 해당 토큰 잔고 USD 추정.

        Phase 1: 공식 API 없음. 환경변수 override 없으면 None 반환 (bonus 0).
        CROSS_LONG_HOT_WALLET_URL 형식: `https://.../balance?token={symbol}` → JSON { usd: ... }
        """
        override = _env('CROSS_LONG_HOT_WALLET_URL')
        if not override or self._http is None:
            return None
        url = override.replace('{symbol}', ticker).replace('{SYMBOL}', ticker.upper())
        try:
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] hot wallet probe: %s', exc)
            return None
        if not isinstance(data, dict):
            return None
        for key in ('usd', 'usd_balance', 'balance_usd', 'balance'):
            v = data.get(key)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------
    # Upbit KRW 마켓 캐시 (5분 TTL)
    # ------------------------------------------------------------------

    async def _is_upbit_listed(self, ticker: str) -> bool:
        await self._refresh_upbit_markets_cache_if_stale()
        return ticker.upper() in self._upbit_markets_cache

    async def _refresh_upbit_markets_cache_if_stale(self) -> None:
        now = time.time()
        if (now - self._upbit_markets_cache_ts) < self.cfg.upbit_market_cache_ttl_sec:
            return
        # 시도: curl_cffi (Cloudflare 대응) → http 실패 시 기존 캐시 유지
        tickers: set[str] = set()
        url = 'https://api.upbit.com/v1/market/all?is_details=false'
        data: Any = None
        if self._curl is not None:
            try:
                resp = await self._curl.get(url)
                if resp.status_code == 200:
                    data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.debug('[cross-long] upbit markets curl err: %s', exc)
        if data is None and self._http is not None:
            try:
                async with self._http.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[cross-long] upbit markets aiohttp err: %s', exc)
        if not isinstance(data, list):
            # 실패 — 캐시 갱신 포기 (오탐 방지를 위해 기존 값 유지)
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            market = str(item.get('market') or '').upper()
            if market.startswith('KRW-'):
                base = market[4:]
                if base:
                    tickers.add(base)
        if tickers:
            self._upbit_markets_cache = tickers
            self._upbit_markets_cache_ts = now

    # ------------------------------------------------------------------
    # 가격 조회 — Bithumb last / Binance mark
    # ------------------------------------------------------------------

    async def _fetch_bithumb_last_price(self, ticker: str) -> Optional[float]:
        """Bithumb public ticker — closing_price (KRW)."""
        if self._curl is None and self._http is None:
            return None
        url = f'https://api.bithumb.com/public/ticker/{ticker}_KRW'
        data: Any = None
        if self._curl is not None:
            try:
                resp = await self._curl.get(url)
                if resp.status_code == 200:
                    data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.debug('[cross-long] bithumb ticker curl %s: %s', ticker, exc)
        if data is None and self._http is not None:
            try:
                async with self._http.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[cross-long] bithumb ticker aio %s: %s', ticker, exc)
        if not isinstance(data, dict):
            return None
        if str(data.get('status') or '') != '0000':
            return None
        body = data.get('data')
        if not isinstance(body, dict):
            return None
        try:
            v = float(body.get('closing_price') or 0.0)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    async def _fetch_binance_mark_price(self, ticker: str) -> Optional[float]:
        """Binance USDT-M perp markPrice."""
        if self._http is None:
            return None
        try:
            url = (
                f'https://fapi.binance.com/fapi/v1/premiumIndex'
                f'?symbol={ticker.upper()}USDT'
            )
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] binance mark %s: %s', ticker, exc)
            return None
        if not isinstance(data, dict):
            return None
        raw = data.get('markPrice') or data.get('indexPrice')
        if raw is None:
            return None
        try:
            v = float(raw)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Gates / 유틸
    # ------------------------------------------------------------------

    def _in_cooldown(self, ticker: str) -> bool:
        last = self._fired_tickers_24h.get(ticker.upper())
        if last is None:
            return False
        return (time.time() - last) < (self.cfg.per_ticker_cooldown_hours * 3600)

    def _can_enter(self, notional_usd: float) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, 'disabled'
        if self._kill_switch_active():
            return False, 'kill_switch_active'
        self._maybe_rollover_daily()
        if self._daily_spent_usd + notional_usd > self.cfg.daily_cap_usd:
            return False, (
                f'daily_cap exceeded (${self._daily_spent_usd:.2f}+${notional_usd:.2f}'
                f'>${self.cfg.daily_cap_usd:.2f})'
            )
        if len(self._open_jobs()) >= self.cfg.max_open:
            return False, f'max_open reached ({self.cfg.max_open})'
        return True, 'ok'

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self._daily_reset_epoch:
            logger.info(
                '[cross-long] daily rollover: spent=$%.2f reset',
                self._daily_spent_usd,
            )
            self._daily_spent_usd = 0.0
            self._daily_reset_epoch = today

    def _open_jobs(self) -> list[CrossListingJob]:
        """현재 돈이 묶여 있는 모든 job (CANDIDATE/OBSERVING/ENTERED)."""
        return [
            j for j in self._jobs.values()
            if j.status in {'CANDIDATE', 'OBSERVING', 'ENTERED'} and not j.closed
        ]

    # ------------------------------------------------------------------
    # 영속화
    # ------------------------------------------------------------------

    async def _append_jsonl(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception as exc:  # noqa: BLE001
            logger.warning('[cross-long] jsonl append err: %s', exc)

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        return {
            'running': self._running,
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': self.cfg.live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'config': {
                'min_score': self.cfg.min_score,
                'notional_usd': self.cfg.notional_usd,
                'leverage': self.cfg.leverage,
                'observe_sec': self.cfg.observe_sec,
                'max_pump_before_entry_pct': self.cfg.max_pump_before_entry_pct,
                'stop_loss_pct': self.cfg.stop_loss_pct,
                'timeout_hours': self.cfg.timeout_hours,
                'daily_cap_usd': self.cfg.daily_cap_usd,
                'max_open': self.cfg.max_open,
                'per_ticker_cooldown_hours': self.cfg.per_ticker_cooldown_hours,
                'jobs_path': self.cfg.jobs_path,
                'kill_switch_file': self.cfg.kill_switch_file,
            },
            'stats': {
                'total_events': self._total_events,
                'total_candidates': self._total_candidates,
                'total_entries': self._total_entries,
                'total_skipped': self._total_skipped,
                'total_wins': self._total_wins,
                'total_stops': self._total_stops,
                'total_timeouts': self._total_timeouts,
                'total_errors': self._total_errors,
                'daily_spent_usd': round(self._daily_spent_usd, 2),
                'fired_tickers_24h': {
                    k: int(v) for k, v in self._fired_tickers_24h.items()
                },
                'last_error': self._last_error,
            },
            'open_jobs_count': len(self._open_jobs()),
            'open_jobs': [j.to_json() for j in self._open_jobs()],
            'upbit_markets_cached_count': len(self._upbit_markets_cache),
            'upbit_markets_cached_age_sec': int(
                time.time() - self._upbit_markets_cache_ts
            ) if self._upbit_markets_cache_ts else None,
        }

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.cfg.jobs_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[cross-long] recent_jobs read: %s', exc)
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

    async def enter_manual(
        self, ticker: str, notional_usd: Optional[float] = None,
    ) -> dict[str, Any]:
        """수동 트리거 — score/observe 무시하고 바로 진입."""
        ticker = str(ticker or '').strip().upper()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
        if self._in_cooldown(ticker):
            return {'ok': False, 'code': 'COOLDOWN',
                    'message': f'{ticker} in 24h cooldown'}
        if ticker in self._inflight_tickers:
            return {'ok': False, 'code': 'INFLIGHT',
                    'message': f'{ticker} already inflight'}

        notional_override: Optional[float] = None
        if notional_usd is not None:
            try:
                notional_override = float(notional_usd)
                if notional_override <= 0:
                    notional_override = None
            except (TypeError, ValueError):
                return {'ok': False, 'code': 'INVALID_INPUT',
                        'message': 'notional_usd must be positive'}

        # 임시 cfg 스왑 (원복 보장)
        prev_notional = self.cfg.notional_usd
        if notional_override is not None:
            self.cfg.notional_usd = notional_override
        try:
            ok, reason = self._can_enter(self.cfg.notional_usd)
            if not ok:
                return {'ok': False, 'code': 'GATE_BLOCKED', 'message': reason}

            # manual job 구성
            score, breakdown, meta = await self._upbit_follow_probability_score(
                {}, ticker,
            )
            job = CrossListingJob(
                job_id=f'clong_{uuid.uuid4().hex[:10]}',
                ticker=ticker,
                mode='live' if self.cfg.live_armed else 'dry_run',
                source_notice_id='manual',
                source_notice_url='manual',
                detected_ts=int(time.time()),
                score=score,
                score_breakdown=breakdown,
                has_binance_perp=bool(meta.get('has_binance_perp')),
                has_binance_alpha=bool(meta.get('has_binance_alpha')),
                market_cap_usd=float(meta.get('market_cap_usd') or 0.0),
                volume_24h_usd=float(meta.get('volume_24h_usd') or 0.0),
                hot_wallet_balance_usd=meta.get('hot_wallet_balance_usd'),
                status='CANDIDATE',
                last_updated=int(time.time()),
                warnings=['manual_trigger'],
            )
            async with self._lock:
                self._jobs[job.job_id] = job
            await self._append_jsonl(job.to_json())

            self._inflight_tickers.add(ticker)
            task = asyncio.create_task(
                self._enter_and_monitor(job),
                name=f'cross_long_manual_{ticker}',
            )
            self._active_tasks[ticker] = task

            def _cleanup(t: asyncio.Task[Any], tkr: str = ticker) -> None:
                self._active_tasks.pop(tkr, None)
                self._inflight_tickers.discard(tkr)
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        logger.error('[cross-long] manual task %s err: %s', tkr, exc)
            task.add_done_callback(_cleanup)

            return {
                'ok': True,
                'job_id': job.job_id,
                'mode': job.mode,
                'score': score,
                'score_breakdown': breakdown,
            }
        finally:
            self.cfg.notional_usd = prev_notional

    async def exit_manual(self, job_id: str, reason: str = 'manual') -> dict[str, Any]:
        job_id = str(job_id or '').strip()
        if not job_id:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {'ok': False, 'code': 'NOT_FOUND', 'message': f'no job {job_id}'}
        if job.closed:
            return {'ok': False, 'code': 'ALREADY_CLOSED',
                    'message': f'status={job.status}'}
        if not job.entered:
            # 관망 / 후보 단계면 task cancel + 상태만 갱신
            task = self._active_tasks.get(job.ticker)
            if task is not None and not task.done():
                task.cancel()
            async with self._lock:
                job.closed = True
                job.closed_ts = int(time.time())
                job.close_reason = f'manual_pre_entry:{reason}'
                job.status = 'CLOSED_SKIP'
                job.last_updated = job.closed_ts
            self._fired_tickers_24h[job.ticker] = time.time()
            await self._append_jsonl(job.to_json())
            return {'ok': True, 'job': job.to_json()}
        # ENTERED 단계 — 시장가 청산
        mark = await self._fetch_binance_mark_price(job.ticker)
        await self._close_job_and_exit(
            job, reason=f'manual:{reason}',
            final_status='CLOSED_WIN' if (
                mark and job.entry_price_usd and mark >= job.entry_price_usd
            ) else 'CLOSED_STOP',
            close_mark=mark,
        )
        return {'ok': True, 'job': job.to_json()}

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    async def _send_telegram(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is not None:
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
            logger.debug('[cross-long] telegram err: %s', exc)
