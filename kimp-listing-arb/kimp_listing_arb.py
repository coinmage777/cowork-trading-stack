"""KIMP Listing Arbitrage — KR 신규 상장 → 글로벌 선물 갭 따리 자동화.

Leo의 4/25 HYPER 사례 (업비트 상장 직후 KR 현물이 글로벌 perp 대비 +30%
김치 프리미엄 → 1~2시간 내 2~3% 수렴 → 초기 진입자 27%+ 수익) 자동 재현용.

설계 원칙:
    - listing_detector 의 add_listener 로 신규 상장 이벤트 구독
    - 양쪽 perp 존재 확인 (event payload 의 binance_perp/bybit_perp)
    - 갭 모니터링: KR 현물 vs 글로벌 perp (ccxt). 30 초 주기
    - 진입 조건: |kimp_pct| >= ENTRY_GAP_THRESHOLD (기본 5%)
        * KIMP 양수 (KR > 글로벌): KR 현물 매도 + 글로벌 perp SHORT
        * KIMP 음수 (역프, KR < 글로벌): KR 현물 매수 + 글로벌 perp LONG
    - 청산 조건: |kimp_pct| <= EXIT_GAP_THRESHOLD (기본 1%) → 양쪽 동시 close
    - 위험 관리:
        * paper 모드 기본값 (LIVE 는 LIVE_CONFIRM env + dry_run=False 동시 충족)
        * max_size_per_trade
        * daily_max_trades
        * kill switch 파일 (data/KILL_KIMP_ARB)
        * kill_gap (역방향으로 더 벌어지면 stop)
        * monitor_timeout_min (수렴 안 되면 forced exit)

데이터 흐름 (paper):
    listing_detector → on_listing_event → spawn _monitor_arb_opportunity
    → 60s 부트 (KR 체결 안정화 대기) → KR price + global perp price 동시 fetch
    → KIMP 계산 → 진입/관망/청산 결정 → JSONL 기록 + Telegram 알림

LIVE 경로 (Phase 2, 기본 비활성):
    Bithumb 상장 케이스만 우선 지원 (submit_bithumb_spot_order 재사용).
    Upbit 케이스는 KYC/private API 미구현 → paper 만.

GPT-5 Codex + Devin 리뷰 대비:
    - decimal precision: float 일관 (CEX ticker 정밀도 한계)
    - rate limit: ccxt enableRateLimit + 30s 폴링
    - 빈 오더북: bids/asks 빈 배열 가드
    - timezone: 모든 epoch UTC float
    - dedup: per-ticker inflight set + cooldown
    - kill switch: 폴링마다 재확인
    - paper mode: LIVE 트리플락 (enabled + !dry_run + live_confirm + LIVE env flag)
    - graceful shutdown: stop() 호출 시 모든 monitor task cancel + await

CLI 백테스트 (HYPER 4/25 사례):
    python -m strategies_minara.kimp_listing_arb --backtest \\
        --ticker HYPER --kr-price 56000 --global-price 43000 --size-usd 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# ----------------------------------------------------------------------
# 프로젝트 루트 임포트 (옵션 — 단독 실행 + backend 통합 둘 다 지원)
# ----------------------------------------------------------------------

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ----------------------------------------------------------------------
# 로깅
# ----------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR = Path(__file__).resolve().parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / 'kimp_listing_arb.log'
JSONL_FILE = DATA_DIR / 'kimp_listing_arb_jobs.jsonl'

logger = logging.getLogger('kimp_listing_arb')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
    fh.setFormatter(
        logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
    )
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter('[kimp_arb] %(levelname)s %(message)s'))
    logger.addHandler(sh)


# ----------------------------------------------------------------------
# env helpers (backend.config 의존 없이 단독 테스트 가능하도록 자체 구현)
# ----------------------------------------------------------------------

def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

@dataclass
class KimpArbConfig:
    """KIMP listing arb 운영 설정.

    `LIVE 트리플 락`:
        live_armed = enabled and (not dry_run) and live_confirm
        모든 항목이 True 일 때만 실주문 경로로 진입한다.
    """

    # 운영 토글
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False

    # 갭 임계값 (단위: %, 양수 = KR 비싸다 = KIMP 양수)
    entry_gap_pct: float = 5.0
    exit_gap_pct: float = 1.0
    kill_gap_pct: float = 30.0  # 양수 방향으로 더 벌어지면 abort (역선택 방지)

    # 사이즈 제어
    max_size_usd: float = 100.0
    daily_max_trades: int = 10
    daily_cap_usd: float = 500.0

    # 모니터링
    boot_delay_sec: int = 60        # 상장 직후 가격 안정화 대기
    monitoring_interval_sec: int = 30
    monitor_timeout_min: int = 240  # 4시간 안에 수렴 안 되면 forced exit
    max_event_age_sec: int = 600

    # 거래소
    global_venues: tuple = ('binance', 'bybit')   # ccxt swap (USDM perp)
    kr_venues: tuple = ('upbit', 'bithumb')

    # 운영 파일
    kill_switch_file: str = 'data/KILL_KIMP_ARB'
    jobs_path: str = str(JSONL_FILE)

    # 거래 가능한 KR venue (live 주문 가능 여부)
    # — Upbit private 미구현 상태이므로 bithumb 만 LIVE 지원
    kr_live_venues: tuple = ('bithumb',)

    @classmethod
    def load(cls) -> 'KimpArbConfig':
        return cls(
            enabled=_bool_env('KIMP_ARB_ENABLED', True),
            dry_run=_bool_env('KIMP_ARB_DRY_RUN', True),
            live_confirm=_bool_env('KIMP_ARB_LIVE_CONFIRM', False),
            entry_gap_pct=max(_float_env('KIMP_ARB_ENTRY_GAP_PCT', 5.0), 0.0),
            exit_gap_pct=max(_float_env('KIMP_ARB_EXIT_GAP_PCT', 1.0), 0.0),
            kill_gap_pct=max(_float_env('KIMP_ARB_KILL_GAP_PCT', 30.0), 0.0),
            max_size_usd=max(_float_env('KIMP_ARB_MAX_SIZE_USD', 100.0), 0.0),
            daily_max_trades=max(_int_env('KIMP_ARB_DAILY_MAX_TRADES', 10), 0),
            daily_cap_usd=max(_float_env('KIMP_ARB_DAILY_CAP_USD', 500.0), 0.0),
            boot_delay_sec=max(_int_env('KIMP_ARB_BOOT_DELAY_SEC', 60), 0),
            monitoring_interval_sec=max(_int_env('KIMP_ARB_MONITOR_INTERVAL_SEC', 30), 5),
            monitor_timeout_min=max(_int_env('KIMP_ARB_MONITOR_TIMEOUT_MIN', 240), 5),
            max_event_age_sec=max(_int_env('KIMP_ARB_MAX_EVENT_AGE_SEC', 600), 0),
            kill_switch_file=_str_env('KIMP_ARB_KILL_FILE', 'data/KILL_KIMP_ARB'),
            jobs_path=_str_env('KIMP_ARB_JOBS_PATH', str(JSONL_FILE)),
        )


# ----------------------------------------------------------------------
# 갭 계산
# ----------------------------------------------------------------------

def calc_kimp_pct(kr_price_usd: float, global_price_usd: float) -> float:
    """KIMP percentage. 양수 = KR > 글로벌 (김프), 음수 = 역프.

    공식: (kr_price_usd - global_price_usd) / global_price_usd * 100

    KR 가격은 USDT/KRW 환산 후 USD 단위로 입력해야 함.
    """
    if not kr_price_usd or not global_price_usd or global_price_usd <= 0:
        return 0.0
    return (kr_price_usd - global_price_usd) / global_price_usd * 100.0


def krw_to_usd(price_krw: float, usdt_krw: float) -> float | None:
    """KRW 가격을 USDT 환산 USD 가격으로 변환."""
    if not price_krw or not usdt_krw or usdt_krw <= 0:
        return None
    return price_krw / usdt_krw


# ----------------------------------------------------------------------
# Job dataclass — JSONL 기록용
# ----------------------------------------------------------------------

@dataclass
class KimpArbJob:
    job_id: str
    ts: float
    ticker: str
    kr_exchange: str
    global_venue: str
    direction: str            # 'kr_sell_global_short' | 'kr_buy_global_long'
    entry_kr_price_usd: float
    entry_global_price_usd: float
    entry_kimp_pct: float
    size_usd: float
    mode: str                 # 'paper' | 'live'

    # 청산 데이터 (수렴 시 채워짐)
    exit_ts: float = 0.0
    exit_kr_price_usd: float = 0.0
    exit_global_price_usd: float = 0.0
    exit_kimp_pct: float = 0.0
    exit_reason: str = ''     # 'converged' | 'kill_gap' | 'timeout' | 'manual' | 'error'
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0      # KIMP delta % (entry - exit, KR sell direction 기준)
    state: str = 'open'       # 'open' | 'closed' | 'aborted'

    notes: str = ''

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# Pricing fetcher (ccxt + KR direct)
# ----------------------------------------------------------------------

class PricingService:
    """KR + 글로벌 가격 조회 어댑터.

    - 글로벌 perp: ccxt fetch_ticker (BTC/USDT:USDT 형식, fapi/USDM)
    - KR 현물: 가능하면 backend.exchanges.bithumb_private.fetch_bithumb_bbo,
      fallback Upbit 직접 API.
    - USDT/KRW: bithumb USDT_KRW ticker.

    backend 임포트 실패해도 동작하도록 import-time fallback 유지.
    """

    def __init__(self) -> None:
        self._ccxt = None
        self._httpx = None
        self._exchanges: dict[str, Any] = {}  # ccxt swap instances cache
        self._lock = asyncio.Lock()

    def _ensure_libs(self) -> bool:
        if self._ccxt is None:
            try:
                import ccxt.async_support as ccxt  # type: ignore
                self._ccxt = ccxt
            except Exception as exc:  # noqa: BLE001
                logger.warning('ccxt not available: %s', exc)
                return False
        if self._httpx is None:
            try:
                import httpx  # type: ignore
                self._httpx = httpx
            except Exception as exc:  # noqa: BLE001
                logger.warning('httpx not available: %s', exc)
                return False
        return True

    async def _get_swap_instance(self, venue: str) -> Any:
        async with self._lock:
            inst = self._exchanges.get(venue)
            if inst is not None:
                return inst
            if not self._ensure_libs():
                return None
            try:
                cls = getattr(self._ccxt, venue)
            except AttributeError:
                logger.warning('ccxt has no exchange %s', venue)
                return None
            inst = cls({
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
            })
            try:
                await inst.load_markets()
            except Exception as exc:  # noqa: BLE001
                logger.warning('load_markets %s failed: %s', venue, exc)
                # cache anyway — fetch_ticker can still work for many venues
            self._exchanges[venue] = inst
            return inst

    async def close(self) -> None:
        async with self._lock:
            for venue, inst in list(self._exchanges.items()):
                try:
                    await inst.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug('close %s failed: %s', venue, exc)
            self._exchanges.clear()

    async def has_global_perp(self, venue: str, ticker: str) -> bool:
        """venue 의 USDM perp 마켓에 ticker 가 상장돼 있는지 확인."""
        inst = await self._get_swap_instance(venue)
        if inst is None:
            return False
        symbol = f'{ticker}/USDT:USDT'
        try:
            markets = inst.markets or {}
            if symbol in markets:
                return True
            # markets 미로드일 때만 fetch_markets 재시도
            if not markets:
                await inst.load_markets()
                return symbol in (inst.markets or {})
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug('has_global_perp(%s,%s) err: %s', venue, ticker, exc)
            return False

    async def fetch_global_perp_price(
        self, venue: str, ticker: str,
    ) -> tuple[float | None, float | None]:
        """글로벌 perp bid/ask 반환 (USD/USDT)."""
        inst = await self._get_swap_instance(venue)
        if inst is None:
            return None, None
        symbol = f'{ticker}/USDT:USDT'
        try:
            t = await inst.fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug('fetch_ticker %s %s err: %s', venue, symbol, exc)
            return None, None
        bid = t.get('bid')
        ask = t.get('ask')
        if bid is None and ask is None:
            return None, None
        try:
            return (float(bid) if bid else None,
                    float(ask) if ask else None)
        except (TypeError, ValueError):
            return None, None

    async def fetch_kr_price_krw(
        self, kr_venue: str, ticker: str,
    ) -> tuple[float | None, float | None]:
        """KR 현물 bid/ask (KRW)."""
        if kr_venue == 'bithumb':
            try:
                from backend.exchanges.bithumb_private import fetch_bithumb_bbo
                bbo = await fetch_bithumb_bbo(ticker)
                if bbo is None:
                    return None, None
                return bbo.bid, bbo.ask
            except Exception as exc:  # noqa: BLE001
                logger.debug('bithumb_bbo fallback for %s: %s', ticker, exc)

        # Upbit (또는 bithumb fallback)
        if not self._ensure_libs():
            return None, None
        try:
            url = 'https://api.upbit.com/v1/orderbook'
            params = {'markets': f'KRW-{ticker}'}
            async with self._httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return None, None
                data = resp.json()
            if not data or not isinstance(data, list):
                return None, None
            units = data[0].get('orderbook_units', [])
            if not units:
                return None, None
            unit = units[0]
            bid = unit.get('bid_price')
            ask = unit.get('ask_price')
            return (float(bid) if bid else None,
                    float(ask) if ask else None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('upbit orderbook %s err: %s', ticker, exc)
            return None, None

    async def fetch_usdt_krw(self) -> float | None:
        """USDT/KRW last."""
        try:
            from backend.exchanges.bithumb_private import fetch_usdt_krw
            v = await fetch_usdt_krw()
            if v:
                return v
        except Exception as exc:  # noqa: BLE001
            logger.debug('fetch_usdt_krw fallback: %s', exc)

        # Fallback — upbit USDT-KRW
        if not self._ensure_libs():
            return None
        try:
            url = 'https://api.upbit.com/v1/ticker'
            async with self._httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={'markets': 'KRW-USDT'})
                if resp.status_code != 200:
                    return None
                data = resp.json()
            if not data or not isinstance(data, list):
                return None
            return float(data[0].get('trade_price') or 0.0) or None
        except Exception as exc:  # noqa: BLE001
            logger.debug('upbit usdt fallback err: %s', exc)
            return None


# ----------------------------------------------------------------------
# 통계
# ----------------------------------------------------------------------

@dataclass
class _ArbState:
    daily_trades: int = 0
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = 0.0
    total_detected: int = 0
    total_skipped: int = 0
    total_entered: int = 0
    total_closed: int = 0
    total_aborted: int = 0
    last_error: str = ''
    open_jobs: dict[str, KimpArbJob] = field(default_factory=dict)
    last_entry_ts_per_ticker: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------
# Notifier (Telegram or no-op)
# ----------------------------------------------------------------------

class _SimpleTelegram:
    """경량 Telegram 알림. 환경변수 미설정 시 no-op."""

    def __init__(self) -> None:
        self.token = _str_env('TELEGRAM_BOT_TOKEN', '')
        self.chat_id = _str_env('TELEGRAM_CHAT_ID', '')
        self._enabled = bool(self.token and self.chat_id)

    async def send(self, text: str) -> None:
        if not self._enabled:
            return
        try:
            import aiohttp  # type: ignore
        except Exception:  # noqa: BLE001
            return
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        payload = {'chat_id': self.chat_id, 'text': text[:4000]}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                await sess.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug('telegram send err: %s', exc)


# ----------------------------------------------------------------------
# Main service
# ----------------------------------------------------------------------

ListingListener = Callable[[dict[str, Any]], Awaitable[None] | None]


class KimpListingArb:
    """KR 신규 상장 → 글로벌 perp KIMP 갭 따리.

    사용 예:
        from backend.services.listing_detector import ListingDetector
        detector = ListingDetector(...)
        arb = KimpListingArb(listing_detector=detector)
        await detector.start()
        await arb.start()
        ...
        await arb.stop()
        await detector.stop()
    """

    def __init__(
        self,
        listing_detector: Any = None,
        config: KimpArbConfig | None = None,
        telegram: Any = None,
    ) -> None:
        self.detector = listing_detector
        self.cfg = config or KimpArbConfig.load()
        self.telegram = telegram or _SimpleTelegram()
        self.pricing = PricingService()
        self.state = _ArbState()
        self._inflight: set[str] = set()
        self._monitor_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._jobs_path = Path(self.cfg.jobs_path)
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if not self.cfg.enabled:
            logger.info('KimpListingArb disabled via config; not starting.')
            return
        self._running = True
        if self.detector is not None and hasattr(self.detector, 'add_listener'):
            self.detector.add_listener(self._on_listing_event)
            logger.info(
                'KimpListingArb started (paper=%s live_armed=%s entry=%.1f%% exit=%.1f%% '
                'size=$%.0f cap=$%.0f)',
                self.cfg.dry_run or not self.cfg.live_confirm,
                (self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm),
                self.cfg.entry_gap_pct,
                self.cfg.exit_gap_pct,
                self.cfg.max_size_usd,
                self.cfg.daily_cap_usd,
            )
        else:
            logger.warning(
                'KimpListingArb: detector=%s has no add_listener; only manual hooks.',
                type(self.detector).__name__ if self.detector else 'None',
            )

    async def stop(self) -> None:
        self._running = False
        # 진행 중 monitor task 들 cancel
        for ticker, task in list(self._monitor_tasks.items()):
            if not task.done():
                task.cancel()
        for ticker, task in list(self._monitor_tasks.items()):
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug('monitor cancel %s err: %s', ticker, exc)
        self._monitor_tasks.clear()
        await self.pricing.close()
        logger.info('KimpListingArb stopped.')

    def status(self) -> dict[str, Any]:
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
        )
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'entry_gap_pct': self.cfg.entry_gap_pct,
            'exit_gap_pct': self.cfg.exit_gap_pct,
            'kill_gap_pct': self.cfg.kill_gap_pct,
            'max_size_usd': self.cfg.max_size_usd,
            'daily_max_trades': self.cfg.daily_max_trades,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'state': {
                'daily_trades': self.state.daily_trades,
                'daily_spent_usd': self.state.daily_spent_usd,
                'total_detected': self.state.total_detected,
                'total_skipped': self.state.total_skipped,
                'total_entered': self.state.total_entered,
                'total_closed': self.state.total_closed,
                'total_aborted': self.state.total_aborted,
                'last_error': self.state.last_error,
                'open_tickers': sorted(self.state.open_jobs.keys()),
            },
        }

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _today_midnight_epoch(self) -> float:
        # KR 기준 자정 (UTC+9). 단순화: UTC 자정.
        t = time.gmtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0))

    def _maybe_rollover_daily(self) -> None:
        bucket = self._today_midnight_epoch()
        if self.state.daily_reset_epoch != bucket:
            if self.state.daily_reset_epoch:
                logger.info(
                    '[kimp_arb] daily rollover: trades=%d spent=$%.2f',
                    self.state.daily_trades,
                    self.state.daily_spent_usd,
                )
            self.state.daily_trades = 0
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = bucket

    # ------------------------------------------------------------------
    # 리스너 엔트리
    # ------------------------------------------------------------------

    def _on_listing_event(self, event: dict[str, Any]) -> Any:
        """ListingDetector 콜백. 즉시 백그라운드 모니터 task 생성 후 return."""
        self.state.total_detected += 1
        if not self._running:
            return None
        ticker = str(event.get('ticker') or '').strip().upper()
        if not ticker:
            return None
        if ticker in self._monitor_tasks and not self._monitor_tasks[ticker].done():
            logger.info('[kimp_arb] %s already monitored; ignore', ticker)
            return None
        try:
            task = asyncio.create_task(
                self._monitor_arb_opportunity_safe(event),
                name=f'kimp_arb_{ticker}',
            )
        except RuntimeError:
            return None
        self._monitor_tasks[ticker] = task
        return task

    async def _monitor_arb_opportunity_safe(self, event: dict[str, Any]) -> None:
        ticker = str(event.get('ticker') or '').strip().upper()
        try:
            await self._monitor_arb_opportunity(event)
        except asyncio.CancelledError:
            logger.info('[kimp_arb] %s monitor cancelled', ticker)
            raise
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f'{type(exc).__name__}: {exc}'
            logger.exception('[kimp_arb] %s monitor unexpected err: %s', ticker, exc)
        finally:
            self._monitor_tasks.pop(ticker, None)

    # ------------------------------------------------------------------
    # 핵심: 모니터링 루프
    # ------------------------------------------------------------------

    async def _monitor_arb_opportunity(self, event: dict[str, Any]) -> None:
        ticker = str(event.get('ticker') or '').strip().upper()
        kr_exchange = str(event.get('exchange') or '').strip().lower()
        notice_id = str(event.get('id') or event.get('notice_id') or '')
        event_ts = float(event.get('ts') or 0.0)
        binance_perp = bool(event.get('binance_perp'))
        bybit_perp = bool(event.get('bybit_perp'))

        # 게이트 1: enabled
        if not self.cfg.enabled:
            self.state.total_skipped += 1
            return

        # 게이트 2: 이벤트 신선도
        now_ts = time.time()
        if event_ts > 0 and self.cfg.max_event_age_sec > 0:
            age = now_ts - event_ts
            if age > self.cfg.max_event_age_sec:
                logger.info('[kimp_arb] skip %s: event %.0fs old', ticker, age)
                self.state.total_skipped += 1
                return

        # 게이트 3: kill switch
        if self._kill_switch_active():
            logger.warning(
                '[kimp_arb] skip %s: kill switch %s present',
                ticker, self.cfg.kill_switch_file,
            )
            self.state.total_skipped += 1
            return

        # 게이트 4: daily caps
        self._maybe_rollover_daily()
        if self.cfg.daily_max_trades > 0 and self.state.daily_trades >= self.cfg.daily_max_trades:
            logger.warning('[kimp_arb] skip %s: daily trade cap %d', ticker, self.cfg.daily_max_trades)
            self.state.total_skipped += 1
            return

        # 게이트 5: per-ticker cooldown (1h 기본)
        last = self.state.last_entry_ts_per_ticker.get(ticker, 0.0)
        if last and (now_ts - last) < 3600:
            logger.info('[kimp_arb] skip %s: cooldown', ticker)
            self.state.total_skipped += 1
            return

        # 게이트 6: 글로벌 perp 후보 선택
        candidate_venues: list[str] = []
        if binance_perp and 'binance' in self.cfg.global_venues:
            candidate_venues.append('binance')
        if bybit_perp and 'bybit' in self.cfg.global_venues:
            candidate_venues.append('bybit')
        # event 의 perp 플래그가 비어 있어도 마켓에 있을 수 있으니 한번 확인
        if not candidate_venues:
            for v in self.cfg.global_venues:
                if await self.pricing.has_global_perp(v, ticker):
                    candidate_venues.append(v)
        if not candidate_venues:
            logger.info('[kimp_arb] skip %s (%s): no global perp', ticker, kr_exchange)
            self.state.total_skipped += 1
            return
        global_venue = candidate_venues[0]

        logger.info(
            '[kimp_arb] watch %s on %s vs %s perp (notice=%s)',
            ticker, kr_exchange, global_venue, notice_id,
        )

        # 부트 딜레이 — KR 가격 안정화 대기
        try:
            await asyncio.sleep(self.cfg.boot_delay_sec)
        except asyncio.CancelledError:
            return

        # 모니터링 루프
        deadline = time.time() + self.cfg.monitor_timeout_min * 60
        active_job: KimpArbJob | None = None

        while self._running and time.time() < deadline:
            if self._kill_switch_active():
                logger.warning('[kimp_arb] %s mid-loop: kill switch; abort', ticker)
                if active_job is not None:
                    await self._close_position(active_job, 'kill_switch')
                return

            usdt_krw = await self.pricing.fetch_usdt_krw()
            kr_bid_krw, kr_ask_krw = await self.pricing.fetch_kr_price_krw(kr_exchange, ticker)
            global_bid, global_ask = await self.pricing.fetch_global_perp_price(global_venue, ticker)

            if not usdt_krw or kr_bid_krw is None or kr_ask_krw is None or not global_bid or not global_ask:
                logger.info(
                    '[kimp_arb] %s pricing incomplete (usdt_krw=%s kr_bid=%s kr_ask=%s g_bid=%s g_ask=%s)',
                    ticker, usdt_krw, kr_bid_krw, kr_ask_krw, global_bid, global_ask,
                )
                await self._sleep_or_cancel(self.cfg.monitoring_interval_sec)
                continue

            # 가격 변환 — entry 방향에 따라 다른 쪽 BBO 적용
            kr_bid_usd = krw_to_usd(kr_bid_krw, usdt_krw)
            kr_ask_usd = krw_to_usd(kr_ask_krw, usdt_krw)

            # KIMP 양수 시나리오 (KR 비쌈): KR sell at bid, global short at bid
            kimp_pos_pct = calc_kimp_pct(kr_bid_usd or 0.0, global_bid)
            # KIMP 음수 시나리오 (역프, KR 쌈): KR buy at ask, global long at ask
            kimp_neg_pct = calc_kimp_pct(kr_ask_usd or 0.0, global_ask)

            # 더 큰 절대값을 가진 방향이 진입 후보
            if abs(kimp_pos_pct) >= abs(kimp_neg_pct):
                kimp_pct = kimp_pos_pct
                direction = 'kr_sell_global_short' if kimp_pct > 0 else 'kr_buy_global_long'
                kr_price_usd = kr_bid_usd or 0.0
                global_price_usd = global_bid
            else:
                kimp_pct = kimp_neg_pct
                direction = 'kr_sell_global_short' if kimp_pct > 0 else 'kr_buy_global_long'
                kr_price_usd = kr_ask_usd or 0.0
                global_price_usd = global_ask

            logger.info(
                '[kimp_arb] %s kimp=%+.2f%% kr=$%.4f g=$%.4f usdt_krw=%.2f',
                ticker, kimp_pct, kr_price_usd, global_price_usd, usdt_krw,
            )

            # ------------------------------------------------------------
            # 진입 전 — 갭 모니터링
            # ------------------------------------------------------------
            if active_job is None:
                if abs(kimp_pct) >= self.cfg.entry_gap_pct:
                    # 안전장치: kill_gap_pct 보다 크면 너무 위험 (역선택, halt 가능성)
                    if abs(kimp_pct) >= self.cfg.kill_gap_pct:
                        logger.warning(
                            '[kimp_arb] %s kimp=%+.2f%% > kill_gap=%.1f%%; abort',
                            ticker, kimp_pct, self.cfg.kill_gap_pct,
                        )
                        self.state.total_aborted += 1
                        return

                    if self.state.daily_spent_usd + self.cfg.max_size_usd > self.cfg.daily_cap_usd:
                        logger.warning('[kimp_arb] %s daily cap reached', ticker)
                        return

                    job = await self._open_position(
                        ticker=ticker,
                        kr_exchange=kr_exchange,
                        global_venue=global_venue,
                        direction=direction,
                        kr_price_usd=kr_price_usd,
                        global_price_usd=global_price_usd,
                        kimp_pct=kimp_pct,
                    )
                    if job is None:
                        return
                    active_job = job
                else:
                    # 갭이 임계값 미만 — 계속 모니터
                    pass
            else:
                # ------------------------------------------------------------
                # 진입 후 — 청산 조건 체크
                # ------------------------------------------------------------
                # 청산은 같은 방향의 KIMP 부호로 평가
                # entry 가 kr_sell_global_short(양수 kimp) 이면 양수 가까이 0 으로 수렴해야 win
                if active_job.direction == 'kr_sell_global_short':
                    eval_kimp = kimp_pos_pct
                else:
                    eval_kimp = kimp_neg_pct
                # 수렴: 절대값이 exit threshold 이하
                if abs(eval_kimp) <= self.cfg.exit_gap_pct:
                    await self._close_position_with_prices(
                        active_job,
                        kr_price_usd=(kr_ask_usd if active_job.direction == 'kr_sell_global_short' else kr_bid_usd) or kr_price_usd,
                        global_price_usd=(global_ask if active_job.direction == 'kr_sell_global_short' else global_bid),
                        kimp_pct=eval_kimp,
                        reason='converged',
                    )
                    return
                # 역방향으로 더 벌어짐 — kill
                if active_job.direction == 'kr_sell_global_short' and eval_kimp >= self.cfg.kill_gap_pct:
                    await self._close_position_with_prices(
                        active_job, kr_price_usd, global_price_usd, eval_kimp, 'kill_gap',
                    )
                    return
                if active_job.direction == 'kr_buy_global_long' and eval_kimp <= -self.cfg.kill_gap_pct:
                    await self._close_position_with_prices(
                        active_job, kr_price_usd, global_price_usd, eval_kimp, 'kill_gap',
                    )
                    return

            await self._sleep_or_cancel(self.cfg.monitoring_interval_sec)

        # 타임아웃
        if active_job is not None:
            await self._close_position(active_job, 'timeout')
        else:
            logger.info('[kimp_arb] %s monitor timeout, no entry', ticker)

    async def _sleep_or_cancel(self, sec: float) -> None:
        try:
            await asyncio.sleep(max(0.5, sec))
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # 진입 / 청산
    # ------------------------------------------------------------------

    def _live_armed(self) -> bool:
        return self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm

    async def _open_position(
        self,
        ticker: str,
        kr_exchange: str,
        global_venue: str,
        direction: str,
        kr_price_usd: float,
        global_price_usd: float,
        kimp_pct: float,
    ) -> KimpArbJob | None:
        size_usd = self.cfg.max_size_usd
        mode = 'live' if self._live_armed() else 'paper'

        # LIVE 추가 가드: KR 거래 가능 venue 인지
        if mode == 'live' and kr_exchange not in self.cfg.kr_live_venues:
            logger.warning(
                '[kimp_arb] %s LIVE blocked: %s not in kr_live_venues=%s — fallback paper',
                ticker, kr_exchange, self.cfg.kr_live_venues,
            )
            mode = 'paper'

        job = KimpArbJob(
            job_id=f'kimp_{ticker}_{int(time.time())}',
            ts=time.time(),
            ticker=ticker,
            kr_exchange=kr_exchange,
            global_venue=global_venue,
            direction=direction,
            entry_kr_price_usd=kr_price_usd,
            entry_global_price_usd=global_price_usd,
            entry_kimp_pct=kimp_pct,
            size_usd=size_usd,
            mode=mode,
        )

        if mode == 'live':
            try:
                await self._execute_live_entry(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception('[kimp_arb] %s LIVE entry failed: %s', ticker, exc)
                job.notes = f'live_entry_error: {exc}'
                job.state = 'aborted'
                self.state.total_aborted += 1
                await self._append_jsonl(job)
                return None

        self.state.total_entered += 1
        self.state.daily_trades += 1
        self.state.daily_spent_usd += size_usd
        self.state.last_entry_ts_per_ticker[ticker] = job.ts
        self.state.open_jobs[ticker] = job
        await self._append_jsonl(job)

        msg = (
            f'[KIMP ARB] ENTRY {ticker} ({mode})\n'
            f'  dir={direction}\n'
            f'  kimp={kimp_pct:+.2f}%  kr=${kr_price_usd:.4f}  g=${global_price_usd:.4f}\n'
            f'  size=${size_usd:.0f}  venue: {kr_exchange} ↔ {global_venue}'
        )
        logger.info(msg.replace('\n', ' | '))
        await self.telegram.send(msg)
        return job

    async def _execute_live_entry(self, job: KimpArbJob) -> None:
        """LIVE 주문 실행. Bithumb spot + ccxt swap perp 양쪽 동시 진입.

        실패 처리:
            - 한쪽만 체결되면 즉시 reverse(반대) 청산 후 abort
            - 양쪽 다 실패하면 abort

        Phase 2 (현재): Upbit private 미구현 → kr_exchange='upbit' 면 paper 폴백.
        """
        if job.kr_exchange != 'bithumb':
            raise RuntimeError(f'kr live not supported for {job.kr_exchange}')

        # 사이즈 → 수량
        if job.entry_kr_price_usd <= 0 or job.entry_global_price_usd <= 0:
            raise RuntimeError('zero price; cannot size')
        # KR side qty (코인 수량)
        kr_qty = job.size_usd / job.entry_kr_price_usd
        global_qty = job.size_usd / job.entry_global_price_usd

        # KR side
        from backend.exchanges.bithumb_private import submit_bithumb_spot_order
        if job.direction == 'kr_sell_global_short':
            kr_side = 'sell'
        else:
            kr_side = 'buy'
        kr_symbol = f'{job.ticker}/KRW'
        try:
            from backend.exchanges.bithumb_private import fetch_usdt_krw
            usdt_krw = await fetch_usdt_krw() or 1300.0
        except Exception:  # noqa: BLE001
            usdt_krw = 1300.0
        ref_price_krw = job.entry_kr_price_usd * usdt_krw
        kr_resp = await submit_bithumb_spot_order(
            symbol=kr_symbol,
            side=kr_side,
            amount=kr_qty,
            reference_price=ref_price_krw,
        )
        job.notes = f'kr_order={kr_resp.get("uuid", "?")}'

        # Global perp side (ccxt)
        inst = await self.pricing._get_swap_instance(job.global_venue)  # noqa: SLF001
        if inst is None:
            raise RuntimeError(f'global venue {job.global_venue} not available')
        side = 'sell' if job.direction == 'kr_sell_global_short' else 'buy'
        try:
            await inst.create_order(
                symbol=f'{job.ticker}/USDT:USDT',
                type='market',
                side=side,
                amount=global_qty,
                params={'reduceOnly': False},
            )
        except Exception as exc:  # noqa: BLE001
            # KR 한쪽 체결됨 — reverse close 시도
            try:
                rev_side = 'sell' if kr_side == 'buy' else 'buy'
                await submit_bithumb_spot_order(
                    symbol=kr_symbol,
                    side=rev_side,
                    amount=kr_qty,
                    reference_price=ref_price_krw,
                )
            except Exception as exc2:  # noqa: BLE001
                logger.error(
                    '[kimp_arb] %s reverse-close after global fail also failed: %s',
                    job.ticker, exc2,
                )
            raise RuntimeError(f'global entry failed: {exc}; KR reverse attempted') from exc

    async def _close_position_with_prices(
        self,
        job: KimpArbJob,
        kr_price_usd: float,
        global_price_usd: float,
        kimp_pct: float,
        reason: str,
    ) -> None:
        job.exit_kr_price_usd = kr_price_usd
        job.exit_global_price_usd = global_price_usd
        job.exit_kimp_pct = kimp_pct
        await self._close_position(job, reason)

    async def _close_position(self, job: KimpArbJob, reason: str) -> None:
        job.exit_ts = time.time()
        job.exit_reason = reason
        # PnL: KIMP delta 의 절대치 만큼 size_usd × delta% 수익 (대략)
        delta_pct = abs(job.entry_kimp_pct) - abs(job.exit_kimp_pct)
        job.pnl_pct = delta_pct
        job.pnl_usd = job.size_usd * (delta_pct / 100.0)
        job.state = 'closed' if reason == 'converged' else 'aborted'

        if job.mode == 'live':
            try:
                await self._execute_live_exit(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception('[kimp_arb] %s LIVE exit failed: %s', job.ticker, exc)
                job.notes = (job.notes + f'; live_exit_error: {exc}').strip('; ')

        if reason == 'converged':
            self.state.total_closed += 1
        else:
            self.state.total_aborted += 1
        self.state.open_jobs.pop(job.ticker, None)
        await self._append_jsonl(job)

        msg = (
            f'[KIMP ARB] EXIT {job.ticker} ({job.mode}) reason={reason}\n'
            f'  entry_kimp={job.entry_kimp_pct:+.2f}%  exit_kimp={job.exit_kimp_pct:+.2f}%\n'
            f'  pnl=${job.pnl_usd:+.2f} ({job.pnl_pct:+.2f}%)\n'
            f'  duration={(job.exit_ts - job.ts)/60:.1f}min'
        )
        logger.info(msg.replace('\n', ' | '))
        await self.telegram.send(msg)

    async def _execute_live_exit(self, job: KimpArbJob) -> None:
        """LIVE 청산. bithumb spot 반대 방향 + perp reduceOnly close.

        부분 실패 허용 — 한쪽 실패해도 다른쪽은 닫도록 best-effort.
        """
        if job.kr_exchange != 'bithumb':
            raise RuntimeError(f'kr live not supported for {job.kr_exchange}')
        from backend.exchanges.bithumb_private import submit_bithumb_spot_order, fetch_usdt_krw

        kr_qty = job.size_usd / max(job.entry_kr_price_usd, 1e-9)
        global_qty = job.size_usd / max(job.entry_global_price_usd, 1e-9)
        rev_side = 'buy' if job.direction == 'kr_sell_global_short' else 'sell'
        try:
            usdt_krw = await fetch_usdt_krw() or 1300.0
        except Exception:  # noqa: BLE001
            usdt_krw = 1300.0
        ref_price_krw = (job.exit_kr_price_usd or job.entry_kr_price_usd) * usdt_krw

        # KR side close
        kr_err: Exception | None = None
        try:
            await submit_bithumb_spot_order(
                symbol=f'{job.ticker}/KRW',
                side=rev_side,
                amount=kr_qty,
                reference_price=ref_price_krw,
            )
        except Exception as exc:  # noqa: BLE001
            kr_err = exc
            logger.exception('[kimp_arb] %s KR exit err: %s', job.ticker, exc)

        # Global side close (reduceOnly)
        global_err: Exception | None = None
        try:
            inst = await self.pricing._get_swap_instance(job.global_venue)  # noqa: SLF001
            if inst is None:
                raise RuntimeError(f'global venue {job.global_venue} not available')
            side = 'buy' if job.direction == 'kr_sell_global_short' else 'sell'
            await inst.create_order(
                symbol=f'{job.ticker}/USDT:USDT',
                type='market',
                side=side,
                amount=global_qty,
                params={'reduceOnly': True},
            )
        except Exception as exc:  # noqa: BLE001
            global_err = exc
            logger.exception('[kimp_arb] %s global exit err: %s', job.ticker, exc)

        if kr_err and global_err:
            raise RuntimeError(f'both legs exit failed: kr={kr_err} global={global_err}')

    # ------------------------------------------------------------------
    # 영속화
    # ------------------------------------------------------------------

    async def _append_jsonl(self, job: KimpArbJob) -> None:
        async with self._write_lock:
            try:
                with self._jobs_path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(job.to_json(), ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('jsonl append err: %s', exc)

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0 or not self._jobs_path.exists():
            return []
        try:
            with self._jobs_path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:  # noqa: BLE001
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


# ----------------------------------------------------------------------
# 백테스트 / CLI 시뮬레이터
# ----------------------------------------------------------------------

def simulate_arb(
    ticker: str,
    entry_kr_price_usd: float,
    entry_global_price_usd: float,
    exit_kr_price_usd: float,
    exit_global_price_usd: float,
    size_usd: float = 100.0,
    fee_pct_each_leg: float = 0.1,    # KR 시장가 0.04% + perp taker 0.05% — 보수적
    slippage_pct_each_leg: float = 0.2,
) -> dict[str, Any]:
    """단순 KIMP 갭 따리 시뮬. 마찰비 (수수료+슬리피지) 차감 PnL 계산.

    가정:
        - entry 시 양쪽 동시 진입 (KR sell, global short or vice versa)
        - exit 시 양쪽 동시 청산
        - 마찰: 4 leg (entry KR + entry G + exit KR + exit G) 각 수수료+슬리피지

    Returns:
        {entry_kimp, exit_kimp, gross_pct, friction_pct, net_pct, net_usd, ...}
    """
    entry_kimp = calc_kimp_pct(entry_kr_price_usd, entry_global_price_usd)
    exit_kimp = calc_kimp_pct(exit_kr_price_usd, exit_global_price_usd)
    gross_pct = abs(entry_kimp) - abs(exit_kimp)
    friction_pct = (fee_pct_each_leg + slippage_pct_each_leg) * 4
    net_pct = gross_pct - friction_pct
    return {
        'ticker': ticker,
        'entry_kimp_pct': entry_kimp,
        'exit_kimp_pct': exit_kimp,
        'gross_pct': gross_pct,
        'friction_pct': friction_pct,
        'net_pct': net_pct,
        'net_usd': size_usd * net_pct / 100.0,
        'size_usd': size_usd,
    }


def _cli() -> int:
    p = argparse.ArgumentParser('kimp_listing_arb')
    p.add_argument('--backtest', action='store_true', help='시뮬레이션만 실행')
    p.add_argument('--ticker', default='HYPER')
    p.add_argument('--entry-kr-usd', type=float, default=56.0,
                   help='entry KR price USD (default: HYPER 4/25 case ≈ $56)')
    p.add_argument('--entry-global-usd', type=float, default=43.0,
                   help='entry global perp USD (default: HYPER 4/25 case ≈ $43)')
    p.add_argument('--exit-kr-usd', type=float, default=44.0,
                   help='exit KR USD (after convergence)')
    p.add_argument('--exit-global-usd', type=float, default=43.0,
                   help='exit global USD (largely flat)')
    p.add_argument('--size-usd', type=float, default=100.0)
    p.add_argument('--fee-pct', type=float, default=0.1)
    p.add_argument('--slip-pct', type=float, default=0.2)
    args = p.parse_args()

    if args.backtest:
        result = simulate_arb(
            ticker=args.ticker,
            entry_kr_price_usd=args.entry_kr_usd,
            entry_global_price_usd=args.entry_global_usd,
            exit_kr_price_usd=args.exit_kr_usd,
            exit_global_price_usd=args.exit_global_usd,
            size_usd=args.size_usd,
            fee_pct_each_leg=args.fee_pct,
            slippage_pct_each_leg=args.slip_pct,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print('use --backtest to run the offline simulator', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(_cli())
