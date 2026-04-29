"""RWA Commodity Cross-Venue Basis Arbitrage.

배경 (plusevdeal #7, JUSTCRYT #1):
Ostium / Hyperliquid / MEXC / Bybit / Extended / Flipster 등 여러 거래소가 원자재
(Brent, WTI crude oil, Gold XAU) 을 perp 형태로 제공한다. 각 거래소가
- 자체 funding rate 스케줄 (시간/빈도/cap 다름)
- 선물/현물 롤오버 일정 차이
- 유동성 편차 (프리미엄/디스카운트)
를 가지므로, 같은 commodity 에 대해 **cross-venue basis spread** 가 자주 벌어진다.

전형적으로 ~0.1% / 일 수준으로 안정적인 cash-and-carry 가 가능하다:
    싼 venue 에서 LONG + 비싼 venue 에서 SHORT (동일 notional)
    → 스프레드 수렴 시 수익, funding 차이로 carry 수익도 누적

세금 경고 (한국):
- perp funding income 은 기타소득/사업소득으로 과세 가능 (~30~44% 실효세율)
- 모든 fill 의 예상 수익은 **pre-tax / post-tax 병기** 로 알림해야 사용자 판단 가능.

트리플 락 (실자금):
    COMMODITY_BASIS_ENABLED=true
    AND COMMODITY_BASIS_DRY_RUN=false
    AND COMMODITY_BASIS_LIVE_CONFIRM=true
추가 안전: kill switch file, daily cap, notional cap, rollover guard.

Ostium 은 custom perp DEX (web3) 라 LIVE 실행은 Phase X.1 (NotImplementedError). Dry-run
에서는 CCXT 가능한 venue 조합만으로도 스프레드 감지가 완전 동작.
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

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# env 헬퍼
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
        raw = os.getenv(key, '').strip()
        return float(raw) if raw else float(default)
    except (ValueError, TypeError):
        return float(default)


def _env_int(key: str, default: int) -> int:
    try:
        raw = os.getenv(key, '').strip()
        return int(float(raw)) if raw else int(default)
    except (ValueError, TypeError):
        return int(default)


# ----------------------------------------------------------------------
# 파일 경로
# ----------------------------------------------------------------------


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_WATCHLIST_PATH = _DATA_DIR / 'commodity_watchlist.json'
_DEFAULT_JOBS_PATH = _DATA_DIR / 'commodity_basis_jobs.jsonl'


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class CommodityBasisConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False

    notional_usd: float = 200.0
    leverage: int = 2                     # commodity 는 저변동 → 2x 권장
    min_spread_pct: float = 0.15          # 0.15% 이상 스프레드에서 신호
    max_notional_usd: float = 500.0       # per-trade cap
    daily_cap_usd: float = 2000.0

    poll_interval_sec: int = 60
    rollover_min_days: int = 3            # 만기 3일 이내면 진입 거부

    tax_rate_pct: float = 40.0            # 예상 한국 실효세율 (기타/사업소득)

    convergence_exit_pct: float = 0.03    # 스프레드 3bp 이하로 수렴 시 TP
    stop_loss_pct: float = 1.5            # 진입 스프레드 대비 +1.5%p 악화 시 SL
    max_hold_days: int = 14

    watchlist_path: str = str(_DEFAULT_WATCHLIST_PATH)
    jobs_path: str = str(_DEFAULT_JOBS_PATH)
    kill_switch_file: str = 'data/KILL_COMMODITY'

    # Ostium / HL 등 LIVE 미지원 거래소 이름 집합 — 진입 차단
    live_unsupported_exchanges: tuple[str, ...] = ('ostium', 'flipster', 'extended')

    @classmethod
    def load(cls) -> 'CommodityBasisConfig':
        return cls(
            enabled=_env_bool('COMMODITY_BASIS_ENABLED', True),
            dry_run=_env_bool('COMMODITY_BASIS_DRY_RUN', True),
            live_confirm=_env_bool('COMMODITY_BASIS_LIVE_CONFIRM', False),
            notional_usd=_env_float('COMMODITY_NOTIONAL_USD', 200.0),
            leverage=max(_env_int('COMMODITY_LEVERAGE', 2), 1),
            min_spread_pct=_env_float('COMMODITY_MIN_SPREAD_PCT', 0.15),
            max_notional_usd=_env_float('COMMODITY_MAX_NOTIONAL_USD', 500.0),
            daily_cap_usd=_env_float('COMMODITY_DAILY_CAP_USD', 2000.0),
            poll_interval_sec=max(_env_int('COMMODITY_POLL_INTERVAL_SEC', 60), 15),
            rollover_min_days=max(_env_int('COMMODITY_ROLLOVER_MIN_DAYS', 3), 0),
            tax_rate_pct=_env_float('COMMODITY_TAX_RATE_PCT', 40.0),
            convergence_exit_pct=_env_float('COMMODITY_CONVERGENCE_EXIT_PCT', 0.03),
            stop_loss_pct=_env_float('COMMODITY_STOP_LOSS_PCT', 1.5),
            max_hold_days=_env_int('COMMODITY_MAX_HOLD_DAYS', 14),
            watchlist_path=_env(
                'COMMODITY_WATCHLIST_PATH', str(_DEFAULT_WATCHLIST_PATH)
            ),
            jobs_path=_env('COMMODITY_JOBS_PATH', str(_DEFAULT_JOBS_PATH)),
            kill_switch_file=_env(
                'COMMODITY_KILL_SWITCH_FILE', 'data/KILL_COMMODITY'
            ),
        )

    @property
    def live_armed(self) -> bool:
        return self.enabled and (not self.dry_run) and self.live_confirm


# ----------------------------------------------------------------------
# Watchlist
# ----------------------------------------------------------------------


@dataclass
class CommodityVenue:
    exchange: str                      # 'ostium' | 'hyperliquid' | 'mexc' | 'bybit' | 'extended' | 'flipster'
    symbol: str                        # 거래소별 심볼 (예: 'BRENT_USDT' on MEXC, 'BRN' on Ostium)
    instrument_type: str = 'perp'      # 'perp' | 'futures' | 'dated_futures'
    expiry_ts: int = 0                 # dated_futures 면 만료 epoch, 0=perp (무만기)

    def is_expiring_soon(self, min_days: int) -> bool:
        if self.expiry_ts <= 0:
            return False
        remaining = self.expiry_ts - int(time.time())
        return remaining < (min_days * 86400)

    def to_json(self) -> dict[str, Any]:
        return {
            'exchange': self.exchange,
            'symbol': self.symbol,
            'type': self.instrument_type,
            'expiry_ts': self.expiry_ts,
        }


@dataclass
class CommodityWatch:
    symbol: str                        # 통합 심볼 (예: 'BRENT', 'WTI', 'XAU')
    venues: list[CommodityVenue] = field(default_factory=list)
    min_spread_pct: float = 0.15       # 이 commodity 전용 override (0 이면 global default)
    notes: str = ''

    def to_json(self) -> dict[str, Any]:
        return {
            'symbol': self.symbol,
            'venues': [v.to_json() for v in self.venues],
            'min_spread_pct': self.min_spread_pct,
            'notes': self.notes,
        }


_BUILTIN_WATCHLIST: list[dict[str, Any]] = [
    {
        'symbol': 'BRENT',
        'venues': [
            {'exchange': 'ostium', 'symbol': 'BRN', 'type': 'perp'},
            {'exchange': 'hyperliquid', 'symbol': 'BRENT', 'type': 'perp'},
            {'exchange': 'mexc', 'symbol': 'BRENT_USDT', 'type': 'futures'},
        ],
        'min_spread_pct': 0.15,
        'notes': 'Brent crude oil — Ostium/HL/MEXC 크로스-벤유 basis',
    },
    {
        'symbol': 'WTI',
        'venues': [
            {'exchange': 'ostium', 'symbol': 'CL', 'type': 'perp'},
            {'exchange': 'hyperliquid', 'symbol': 'WTI', 'type': 'perp'},
            {'exchange': 'mexc', 'symbol': 'WTI_USDT', 'type': 'futures'},
        ],
        'min_spread_pct': 0.15,
        'notes': 'WTI crude oil',
    },
    {
        'symbol': 'XAU',
        'venues': [
            {'exchange': 'hyperliquid', 'symbol': 'XAU', 'type': 'perp'},
            {'exchange': 'bybit', 'symbol': 'XAUUSDT', 'type': 'perp'},
            {'exchange': 'ostium', 'symbol': 'XAU', 'type': 'perp'},
        ],
        'min_spread_pct': 0.10,
        'notes': 'Gold XAU — HL / Bybit / Ostium',
    },
]


def _load_watchlist_from_file(path: Path) -> list[CommodityWatch]:
    if not path.exists():
        return []
    try:
        with path.open('r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[commodity] watchlist load failed: %s', exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[CommodityWatch] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            venues_raw = item.get('venues') or []
            venues: list[CommodityVenue] = []
            for v in venues_raw:
                if not isinstance(v, dict):
                    continue
                ex = str(v.get('exchange') or '').strip().lower()
                sym = str(v.get('symbol') or '').strip()
                if not ex or not sym:
                    continue
                venues.append(CommodityVenue(
                    exchange=ex,
                    symbol=sym,
                    instrument_type=str(v.get('type') or 'perp').strip().lower(),
                    expiry_ts=int(float(v.get('expiry_ts') or 0)),
                ))
            if len(venues) < 2:
                logger.debug(
                    '[commodity] skip watch %s: <2 venues',
                    item.get('symbol'),
                )
                continue
            entry = CommodityWatch(
                symbol=str(item.get('symbol') or '').strip().upper(),
                venues=venues,
                min_spread_pct=float(item.get('min_spread_pct') or 0.0),
                notes=str(item.get('notes') or ''),
            )
            if not entry.symbol:
                continue
        except (TypeError, ValueError) as exc:
            logger.warning('[commodity] watchlist item parse failed: %s', exc)
            continue
        out.append(entry)
    return out


def _save_watchlist_to_file(path: Path, entries: list[CommodityWatch]) -> None:
    payload = [e.to_json() for e in entries]
    tmp = path.with_suffix('.json.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ----------------------------------------------------------------------
# Job 모델
# ----------------------------------------------------------------------


@dataclass
class CommodityBasisJob:
    job_id: str
    symbol: str
    buy_exchange: str                  # long leg
    buy_symbol: str
    sell_exchange: str                 # short leg
    sell_symbol: str
    mode: str                          # 'live' | 'dry_run'
    entry_buy_ask: float
    entry_sell_bid: float
    entry_spread_pct: float
    notional_usd: float
    leverage: int
    buy_qty: float
    sell_qty: float
    created_at: int
    max_hold_ts: int
    buy_funding_rate: float = 0.0      # last snapshot
    sell_funding_rate: float = 0.0
    # 예상 일일 수익 (진입 시점 기준)
    expected_daily_usd_pretax: float = 0.0
    expected_daily_usd_posttax: float = 0.0
    status: str = 'open'               # open | closing | closed_win | closed_stop | closed_timeout | closed_rollover | closed_err
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    close_buy_price: Optional[float] = None
    close_sell_price: Optional[float] = None
    close_spread_pct: Optional[float] = None
    close_reason: Optional[str] = None
    closed_at: Optional[int] = None
    pnl_usd: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'symbol': self.symbol,
            'buy_exchange': self.buy_exchange,
            'buy_symbol': self.buy_symbol,
            'sell_exchange': self.sell_exchange,
            'sell_symbol': self.sell_symbol,
            'mode': self.mode,
            'entry_buy_ask': self.entry_buy_ask,
            'entry_sell_bid': self.entry_sell_bid,
            'entry_spread_pct': self.entry_spread_pct,
            'notional_usd': self.notional_usd,
            'leverage': self.leverage,
            'buy_qty': self.buy_qty,
            'sell_qty': self.sell_qty,
            'created_at': self.created_at,
            'max_hold_ts': self.max_hold_ts,
            'buy_funding_rate': self.buy_funding_rate,
            'sell_funding_rate': self.sell_funding_rate,
            'expected_daily_usd_pretax': self.expected_daily_usd_pretax,
            'expected_daily_usd_posttax': self.expected_daily_usd_posttax,
            'status': self.status,
            'buy_order_id': self.buy_order_id,
            'sell_order_id': self.sell_order_id,
            'close_buy_price': self.close_buy_price,
            'close_sell_price': self.close_sell_price,
            'close_spread_pct': self.close_spread_pct,
            'close_reason': self.close_reason,
            'closed_at': self.closed_at,
            'pnl_usd': self.pnl_usd,
            'warnings': list(self.warnings),
        }


# ----------------------------------------------------------------------
# 유틸
# ----------------------------------------------------------------------


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


@dataclass
class _VenueQuote:
    exchange: str
    symbol: str
    bid: float                          # 매수 가능한 최고가 (상대가 팔겠다는 가격이 아니라, 내가 팔 때 받는 가격)
    ask: float                          # 매도 제시가 (내가 살 때 내야 하는 가격)
    funding_rate: float = 0.0           # per interval (일반적으로 8h)
    funding_interval_hours: float = 8.0

    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.bid or self.ask

    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class _SpreadOpportunity:
    symbol: str
    buy_venue: _VenueQuote              # 롱 leg
    sell_venue: _VenueQuote             # 숏 leg
    spread_pct: float                   # (sell.bid - buy.ask) / buy.ask * 100

    def to_summary(self) -> dict[str, Any]:
        return {
            'symbol': self.symbol,
            'buy_exchange': self.buy_venue.exchange,
            'sell_exchange': self.sell_venue.exchange,
            'buy_ask': self.buy_venue.ask,
            'sell_bid': self.sell_venue.bid,
            'spread_pct': round(self.spread_pct, 4),
            'buy_funding_rate': self.buy_venue.funding_rate,
            'sell_funding_rate': self.sell_venue.funding_rate,
        }


# ----------------------------------------------------------------------
# 메인 서비스
# ----------------------------------------------------------------------


class CommodityBasisArb:
    """Cross-venue commodity basis arbitrage.

    사용 예::

        service = CommodityBasisArb(hedge_service=hedge_trade_service, telegram_service=tg)
        await service.start()
        ...
        await service.stop()

    주의:
    - Ostium / Extended / Flipster 는 LIVE 실행 미지원 (NotImplementedError).
      dry-run 은 cross-venue detection + 알림 완전 동작.
    - LIVE 진입은 triple-lock + kill switch + daily cap + rollover guard 전부 통과해야 함.
    - 세금 계산은 **추정치** — 실제 한국 신고 기준이 아닐 수 있음. 사용자 검증 필수.
    """

    def __init__(
        self,
        hedge_service: Any = None,
        telegram_service: Any = None,
        cfg: Optional[CommodityBasisConfig] = None,
    ) -> None:
        self.cfg = cfg or CommodityBasisConfig.load()
        self.hedge_service = hedge_service
        self.telegram = telegram_service

        self._jobs: dict[str, CommodityBasisJob] = {}
        self._lock = asyncio.Lock()

        self._scan_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._running: bool = False

        self._inflight_keys: set[str] = set()
        self._last_alert_ts: dict[str, float] = {}
        self._alert_cooldown_sec: int = 1800

        self._daily_spent_usd: float = 0.0
        self._daily_reset_epoch: float = _today_midnight_epoch()

        self._watchlist: list[CommodityWatch] = []
        self._load_or_seed_watchlist()

        self._http = None  # type: Optional[Any]

        # 최근 기회 캐시 (status API 응답용)
        self._recent_opportunities: list[dict[str, Any]] = []
        self._max_recent_opps = 20

        # 통계
        self._total_scans = 0
        self._total_signals = 0
        self._total_entries = 0
        self._total_dry_run = 0
        self._total_wins = 0
        self._total_stops = 0
        self._total_timeouts = 0
        self._total_rollover_exits = 0
        self._total_errors = 0
        self._last_error: str = ''

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            import aiohttp  # type: ignore
            timeout = aiohttp.ClientTimeout(total=15)
            self._http = aiohttp.ClientSession(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                '[commodity] aiohttp unavailable: %s (dry-run 감지 degraded)', exc,
            )
            self._http = None

        self._scan_task = asyncio.create_task(
            self._scan_loop(), name='commodity_basis_scan',
        )
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name='commodity_basis_monitor',
        )
        logger.info(
            '[commodity] started | enabled=%s dry_run=%s live_confirm=%s '
            'min_spread=%.2f%% notional=$%.0f lev=%dx daily_cap=$%.0f '
            'tax=%.1f%% watch=%d',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.min_spread_pct, self.cfg.notional_usd, self.cfg.leverage,
            self.cfg.daily_cap_usd, self.cfg.tax_rate_pct, len(self._watchlist),
        )

    async def stop(self) -> None:
        self._running = False
        for t in (self._scan_task, self._monitor_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug('[commodity] task stop err: %s', exc)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:  # noqa: BLE001
                pass
            self._http = None
        logger.info('[commodity] stopped')

    # ------------------------------------------------------------------
    # 상태
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
            'notional_usd': self.cfg.notional_usd,
            'max_notional_usd': self.cfg.max_notional_usd,
            'leverage': self.cfg.leverage,
            'min_spread_pct': self.cfg.min_spread_pct,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self._daily_spent_usd, 2),
            'convergence_exit_pct': self.cfg.convergence_exit_pct,
            'stop_loss_pct': self.cfg.stop_loss_pct,
            'max_hold_days': self.cfg.max_hold_days,
            'rollover_min_days': self.cfg.rollover_min_days,
            'tax_rate_pct': self.cfg.tax_rate_pct,
            'poll_interval_sec': self.cfg.poll_interval_sec,
            'live_unsupported_exchanges': list(self.cfg.live_unsupported_exchanges),
            'watchlist_count': len(self._watchlist),
            'watchlist': [w.to_json() for w in self._watchlist],
            'open_jobs_count': len(self._open_jobs()),
            'open_jobs': [j.to_json() for j in self._open_jobs()],
            'recent_opportunities': list(self._recent_opportunities),
            'total_scans': self._total_scans,
            'total_signals': self._total_signals,
            'total_entries': self._total_entries,
            'total_dry_run': self._total_dry_run,
            'total_wins': self._total_wins,
            'total_stops': self._total_stops,
            'total_timeouts': self._total_timeouts,
            'total_rollover_exits': self._total_rollover_exits,
            'total_errors': self._total_errors,
            'last_error': self._last_error,
        }

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.cfg.jobs_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] recent_jobs read err: %s', exc)
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
    # scan loop
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._total_errors += 1
                self._last_error = f'{type(exc).__name__}: {exc}'
                logger.error('[commodity] scan err: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _scan_once(self) -> None:
        self._total_scans += 1
        if self._kill_switch_active():
            logger.debug('[commodity] kill switch active, skip scan')
            return
        self._maybe_rollover_daily()

        open_symbols = {
            self._key(j.symbol, j.buy_exchange, j.sell_exchange)
            for j in self._open_jobs()
        }
        current_opps: list[dict[str, Any]] = []

        for watch in list(self._watchlist):
            try:
                quotes = await self._fetch_all_quotes(watch)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    '[commodity] %s quotes fetch err: %s', watch.symbol, exc,
                )
                continue
            valid = [q for q in quotes if q.is_valid()]
            if len(valid) < 2:
                continue
            opp = self._best_spread(watch.symbol, valid)
            if opp is None:
                continue

            min_spread = watch.min_spread_pct or self.cfg.min_spread_pct
            current_opps.append(opp.to_summary())
            if opp.spread_pct < min_spread:
                continue

            key = self._key(
                watch.symbol, opp.buy_venue.exchange, opp.sell_venue.exchange,
            )
            if key in open_symbols:
                continue
            if key in self._inflight_keys:
                continue

            self._total_signals += 1
            await self._handle_signal(watch, opp)

        # 최근 기회 캐시 업데이트
        self._recent_opportunities = current_opps[: self._max_recent_opps]

    def _best_spread(
        self, symbol: str, quotes: list[_VenueQuote],
    ) -> Optional[_SpreadOpportunity]:
        """모든 (buy, sell) 페어 중 최대 스프레드 조합 반환."""
        best: Optional[_SpreadOpportunity] = None
        for buy_q in quotes:
            for sell_q in quotes:
                if buy_q is sell_q:
                    continue
                if buy_q.ask <= 0:
                    continue
                spread_pct = (sell_q.bid - buy_q.ask) / buy_q.ask * 100.0
                if best is None or spread_pct > best.spread_pct:
                    best = _SpreadOpportunity(
                        symbol=symbol, buy_venue=buy_q,
                        sell_venue=sell_q, spread_pct=spread_pct,
                    )
        return best

    # ------------------------------------------------------------------
    # signal handling
    # ------------------------------------------------------------------

    async def _handle_signal(
        self, watch: CommodityWatch, opp: _SpreadOpportunity,
    ) -> None:
        key = self._key(
            opp.symbol, opp.buy_venue.exchange, opp.sell_venue.exchange,
        )
        self._inflight_keys.add(key)
        try:
            # rollover guard: 둘 중 하나라도 만기 < 3일이면 진입 거부
            for leg_ex, leg_sym in (
                (opp.buy_venue.exchange, opp.buy_venue.symbol),
                (opp.sell_venue.exchange, opp.sell_venue.symbol),
            ):
                venue = self._find_venue(watch, leg_ex, leg_sym)
                if venue is not None and venue.is_expiring_soon(self.cfg.rollover_min_days):
                    msg = (
                        f'{leg_ex}/{leg_sym} expires < {self.cfg.rollover_min_days}d — '
                        f'entry blocked'
                    )
                    logger.info('[commodity] %s signal BLOCKED: %s', opp.symbol, msg)
                    await self._alert_once(
                        key=f'rollover:{key}',
                        text=(
                            f'ℹ️ [commodity] signal blocked (rollover guard)\n'
                            f'  {opp.symbol} {leg_ex}/{leg_sym}\n  {msg}'
                        ),
                    )
                    return

            ok, reason = self._can_enter(self.cfg.notional_usd)
            if not ok:
                logger.info(
                    '[commodity] %s signal BLOCKED: %s (spread=%.2f%%)',
                    opp.symbol, reason, opp.spread_pct,
                )
                await self._alert_once(
                    key=f'block:{key}',
                    text=(
                        f'ℹ️ [commodity] signal blocked\n'
                        f'  {opp.symbol} '
                        f'{opp.buy_venue.exchange}→{opp.sell_venue.exchange} '
                        f'spread={opp.spread_pct:+.2f}%\n  reason={reason}'
                    ),
                )
                return

            if not self.cfg.live_armed:
                await self._record_dry_run(watch, opp)
                return

            await self._execute_basis_entry(watch, opp, self.cfg.notional_usd)
        finally:
            self._inflight_keys.discard(key)

    # ------------------------------------------------------------------
    # 예상 수익 / 세금 계산
    # ------------------------------------------------------------------

    def _estimate_daily_income(self, opp: _SpreadOpportunity, notional: float) -> tuple[float, float]:
        """(pretax_usd_per_day, posttax_usd_per_day) 추정.

        - 스프레드 수렴 이익 (convergence) 은 일회성이라 funding-only 로 계산.
        - funding_rate 단위: per interval (기본 8h). 하루 = 3 intervals 가정.
        - LONG leg 은 funding < 0 이면 지급받음 (long pays positive funding). 공식:
              long_daily_funding = -buy_funding_rate * intervals_per_day * notional
              short_daily_funding = +sell_funding_rate * intervals_per_day * notional
          (간단히: short 는 funding 이 +면 지급받음, long 은 funding 이 -면 지급받음)
        - Net carry = long_daily_funding + short_daily_funding
        """
        # 둘 중 한쪽 interval 기준이 다를 수 있으니 평균 사용
        buy_iv = opp.buy_venue.funding_interval_hours or 8.0
        sell_iv = opp.sell_venue.funding_interval_hours or 8.0
        buy_intervals_per_day = 24.0 / buy_iv if buy_iv > 0 else 3.0
        sell_intervals_per_day = 24.0 / sell_iv if sell_iv > 0 else 3.0

        long_daily = -float(opp.buy_venue.funding_rate) * buy_intervals_per_day * notional
        short_daily = float(opp.sell_venue.funding_rate) * sell_intervals_per_day * notional
        pretax = long_daily + short_daily

        tax_rate = max(0.0, min(self.cfg.tax_rate_pct, 100.0)) / 100.0
        # funding income 만 과세 대상 (positive 일 때만 과세)
        posttax = pretax - max(pretax, 0.0) * tax_rate
        return pretax, posttax

    # ------------------------------------------------------------------
    # dry-run
    # ------------------------------------------------------------------

    async def _record_dry_run(
        self, watch: CommodityWatch, opp: _SpreadOpportunity,
    ) -> None:
        self._total_dry_run += 1
        notional = self.cfg.notional_usd
        buy_qty = notional / opp.buy_venue.ask if opp.buy_venue.ask > 0 else 0.0
        sell_qty = notional / opp.sell_venue.bid if opp.sell_venue.bid > 0 else 0.0
        pretax, posttax = self._estimate_daily_income(opp, notional)
        max_hold_ts = int(time.time() + self.cfg.max_hold_days * 86400)
        job = CommodityBasisJob(
            job_id=f'cmbs_{uuid.uuid4().hex[:10]}',
            symbol=opp.symbol,
            buy_exchange=opp.buy_venue.exchange,
            buy_symbol=opp.buy_venue.symbol,
            sell_exchange=opp.sell_venue.exchange,
            sell_symbol=opp.sell_venue.symbol,
            mode='dry_run',
            entry_buy_ask=opp.buy_venue.ask,
            entry_sell_bid=opp.sell_venue.bid,
            entry_spread_pct=opp.spread_pct,
            notional_usd=notional,
            leverage=self.cfg.leverage,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            created_at=int(time.time()),
            max_hold_ts=max_hold_ts,
            buy_funding_rate=opp.buy_venue.funding_rate,
            sell_funding_rate=opp.sell_venue.funding_rate,
            expected_daily_usd_pretax=round(pretax, 4),
            expected_daily_usd_posttax=round(posttax, 4),
            status='open',
            warnings=['dry_run'],
        )
        async with self._lock:
            self._jobs[job.job_id] = job
        await self._append_jsonl(job.to_json())
        self._daily_spent_usd += notional
        logger.info(
            '[DRY-COMMODITY] %s %s→%s spread=%.3f%% buy=%.4f sell=%.4f '
            'expected $%.2f/day pretax ($%.2f posttax @ %.1f%% tax)',
            opp.symbol, opp.buy_venue.exchange, opp.sell_venue.exchange,
            opp.spread_pct, opp.buy_venue.ask, opp.sell_venue.bid,
            pretax, posttax, self.cfg.tax_rate_pct,
        )
        await self._send_telegram(
            f'🛢️ [DRY] commodity basis\n'
            f'  {opp.symbol} +{opp.spread_pct:.2f}% '
            f'({opp.buy_venue.exchange}→{opp.sell_venue.exchange})\n'
            f'  buy={opp.buy_venue.ask:.4f} sell={opp.sell_venue.bid:.4f} '
            f'${notional:.0f} x{self.cfg.leverage}\n'
            f'  expected ${pretax:.2f}/day pre-tax / ${posttax:.2f} post-tax '
            f'({self.cfg.tax_rate_pct:.0f}% KR)',
            alert_key='commodity_dry',
        )

    # ------------------------------------------------------------------
    # LIVE 실행 — 양쪽 leg 진입
    # ------------------------------------------------------------------

    async def _execute_basis_entry(
        self, watch: CommodityWatch, opp: _SpreadOpportunity, notional: float,
    ) -> None:
        unsupported = {x.lower() for x in self.cfg.live_unsupported_exchanges}
        buy_ex = opp.buy_venue.exchange.lower()
        sell_ex = opp.sell_venue.exchange.lower()

        if buy_ex in unsupported or sell_ex in unsupported:
            msg = (
                f'LIVE 미지원 venue 포함: buy={buy_ex} sell={sell_ex} '
                f'(unsupported={sorted(unsupported)})'
            )
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(
                f'⚠️ commodity LIVE 미지원\n  {opp.symbol}\n  {msg}'
            )
            raise NotImplementedError(
                f'commodity LIVE integration — Phase X.1 (buy={buy_ex} sell={sell_ex})'
            )

        if self.hedge_service is None:
            msg = 'hedge_service is None'
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        if notional <= 0 or notional > self.cfg.max_notional_usd:
            msg = (
                f'notional $%.2f outside [0, $%.2f]'
                % (notional, self.cfg.max_notional_usd)
            )
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        buy_qty = notional / opp.buy_venue.ask if opp.buy_venue.ask > 0 else 0.0
        sell_qty = notional / opp.sell_venue.bid if opp.sell_venue.bid > 0 else 0.0
        if buy_qty <= 0 or sell_qty <= 0:
            msg = f'qty<=0 buy={buy_qty} sell={sell_qty}'
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        pretax, posttax = self._estimate_daily_income(opp, notional)
        max_hold_ts = int(time.time() + self.cfg.max_hold_days * 86400)
        job = CommodityBasisJob(
            job_id=f'cmbs_{uuid.uuid4().hex[:10]}',
            symbol=opp.symbol,
            buy_exchange=buy_ex,
            buy_symbol=opp.buy_venue.symbol,
            sell_exchange=sell_ex,
            sell_symbol=opp.sell_venue.symbol,
            mode='live',
            entry_buy_ask=opp.buy_venue.ask,
            entry_sell_bid=opp.sell_venue.bid,
            entry_spread_pct=opp.spread_pct,
            notional_usd=notional,
            leverage=self.cfg.leverage,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            created_at=int(time.time()),
            max_hold_ts=max_hold_ts,
            buy_funding_rate=opp.buy_venue.funding_rate,
            sell_funding_rate=opp.sell_venue.funding_rate,
            expected_daily_usd_pretax=round(pretax, 4),
            expected_daily_usd_posttax=round(posttax, 4),
            status='open',
        )

        try:
            from backend.exchanges import manager as exchange_manager
        except Exception as exc:  # noqa: BLE001
            msg = f'exchange_manager import failed: {exc}'
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        buy_instance = exchange_manager.get_instance(buy_ex, 'swap')
        sell_instance = exchange_manager.get_instance(sell_ex, 'swap')
        if buy_instance is None or sell_instance is None:
            msg = (
                f'swap instance unavailable buy={buy_instance is not None} '
                f'sell={sell_instance is not None}'
            )
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(f'⚠️ commodity {opp.symbol} {msg}')
            return

        buy_symbol = self._resolve_symbol(
            exchange_manager, buy_ex, opp.buy_venue.symbol,
        )
        sell_symbol = self._resolve_symbol(
            exchange_manager, sell_ex, opp.sell_venue.symbol,
        )

        for inst in (buy_instance, sell_instance):
            try:
                if not getattr(inst, 'markets', None):
                    await inst.load_markets()
            except Exception as exc:  # noqa: BLE001
                logger.warning('[commodity] load_markets err: %s', exc)

        try:
            if hasattr(buy_instance, 'amount_to_precision'):
                buy_qty = float(buy_instance.amount_to_precision(buy_symbol, buy_qty))
            if hasattr(sell_instance, 'amount_to_precision'):
                sell_qty = float(sell_instance.amount_to_precision(sell_symbol, sell_qty))
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] normalize qty err: %s', exc)

        job.buy_qty = buy_qty
        job.sell_qty = sell_qty

        warnings: list[str] = []
        prepare = getattr(self.hedge_service, '_prepare_futures_account', None)
        if callable(prepare):
            try:
                w1 = await prepare(
                    exchange_instance=buy_instance, symbol=buy_symbol,
                    leverage=self.cfg.leverage,
                ) or []
                w2 = await prepare(
                    exchange_instance=sell_instance, symbol=sell_symbol,
                    leverage=self.cfg.leverage,
                ) or []
                warnings.extend(w1)
                warnings.extend(w2)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f'leverage prep err: {exc}')
                logger.warning('[commodity] leverage prep: %s', exc)

        submit = getattr(self.hedge_service, '_submit_market_order', None)
        if not callable(submit):
            msg = 'hedge_service._submit_market_order unavailable'
            logger.error('[commodity] %s abort: %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        # 2-leg 동시 진입 — 하나 실패 시 상대 leg 롤백
        try:
            buy_task = asyncio.create_task(submit(
                exchange_instance=buy_instance, exchange_name=buy_ex,
                symbol=buy_symbol, side='buy', amount=buy_qty,
                market='futures', reference_price=opp.buy_venue.ask,
            ))
            sell_task = asyncio.create_task(submit(
                exchange_instance=sell_instance, exchange_name=sell_ex,
                symbol=sell_symbol, side='sell', amount=sell_qty,
                market='futures', reference_price=opp.sell_venue.bid,
            ))
            buy_result, sell_result = await asyncio.gather(
                buy_task, sell_task, return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f'submit gather exc: {exc}'
            logger.exception('[commodity] %s %s', opp.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(f'⚠️ commodity {opp.symbol} {msg}')
            return

        buy_ok, buy_filled, buy_avg, buy_err = self._parse_fill(buy_result)
        sell_ok, sell_filled, sell_avg, sell_err = self._parse_fill(sell_result)

        if not (buy_ok and sell_ok):
            # 한쪽만 체결됐으면 롤백 (reduceOnly 로 즉시 청산)
            job.warnings.append(
                f'partial fill: buy_ok={buy_ok} sell_ok={sell_ok} '
                f'buy_err={buy_err} sell_err={sell_err}'
            )
            await self._rollback_partial_fill(
                exchange_manager=exchange_manager,
                job=job,
                buy_ok=buy_ok, buy_filled=buy_filled, buy_symbol=buy_symbol,
                buy_instance=buy_instance,
                sell_ok=sell_ok, sell_filled=sell_filled, sell_symbol=sell_symbol,
                sell_instance=sell_instance,
            )
            job.status = 'closed_err'
            job.closed_at = int(time.time())
            job.close_reason = 'partial_fill_rollback'
            await self._append_jsonl(job.to_json())
            self._total_errors += 1
            await self._send_telegram(
                f'⚠️ commodity {opp.symbol} partial fill rollback'
            )
            return

        # 정상 체결 — entry 가 갱신
        job.entry_buy_ask = float(buy_avg or opp.buy_venue.ask)
        job.entry_sell_bid = float(sell_avg or opp.sell_venue.bid)
        job.buy_qty = float(buy_filled)
        job.sell_qty = float(sell_filled)
        if job.entry_buy_ask > 0:
            job.entry_spread_pct = (
                (job.entry_sell_bid - job.entry_buy_ask) / job.entry_buy_ask * 100.0
            )
        job.buy_order_id = str(self._get_field(buy_result, 'order_id') or '') or None
        job.sell_order_id = str(self._get_field(sell_result, 'order_id') or '') or None
        job.warnings = list(warnings)

        async with self._lock:
            self._jobs[job.job_id] = job

        self._total_entries += 1
        self._daily_spent_usd += notional
        await self._append_jsonl(job.to_json())
        logger.info(
            '[commodity] LIVE %s %s→%s spread=%.3f%% '
            'buy=%.4f(%.6f) sell=%.4f(%.6f) expected $%.2f/day pre / $%.2f post',
            opp.symbol, buy_ex, sell_ex, job.entry_spread_pct,
            job.entry_buy_ask, job.buy_qty, job.entry_sell_bid, job.sell_qty,
            pretax, posttax,
        )
        await self._send_telegram(
            f'🛢️ commodity basis ENTER\n'
            f'  {opp.symbol} +{job.entry_spread_pct:.2f}% '
            f'({buy_ex}→{sell_ex})\n'
            f'  buy=${job.entry_buy_ask:.4f} sell=${job.entry_sell_bid:.4f} '
            f'${notional:.0f} x{self.cfg.leverage}\n'
            f'  expected ${pretax:.2f}/day pre-tax / ${posttax:.2f} post-tax '
            f'({self.cfg.tax_rate_pct:.0f}% KR)'
        )

    @staticmethod
    def _resolve_symbol(exchange_manager: Any, exchange: str, raw_symbol: str) -> str:
        try:
            return exchange_manager.get_symbol(
                ticker=raw_symbol, market_type='swap', exchange_id=exchange,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                '[commodity] get_symbol fallback %s/%s: %s',
                exchange, raw_symbol, exc,
            )
            return raw_symbol

    @staticmethod
    def _parse_fill(result: Any) -> tuple[bool, float, float, Any]:
        if isinstance(result, BaseException):
            return False, 0.0, 0.0, result
        if not isinstance(result, dict):
            return False, 0.0, 0.0, 'non_dict_result'
        err = result.get('error')
        try:
            filled = float(result.get('filled_qty') or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        try:
            avg = float(result.get('avg_price') or 0.0)
        except (TypeError, ValueError):
            avg = 0.0
        status = str(result.get('status') or '').lower()
        ok = (
            filled > 0
            and not err
            and status in {'closed', 'filled', 'ok'}
        )
        return ok, filled, avg, err

    @staticmethod
    def _get_field(result: Any, key: str) -> Any:
        if isinstance(result, dict):
            return result.get(key)
        return None

    async def _rollback_partial_fill(
        self,
        *,
        exchange_manager: Any,
        job: CommodityBasisJob,
        buy_ok: bool, buy_filled: float, buy_symbol: str, buy_instance: Any,
        sell_ok: bool, sell_filled: float, sell_symbol: str, sell_instance: Any,
    ) -> None:
        """부분 체결 롤백 — 체결된 leg 를 reduceOnly 로 즉시 청산."""
        submit_close = getattr(
            self.hedge_service, '_submit_futures_close_generic_reduce_only', None,
        )
        if not callable(submit_close):
            job.warnings.append('rollback unavailable: no submit_close helper')
            return

        if buy_ok and buy_filled > 0:
            try:
                await submit_close(
                    futures_instance=buy_instance,
                    exchange_name=job.buy_exchange,
                    symbol=buy_symbol,
                    side='sell',
                    amount=buy_filled,
                )
            except Exception as exc:  # noqa: BLE001
                job.warnings.append(f'rollback buy-leg err: {exc}')
                logger.warning('[commodity] rollback buy err: %s', exc)
        if sell_ok and sell_filled > 0:
            try:
                await submit_close(
                    futures_instance=sell_instance,
                    exchange_name=job.sell_exchange,
                    symbol=sell_symbol,
                    side='buy',
                    amount=sell_filled,
                )
            except Exception as exc:  # noqa: BLE001
                job.warnings.append(f'rollback sell-leg err: {exc}')
                logger.warning('[commodity] rollback sell err: %s', exc)

    # ------------------------------------------------------------------
    # monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._monitor_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error('[commodity] monitor err: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _monitor_tick(self) -> None:
        async with self._lock:
            open_jobs = self._open_jobs()
        for job in open_jobs:
            try:
                await self._check_job(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    '[commodity] check_job %s err: %s', job.job_id, exc,
                )

    async def _check_job(self, job: CommodityBasisJob) -> None:
        now = int(time.time())
        # 1) timeout
        if now >= job.max_hold_ts:
            await self._close_job(
                job, reason='timeout', final_status='closed_timeout',
                close_buy=None, close_sell=None, close_spread=None,
            )
            self._total_timeouts += 1
            return

        # 2) rollover guard (in-flight): 둘 중 하나라도 만기 < 2일이면 즉시 청산
        watch = self._find_watch(job.symbol)
        if watch is not None:
            for leg_ex, leg_sym in (
                (job.buy_exchange, job.buy_symbol),
                (job.sell_exchange, job.sell_symbol),
            ):
                v = self._find_venue(watch, leg_ex, leg_sym)
                if v is not None and v.is_expiring_soon(max(self.cfg.rollover_min_days - 1, 1)):
                    await self._close_job(
                        job, reason=f'rollover_imminent:{leg_ex}:{leg_sym}',
                        final_status='closed_rollover',
                        close_buy=None, close_sell=None, close_spread=None,
                    )
                    self._total_rollover_exits += 1
                    return

        # 3) 현재 스프레드 재조회
        quotes = await self._fetch_all_quotes(watch) if watch else []
        valid = [q for q in quotes if q.is_valid()]
        buy_q = self._find_quote(valid, job.buy_exchange, job.buy_symbol)
        sell_q = self._find_quote(valid, job.sell_exchange, job.sell_symbol)
        if buy_q is None or sell_q is None:
            return
        # 롱 leg 이 올랐고 숏 leg 이 내렸으면 스프레드 축소 = win
        current_spread_pct = (
            (sell_q.bid - buy_q.ask) / buy_q.ask * 100.0 if buy_q.ask > 0 else 0.0
        )

        # 4) TP — 수렴
        if current_spread_pct <= self.cfg.convergence_exit_pct:
            await self._close_job(
                job, reason='target_converged',
                final_status='closed_win',
                close_buy=buy_q.ask, close_sell=sell_q.bid,
                close_spread=current_spread_pct,
            )
            self._total_wins += 1
            return

        # 5) SL — 스프레드가 진입 대비 +stop_loss_pct p 만큼 악화
        if current_spread_pct > job.entry_spread_pct + self.cfg.stop_loss_pct:
            await self._close_job(
                job,
                reason=(
                    f'stop_loss_spread_{current_spread_pct:.2f}_from_'
                    f'{job.entry_spread_pct:.2f}'
                ),
                final_status='closed_stop',
                close_buy=buy_q.ask, close_sell=sell_q.bid,
                close_spread=current_spread_pct,
            )
            self._total_stops += 1
            return

    # ------------------------------------------------------------------
    # 가격 / funding 조회
    # ------------------------------------------------------------------

    async def _fetch_all_quotes(
        self, watch: CommodityWatch,
    ) -> list[_VenueQuote]:
        tasks = [self._fetch_quote(watch.symbol, v) for v in watch.venues]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_VenueQuote] = []
        for v, r in zip(watch.venues, results):
            if isinstance(r, BaseException):
                logger.debug(
                    '[commodity] %s %s/%s fetch err: %s',
                    watch.symbol, v.exchange, v.symbol, r,
                )
                continue
            if r is None:
                continue
            out.append(r)
        return out

    async def _fetch_quote(
        self, symbol: str, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        ex = venue.exchange.lower()
        if ex == 'hyperliquid':
            return await self._fetch_hyperliquid_quote(venue)
        if ex == 'bybit':
            return await self._fetch_bybit_quote(venue)
        if ex == 'mexc':
            return await self._fetch_mexc_quote(venue)
        if ex == 'ostium':
            return await self._fetch_ostium_quote(venue)
        if ex in {'extended', 'flipster'}:
            return await self._fetch_generic_override_quote(venue)
        # ccxt fallback
        return await self._fetch_ccxt_quote(venue)

    async def _fetch_hyperliquid_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        """HL metaAndAssetCtxs 에서 mid + funding 추출."""
        if self._http is None:
            return None
        try:
            async with self._http.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'metaAndAssetCtxs'},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] HL err %s: %s', venue.symbol, exc)
            return None
        try:
            meta = data[0] if isinstance(data, list) and data else {}
            ctxs = data[1] if isinstance(data, list) and len(data) > 1 else []
            universe = (meta or {}).get('universe') or []
            for idx, uni in enumerate(universe):
                name = str(uni.get('name') or '').upper()
                if name == venue.symbol.upper() and idx < len(ctxs):
                    ctx = ctxs[idx] or {}
                    mark_raw = ctx.get('markPx') or ctx.get('markPrice')
                    mid_raw = ctx.get('midPx') or mark_raw
                    funding_raw = ctx.get('funding') or 0.0
                    mid = float(mid_raw) if mid_raw is not None else 0.0
                    if mid <= 0:
                        return None
                    # HL 은 book BBO 대신 mid 만 노출 → ±0.5bp 로 근사
                    spread_eps = mid * 0.00005
                    return _VenueQuote(
                        exchange=venue.exchange, symbol=venue.symbol,
                        bid=mid - spread_eps, ask=mid + spread_eps,
                        funding_rate=float(funding_raw),
                        funding_interval_hours=1.0,  # HL funding 1시간 주기
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] HL parse err %s: %s', venue.symbol, exc)
        return None

    async def _fetch_bybit_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        if self._http is None:
            return None
        try:
            url = (
                f'https://api.bybit.com/v5/market/tickers'
                f'?category=linear&symbol={venue.symbol}'
            )
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] bybit err %s: %s', venue.symbol, exc)
            return None
        try:
            lst = ((data or {}).get('result') or {}).get('list') or []
            if not lst:
                return None
            row = lst[0]
            bid = float(row.get('bid1Price') or 0.0)
            ask = float(row.get('ask1Price') or 0.0)
            if bid <= 0 or ask <= 0:
                # fallback to markPrice
                mark = float(row.get('markPrice') or row.get('lastPrice') or 0.0)
                if mark <= 0:
                    return None
                spread_eps = mark * 0.0001
                bid = mark - spread_eps
                ask = mark + spread_eps
            funding_raw = float(row.get('fundingRate') or 0.0)
            return _VenueQuote(
                exchange=venue.exchange, symbol=venue.symbol,
                bid=bid, ask=ask, funding_rate=funding_raw,
                funding_interval_hours=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] bybit parse err %s: %s', venue.symbol, exc)
            return None

    async def _fetch_mexc_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        if self._http is None:
            return None
        # MEXC contract v1 public ticker
        try:
            url = (
                f'https://contract.mexc.com/api/v1/contract/ticker'
                f'?symbol={venue.symbol}'
            )
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] mexc err %s: %s', venue.symbol, exc)
            return None
        try:
            d = (data or {}).get('data') or {}
            if isinstance(d, list):
                d = d[0] if d else {}
            bid = float(d.get('bid1') or 0.0)
            ask = float(d.get('ask1') or 0.0)
            if bid <= 0 or ask <= 0:
                fair = float(d.get('fairPrice') or d.get('lastPrice') or 0.0)
                if fair <= 0:
                    return None
                spread_eps = fair * 0.0001
                bid = fair - spread_eps
                ask = fair + spread_eps
            funding_raw = float(d.get('fundingRate') or 0.0)
            return _VenueQuote(
                exchange=venue.exchange, symbol=venue.symbol,
                bid=bid, ask=ask, funding_rate=funding_raw,
                funding_interval_hours=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] mexc parse err %s: %s', venue.symbol, exc)
            return None

    async def _fetch_ostium_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        """Ostium public price feed — 엔드포인트 override 필요.

        OSTIUM_QUOTE_URL=https://.../markets/{symbol}
        응답에서 bid/ask/mark/fundingRate 키를 탐색.
        """
        override = _env('OSTIUM_QUOTE_URL')
        if not override or self._http is None:
            return None
        url = override.replace('{symbol}', venue.symbol).replace('{SYMBOL}', venue.symbol)
        try:
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] ostium err %s: %s', venue.symbol, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            bid = float(data.get('bid') or 0.0)
            ask = float(data.get('ask') or 0.0)
            if bid <= 0 or ask <= 0:
                mark = float(
                    data.get('markPrice')
                    or data.get('mark_px')
                    or data.get('price')
                    or 0.0
                )
                if mark <= 0:
                    return None
                spread_eps = mark * 0.0002
                bid = mark - spread_eps
                ask = mark + spread_eps
            funding_raw = float(data.get('fundingRate') or 0.0)
            return _VenueQuote(
                exchange=venue.exchange, symbol=venue.symbol,
                bid=bid, ask=ask, funding_rate=funding_raw,
                funding_interval_hours=float(data.get('fundingIntervalHours') or 8.0),
            )
        except (TypeError, ValueError) as exc:
            logger.debug('[commodity] ostium parse err %s: %s', venue.symbol, exc)
            return None

    async def _fetch_generic_override_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        """Extended / Flipster 등 — {EXCHANGE}_QUOTE_URL 엔드포인트 override."""
        key = f'{venue.exchange.upper()}_QUOTE_URL'
        override = _env(key)
        if not override or self._http is None:
            return None
        url = override.replace('{symbol}', venue.symbol).replace('{SYMBOL}', venue.symbol)
        try:
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[commodity] %s err %s: %s', venue.exchange, venue.symbol, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            bid = float(data.get('bid') or 0.0)
            ask = float(data.get('ask') or 0.0)
            if bid <= 0 or ask <= 0:
                mark = float(
                    data.get('markPrice') or data.get('price') or 0.0
                )
                if mark <= 0:
                    return None
                spread_eps = mark * 0.0002
                bid = mark - spread_eps
                ask = mark + spread_eps
            funding_raw = float(data.get('fundingRate') or 0.0)
            return _VenueQuote(
                exchange=venue.exchange, symbol=venue.symbol,
                bid=bid, ask=ask, funding_rate=funding_raw,
                funding_interval_hours=float(data.get('fundingIntervalHours') or 8.0),
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                '[commodity] %s parse err %s: %s',
                venue.exchange, venue.symbol, exc,
            )
            return None

    async def _fetch_ccxt_quote(
        self, venue: CommodityVenue,
    ) -> Optional[_VenueQuote]:
        """CCXT 일반 경로 — 낯선 거래소 fallback."""
        try:
            from backend.exchanges import manager as exchange_manager
        except Exception:  # noqa: BLE001
            return None
        instance = exchange_manager.get_instance(venue.exchange, 'swap')
        if instance is None:
            return None
        try:
            if not getattr(instance, 'markets', None):
                await instance.load_markets()
        except Exception:  # noqa: BLE001
            pass
        try:
            ccxt_symbol = exchange_manager.get_symbol(
                ticker=venue.symbol, market_type='swap', exchange_id=venue.exchange,
            )
        except Exception:  # noqa: BLE001
            ccxt_symbol = venue.symbol
        try:
            ticker = await instance.fetch_ticker(ccxt_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                '[commodity] ccxt fetch_ticker err %s/%s: %s',
                venue.exchange, venue.symbol, exc,
            )
            return None
        try:
            bid = float(ticker.get('bid') or 0.0) if ticker else 0.0
            ask = float(ticker.get('ask') or 0.0) if ticker else 0.0
            last = float(ticker.get('last') or 0.0) if ticker else 0.0
            if (bid <= 0 or ask <= 0) and last > 0:
                spread_eps = last * 0.0001
                bid = last - spread_eps
                ask = last + spread_eps
            if bid <= 0 or ask <= 0:
                return None
        except (TypeError, ValueError):
            return None
        funding = 0.0
        try:
            if hasattr(instance, 'fetch_funding_rate'):
                fr = await instance.fetch_funding_rate(ccxt_symbol)
                if isinstance(fr, dict):
                    funding = float(fr.get('fundingRate') or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                '[commodity] fetch_funding_rate %s/%s: %s',
                venue.exchange, venue.symbol, exc,
            )
        return _VenueQuote(
            exchange=venue.exchange, symbol=venue.symbol,
            bid=bid, ask=ask, funding_rate=funding,
            funding_interval_hours=8.0,
        )

    # ------------------------------------------------------------------
    # 청산
    # ------------------------------------------------------------------

    async def _close_job(
        self,
        job: CommodityBasisJob,
        *,
        reason: str,
        final_status: str,
        close_buy: Optional[float],
        close_sell: Optional[float],
        close_spread: Optional[float],
    ) -> None:
        async with self._lock:
            if job.status != 'open':
                return
            job.status = 'closing'

        pnl_usd: Optional[float] = None
        live_closed = False

        if job.mode == 'live' and self.hedge_service is not None:
            try:
                b_res, s_res = await self._live_close_job(job)
                b_ok, b_filled, b_avg, _ = self._parse_fill(b_res)
                s_ok, s_filled, s_avg, _ = self._parse_fill(s_res)
                if b_ok and s_ok:
                    live_closed = True
                    close_buy = close_buy or b_avg
                    close_sell = close_sell or s_avg
                    # PnL = 롱leg(상승분) + 숏leg(하락분)
                    #     = (close_buy - entry_buy_ask)*buy_qty
                    #     + (entry_sell_bid - close_sell)*sell_qty
                    pnl_usd = round(
                        (float(b_avg) - job.entry_buy_ask) * job.buy_qty
                        + (job.entry_sell_bid - float(s_avg)) * job.sell_qty,
                        6,
                    )
                else:
                    job.warnings.append(
                        f'close partial: buy_ok={b_ok} sell_ok={s_ok}'
                    )
            except NotImplementedError as exc:
                logger.warning('[commodity] close unsupported: %s', exc)
                job.warnings.append(f'close_not_implemented: {exc}')
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    '[commodity] live close failed %s: %s', job.job_id, exc,
                )
                job.warnings.append(f'close_exc: {exc}')

        # 가상 PnL (dry-run / live 실패 시)
        if pnl_usd is None and close_buy and close_sell:
            if job.buy_qty > 0 and job.sell_qty > 0:
                pnl_usd = round(
                    (float(close_buy) - job.entry_buy_ask) * job.buy_qty
                    + (job.entry_sell_bid - float(close_sell)) * job.sell_qty,
                    6,
                )

        async with self._lock:
            job.status = final_status
            job.closed_at = int(time.time())
            job.close_reason = reason
            job.close_buy_price = close_buy
            job.close_sell_price = close_sell
            job.close_spread_pct = close_spread
            job.pnl_usd = pnl_usd

        await self._append_jsonl(job.to_json())
        logger.info(
            '[commodity] CLOSE %s %s %s→%s reason=%s '
            'close_buy=%s close_sell=%s spread=%s pnl=%s (live_closed=%s)',
            job.job_id, job.symbol, job.buy_exchange, job.sell_exchange,
            reason, close_buy, close_sell, close_spread, pnl_usd, live_closed,
        )
        emoji = '✅' if final_status == 'closed_win' else (
            '🟠' if final_status in {'closed_timeout', 'closed_rollover'} else '🛑'
        )
        await self._send_telegram(
            f'{emoji} commodity CLOSE {job.symbol}\n'
            f'  {job.buy_exchange}→{job.sell_exchange} reason={reason}\n'
            f'  entry_spread={job.entry_spread_pct:.3f}% '
            f'close_spread={close_spread if close_spread is None else round(close_spread, 3)} '
            f'pnl=${pnl_usd}'
        )

    async def _live_close_job(
        self, job: CommodityBasisJob,
    ) -> tuple[Any, Any]:
        unsupported = {x.lower() for x in self.cfg.live_unsupported_exchanges}
        if (
            job.buy_exchange.lower() in unsupported
            or job.sell_exchange.lower() in unsupported
        ):
            raise NotImplementedError(
                f'LIVE close 미지원: {job.buy_exchange}/{job.sell_exchange}'
            )
        try:
            from backend.exchanges import manager as exchange_manager
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f'exchange_manager import err: {exc}') from exc
        buy_inst = exchange_manager.get_instance(job.buy_exchange, 'swap')
        sell_inst = exchange_manager.get_instance(job.sell_exchange, 'swap')
        if buy_inst is None or sell_inst is None:
            raise RuntimeError(
                f'swap instance unavailable buy={buy_inst is not None} '
                f'sell={sell_inst is not None}'
            )
        for inst in (buy_inst, sell_inst):
            try:
                if not getattr(inst, 'markets', None):
                    await inst.load_markets()
            except Exception:  # noqa: BLE001
                pass

        submit_close = getattr(
            self.hedge_service, '_submit_futures_close_generic_reduce_only', None,
        )
        if not callable(submit_close):
            raise RuntimeError('submit_close helper unavailable')

        buy_sym = self._resolve_symbol(
            exchange_manager, job.buy_exchange, job.buy_symbol,
        )
        sell_sym = self._resolve_symbol(
            exchange_manager, job.sell_exchange, job.sell_symbol,
        )

        # 롱leg 청산 = sell reduceOnly, 숏leg 청산 = buy reduceOnly
        buy_task = asyncio.create_task(submit_close(
            futures_instance=buy_inst, exchange_name=job.buy_exchange,
            symbol=buy_sym, side='sell', amount=job.buy_qty,
        ))
        sell_task = asyncio.create_task(submit_close(
            futures_instance=sell_inst, exchange_name=job.sell_exchange,
            symbol=sell_sym, side='buy', amount=job.sell_qty,
        ))
        return await asyncio.gather(buy_task, sell_task, return_exceptions=True)

    # ------------------------------------------------------------------
    # manual API
    # ------------------------------------------------------------------

    async def enter_manual(
        self,
        symbol: str,
        buy_exchange: str,
        sell_exchange: str,
        notional_usd: Optional[float] = None,
    ) -> dict[str, Any]:
        symbol = str(symbol or '').strip().upper()
        buy_ex = str(buy_exchange or '').strip().lower()
        sell_ex = str(sell_exchange or '').strip().lower()
        if not symbol or not buy_ex or not sell_ex or buy_ex == sell_ex:
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'symbol, buy_exchange, sell_exchange required (distinct)'}
        watch = self._find_watch(symbol)
        if watch is None:
            return {'ok': False, 'code': 'NOT_IN_WATCHLIST',
                    'message': f'{symbol} not in watchlist'}
        quotes = await self._fetch_all_quotes(watch)
        valid = [q for q in quotes if q.is_valid()]
        buy_q = self._find_quote(valid, buy_ex, None)
        sell_q = self._find_quote(valid, sell_ex, None)
        if buy_q is None or sell_q is None:
            return {'ok': False, 'code': 'NO_QUOTES',
                    'message': f'quote unavailable for {buy_ex}/{sell_ex}'}
        if buy_q.ask <= 0:
            return {'ok': False, 'code': 'NO_QUOTES', 'message': 'buy ask <= 0'}
        spread_pct = (sell_q.bid - buy_q.ask) / buy_q.ask * 100.0
        opp = _SpreadOpportunity(
            symbol=symbol, buy_venue=buy_q, sell_venue=sell_q, spread_pct=spread_pct,
        )

        notional_override: Optional[float] = None
        if notional_usd is not None:
            try:
                notional_override = float(notional_usd)
            except (TypeError, ValueError):
                return {'ok': False, 'code': 'INVALID_INPUT',
                        'message': 'notional_usd must be number'}
            if notional_override <= 0 or notional_override > self.cfg.max_notional_usd:
                return {
                    'ok': False, 'code': 'NOTIONAL_OUT_OF_RANGE',
                    'message': (
                        f'notional must be in (0, ${self.cfg.max_notional_usd}]'
                    ),
                }

        key = self._key(symbol, buy_ex, sell_ex)
        if key in {self._key(j.symbol, j.buy_exchange, j.sell_exchange)
                   for j in self._open_jobs()}:
            return {'ok': False, 'code': 'ALREADY_OPEN',
                    'message': f'{key} already has open job'}

        if notional_override is not None:
            prev = self.cfg.notional_usd
            self.cfg.notional_usd = notional_override
            try:
                await self._handle_signal(watch, opp)
            finally:
                self.cfg.notional_usd = prev
        else:
            await self._handle_signal(watch, opp)
        return {
            'ok': True,
            'mode': 'live' if self.cfg.live_armed else 'dry_run',
            'spread_pct': round(spread_pct, 4),
        }

    async def exit_manual(
        self, job_id: str, reason: str = 'manual',
    ) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {'ok': False, 'code': 'NOT_FOUND',
                        'message': f'no job {job_id}'}
            if job.status != 'open':
                return {'ok': False, 'code': 'NOT_OPEN',
                        'message': f'status={job.status}'}
        # 현재 스프레드 재조회
        watch = self._find_watch(job.symbol)
        close_buy = None
        close_sell = None
        close_spread = None
        if watch:
            quotes = await self._fetch_all_quotes(watch)
            valid = [q for q in quotes if q.is_valid()]
            buy_q = self._find_quote(valid, job.buy_exchange, job.buy_symbol)
            sell_q = self._find_quote(valid, job.sell_exchange, job.sell_symbol)
            if buy_q and sell_q and buy_q.ask > 0:
                close_buy = buy_q.ask
                close_sell = sell_q.bid
                close_spread = (sell_q.bid - buy_q.ask) / buy_q.ask * 100.0
        final_status = (
            'closed_win'
            if (close_spread is not None and close_spread <= self.cfg.convergence_exit_pct)
            else 'closing'
        )
        await self._close_job(
            job, reason=reason, final_status=final_status,
            close_buy=close_buy, close_sell=close_sell, close_spread=close_spread,
        )
        return {'ok': True, 'job': job.to_json()}

    def add_watch_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'dict required'}
        try:
            venues_raw = payload.get('venues') or []
            venues: list[CommodityVenue] = []
            for v in venues_raw:
                if not isinstance(v, dict):
                    continue
                ex = str(v.get('exchange') or '').strip().lower()
                sym = str(v.get('symbol') or '').strip()
                if not ex or not sym:
                    continue
                venues.append(CommodityVenue(
                    exchange=ex, symbol=sym,
                    instrument_type=str(v.get('type') or 'perp').strip().lower(),
                    expiry_ts=int(float(v.get('expiry_ts') or 0)),
                ))
            if len(venues) < 2:
                return {'ok': False, 'code': 'INVALID_INPUT',
                        'message': 'at least 2 venues required'}
            entry = CommodityWatch(
                symbol=str(payload.get('symbol') or '').strip().upper(),
                venues=venues,
                min_spread_pct=float(payload.get('min_spread_pct') or 0.0),
                notes=str(payload.get('notes') or ''),
            )
            if not entry.symbol:
                return {'ok': False, 'code': 'INVALID_INPUT',
                        'message': 'symbol required'}
        except (TypeError, ValueError) as exc:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': str(exc)}

        self._watchlist = [w for w in self._watchlist if w.symbol != entry.symbol]
        self._watchlist.append(entry)
        self._persist_watchlist()
        return {'ok': True, 'entry': entry.to_json(), 'count': len(self._watchlist)}

    def remove_watch_entry(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or '').strip().upper()
        if not sym:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'symbol required'}
        before = len(self._watchlist)
        self._watchlist = [w for w in self._watchlist if w.symbol != sym]
        removed = before - len(self._watchlist)
        if removed:
            self._persist_watchlist()
        return {'ok': True, 'removed': removed, 'count': len(self._watchlist)}

    # ------------------------------------------------------------------
    # 유틸
    # ------------------------------------------------------------------

    @staticmethod
    def _key(symbol: str, buy_ex: str, sell_ex: str) -> str:
        return f'{symbol.upper()}|{buy_ex.lower()}|{sell_ex.lower()}'

    def _open_jobs(self) -> list[CommodityBasisJob]:
        return [j for j in self._jobs.values() if j.status == 'open']

    def _find_watch(self, symbol: str) -> Optional[CommodityWatch]:
        s = symbol.upper()
        for w in self._watchlist:
            if w.symbol == s:
                return w
        return None

    @staticmethod
    def _find_venue(
        watch: CommodityWatch, exchange: str, symbol: str,
    ) -> Optional[CommodityVenue]:
        ex = exchange.lower()
        for v in watch.venues:
            if v.exchange == ex and v.symbol == symbol:
                return v
        return None

    @staticmethod
    def _find_quote(
        quotes: list[_VenueQuote], exchange: str, symbol: Optional[str],
    ) -> Optional[_VenueQuote]:
        ex = exchange.lower()
        for q in quotes:
            if q.exchange.lower() != ex:
                continue
            if symbol is None or q.symbol == symbol:
                return q
        return None

    def _can_enter(self, notional_usd: float) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, 'disabled'
        if self._kill_switch_active():
            return False, 'kill_switch_active'
        if notional_usd <= 0:
            return False, 'notional<=0'
        if notional_usd > self.cfg.max_notional_usd:
            return False, (
                f'notional ${notional_usd:.2f} > max ${self.cfg.max_notional_usd:.2f}'
            )
        self._maybe_rollover_daily()
        if self._daily_spent_usd + notional_usd > self.cfg.daily_cap_usd:
            return False, (
                f'daily_cap exceeded ($%.2f + $%.2f > $%.2f)' %
                (self._daily_spent_usd, notional_usd, self.cfg.daily_cap_usd)
            )
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
                '[commodity] daily rollover: spent=$%.2f reset',
                self._daily_spent_usd,
            )
            self._daily_spent_usd = 0.0
            self._daily_reset_epoch = today

    # ------------------------------------------------------------------
    # 파일 IO
    # ------------------------------------------------------------------

    def _load_or_seed_watchlist(self) -> None:
        path = Path(self.cfg.watchlist_path)
        loaded = _load_watchlist_from_file(path)
        if loaded:
            self._watchlist = loaded
            return
        self._watchlist = []
        for item in _BUILTIN_WATCHLIST:
            try:
                venues_raw = item.get('venues') or []
                venues: list[CommodityVenue] = []
                for v in venues_raw:
                    if not isinstance(v, dict):
                        continue
                    ex = str(v.get('exchange') or '').strip().lower()
                    sym = str(v.get('symbol') or '').strip()
                    if not ex or not sym:
                        continue
                    venues.append(CommodityVenue(
                        exchange=ex, symbol=sym,
                        instrument_type=str(v.get('type') or 'perp').strip().lower(),
                        expiry_ts=int(float(v.get('expiry_ts') or 0)),
                    ))
                if len(venues) < 2:
                    continue
                self._watchlist.append(CommodityWatch(
                    symbol=str(item.get('symbol') or '').strip().upper(),
                    venues=venues,
                    min_spread_pct=float(item.get('min_spread_pct') or 0.0),
                    notes=str(item.get('notes') or ''),
                ))
            except (TypeError, ValueError) as exc:
                logger.warning('[commodity] seed parse err: %s', exc)
                continue
        try:
            _save_watchlist_to_file(path, self._watchlist)
            logger.info(
                '[commodity] seeded watchlist → %s (%d entries)',
                path, len(self._watchlist),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('[commodity] seed save err: %s', exc)

    def _persist_watchlist(self) -> None:
        try:
            _save_watchlist_to_file(
                Path(self.cfg.watchlist_path), self._watchlist,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('[commodity] watchlist save err: %s', exc)

    async def _append_jsonl(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception as exc:  # noqa: BLE001
            logger.warning('[commodity] jsonl append err: %s', exc)

    # ------------------------------------------------------------------
    # 알림
    # ------------------------------------------------------------------

    async def _alert_once(self, key: str, text: str) -> None:
        now = time.time()
        last = self._last_alert_ts.get(key, 0.0)
        if (now - last) < self._alert_cooldown_sec:
            return
        self._last_alert_ts[key] = now
        await self._send_telegram(text)

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
            logger.debug('[commodity] telegram err: %s', exc)
