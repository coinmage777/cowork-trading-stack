"""Oracle Formula Divergence Short 스캐너.

배경 (pannpunch msg 387 + plusevdeal strategy 4):
Pre-IPO / 유동성이 낮은 perp 들은 markPrice 오라클이 다음과 비슷한 공식으로 계산된다:

    P_mark = α * P_notice + (1 - α) * EMA_2h(mark_px)

여기에 funding rate cap (예: 0.01%/hour) 이 붙는다. 이 구조는 다음 기회를 만든다:

- 모두가 long → markPrice 가 위로 drift. 하지만 실제 공정가치는 notional / fair value.
- 오라클이 현실을 따라가지 못하는 상태 (lag) → 과대평가된 mark 에 SHORT 진입.
- funding cap 이 작아서 bleed 가 느림 → 대기 비용 저렴.
- Exit: (a) 거래소가 funding cap 을 0.01%/h → 2%/h 등으로 올리는 공식 패치, (b) 실제 IPO / 공정가치 하락.

실전 예시 (plusevdeal):
    SpaceX Ventuals  $592 → $354 short, top PnL ~$100k~150k, 패치 직전 exit $18k.
    Anthropic        $425 → $263 short.
    OpenAI           $783 → $561 short.

이 서비스는 다음을 수행한다:
1. watchlist (JSON + env override) 의 심볼들에 대해 60s 주기로 mark price 폴링.
2. reference_fdv / circulating_supply 로 공정가치 추정 → divergence 계산.
3. divergence >= ORACLE_DIVERGENCE_ENTER_PCT (기본 20%) 이면 SHORT 신호.
4. 실행 (live_armed 일 때만) — CCXT 호환 거래소면 실주문, Ventuals 같은 unsupported 는
   NotImplementedError 로 명시. dry_run 은 로그/알림만 남긴다.
5. 공지 모니터 (bonus): funding formula 변경 키워드를 Bybit/HL 공지 피드에서 스캔하여
   패치 직전 exit.

트리플 락 (실자금):
    ORACLE_DIV_ENABLED=true
    AND ORACLE_DIV_DRY_RUN=false
    AND ORACLE_DIV_LIVE_CONFIRM=true
추가: kill switch file, daily cap, max open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
# 저장 경로
# ----------------------------------------------------------------------


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data'
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_WATCHLIST_PATH = _DATA_DIR / 'oracle_watchlist.json'
_DEFAULT_JOBS_PATH = _DATA_DIR / 'oracle_divergence_jobs.jsonl'
_DEFAULT_ANNOUNCE_CACHE = _DATA_DIR / 'oracle_announcement_cache.json'


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class OracleDivergenceConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False

    enter_pct: float = 20.0       # mark 가 fair 대비 +20% 이상이면 SHORT 신호
    exit_pct: float = 5.0         # divergence 가 +5% 이하로 복귀 시 TP
    stop_loss_pct: float = 30.0   # 진입 후 mark 가 +30% 추가 상승 시 손절
    max_hold_days: int = 14       # 이 시간 넘으면 reference 값이 바뀌었을 수 있음 → 강제 종료

    notional_usd: float = 200.0
    leverage: int = 2             # gap risk 로 낮게
    daily_cap_usd: float = 500.0
    max_open: int = 3

    poll_interval_sec: int = 60
    announcement_poll_interval_sec: int = 900    # 15분마다 공지 폴링
    funding_cap_patch_keywords: tuple[str, ...] = (
        'funding', 'cap', 'mark price', 'oracle', 'formula',
        'pre-ipo', 'pre-market', 'preipo', 'pre_ipo',
    )

    watchlist_path: str = str(_DEFAULT_WATCHLIST_PATH)
    jobs_path: str = str(_DEFAULT_JOBS_PATH)
    announce_cache_path: str = str(_DEFAULT_ANNOUNCE_CACHE)
    kill_switch_file: str = 'data/KILL_ORACLE_DIV'

    @classmethod
    def load(cls) -> 'OracleDivergenceConfig':
        return cls(
            enabled=_env_bool('ORACLE_DIV_ENABLED', True),
            dry_run=_env_bool('ORACLE_DIV_DRY_RUN', True),
            live_confirm=_env_bool('ORACLE_DIV_LIVE_CONFIRM', False),
            enter_pct=_env_float('ORACLE_DIV_ENTER_PCT', 20.0),
            exit_pct=_env_float('ORACLE_DIV_EXIT_PCT', 5.0),
            stop_loss_pct=_env_float('ORACLE_DIV_STOP_LOSS_PCT', 30.0),
            max_hold_days=_env_int('ORACLE_DIV_MAX_HOLD_DAYS', 14),
            notional_usd=_env_float('ORACLE_DIV_NOTIONAL_USD', 200.0),
            leverage=max(_env_int('ORACLE_DIV_LEVERAGE', 2), 1),
            daily_cap_usd=_env_float('ORACLE_DIV_DAILY_CAP_USD', 500.0),
            max_open=_env_int('ORACLE_DIV_MAX_OPEN', 3),
            poll_interval_sec=max(_env_int('ORACLE_DIV_POLL_INTERVAL_SEC', 60), 10),
            announcement_poll_interval_sec=max(
                _env_int('ORACLE_DIV_ANNOUNCE_POLL_SEC', 900), 60,
            ),
            watchlist_path=_env(
                'ORACLE_DIV_WATCHLIST_PATH', str(_DEFAULT_WATCHLIST_PATH)
            ),
            jobs_path=_env('ORACLE_DIV_JOBS_PATH', str(_DEFAULT_JOBS_PATH)),
            announce_cache_path=_env(
                'ORACLE_DIV_ANNOUNCE_CACHE', str(_DEFAULT_ANNOUNCE_CACHE)
            ),
            kill_switch_file=_env(
                'ORACLE_DIV_KILL_SWITCH_FILE', 'data/KILL_ORACLE_DIV'
            ),
        )

    @property
    def live_armed(self) -> bool:
        return self.enabled and (not self.dry_run) and self.live_confirm


# ----------------------------------------------------------------------
# Watchlist 엔트리
# ----------------------------------------------------------------------


@dataclass
class OracleWatchEntry:
    """감시 대상 pre-IPO / 저유동성 perp.

    fair_value 추정:
        reference_fdv / circulating_supply
    (또는 reference_price 를 직접 지정하면 그걸 사용)
    """
    exchange: str                      # 'ventuals' | 'hyperliquid' | 'bybit' | 'ostium'
    symbol: str                        # 'SPACEX' | 'SPACEX-PERP' 등 거래소별 심볼
    reference_price_src: str = 'notional_fdv'   # 'notional_fdv' | 'direct' | 'manual'
    reference_fdv: float = 0.0         # 기업 가치 USD
    circulating_supply: float = 0.0    # 토큰 발행량 (Ventuals synthetic 기준)
    reference_price: float = 0.0       # direct 모드일 때 사용하는 공정가 (USD)
    notional_usd: float = 0.0          # 기업 평가액 (reference_fdv 과 동일 의미 — 호환용)
    notes: str = ''

    def fair_value(self) -> Optional[float]:
        """공정 가치 추정 (USD). 계산 불가면 None."""
        mode = (self.reference_price_src or 'notional_fdv').strip().lower()
        if mode == 'direct':
            return self.reference_price if self.reference_price > 0 else None
        # notional_fdv 기본 경로
        fdv = self.reference_fdv if self.reference_fdv > 0 else self.notional_usd
        if fdv > 0 and self.circulating_supply > 0:
            return fdv / self.circulating_supply
        # 마지막 수단 — reference_price 가 있으면 사용
        return self.reference_price if self.reference_price > 0 else None

    def to_json(self) -> dict[str, Any]:
        return {
            'exchange': self.exchange,
            'symbol': self.symbol,
            'reference_price_src': self.reference_price_src,
            'reference_fdv': self.reference_fdv,
            'circulating_supply': self.circulating_supply,
            'reference_price': self.reference_price,
            'notional_usd': self.notional_usd,
            'notes': self.notes,
        }


# 하드코딩 기본 watchlist — 사용자가 data/oracle_watchlist.json 으로 덮어쓸 수 있음.
# 값은 2026-04 기준 대략적 public valuation. 정확성은 plusevdeal 카탈로그에 위임.
_BUILTIN_WATCHLIST: list[dict[str, Any]] = [
    {
        'exchange': 'ventuals',
        'symbol': 'SPACEX',
        'reference_price_src': 'notional_fdv',
        'reference_fdv': 3.5e11,          # SpaceX 기업가치 약 $350B (private)
        'circulating_supply': 1.5e9,      # Ventuals synthetic supply 가정
        'notional_usd': 3.5e11,
        'notes': 'SpaceX (Ventuals pre-IPO). plusev top trade — short $592→$354',
    },
    {
        'exchange': 'ventuals',
        'symbol': 'ANTHROPIC',
        'reference_price_src': 'notional_fdv',
        'reference_fdv': 1.8e11,
        'circulating_supply': 1.0e9,
        'notional_usd': 1.8e11,
        'notes': 'Anthropic pre-IPO. plusev: $425→$263 short',
    },
    {
        'exchange': 'ventuals',
        'symbol': 'OPENAI',
        'reference_price_src': 'notional_fdv',
        'reference_fdv': 5.0e11,
        'circulating_supply': 1.0e9,
        'notional_usd': 5.0e11,
        'notes': 'OpenAI pre-IPO. plusev: $783→$561 short',
    },
    {
        'exchange': 'hyperliquid',
        'symbol': 'SPACEX',
        'reference_price_src': 'direct',
        'reference_price': 0.0,
        'notes': 'HL pre-IPO perp mirror. reference_price 수동 지정 필요',
    },
    {
        'exchange': 'ostium',
        'symbol': 'BRENT',
        'reference_price_src': 'direct',
        'reference_price': 0.0,
        'notes': 'Ostium Brent oil — reference_price 는 CL/BZ 선물 따라 동적 업데이트 필요',
    },
    {
        'exchange': 'bybit',
        'symbol': 'PREMARKET_SLOT',
        'reference_price_src': 'direct',
        'reference_price': 0.0,
        'notes': 'Bybit pre-market 런치 슬롯 (심볼은 상장 시 업데이트)',
    },
]


def _load_watchlist_from_file(path: Path) -> list[OracleWatchEntry]:
    if not path.exists():
        return []
    try:
        with path.open('r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning('[oracle-div] watchlist load failed: %s', exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[OracleWatchEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get('symbol') or '').strip().upper()
        exchange = str(item.get('exchange') or '').strip().lower()
        if not symbol or not exchange:
            continue
        try:
            entry = OracleWatchEntry(
                exchange=exchange,
                symbol=symbol,
                reference_price_src=str(
                    item.get('reference_price_src') or 'notional_fdv'
                ),
                reference_fdv=float(item.get('reference_fdv') or 0.0),
                circulating_supply=float(item.get('circulating_supply') or 0.0),
                reference_price=float(item.get('reference_price') or 0.0),
                notional_usd=float(item.get('notional_usd') or 0.0),
                notes=str(item.get('notes') or ''),
            )
        except (TypeError, ValueError) as exc:
            logger.warning('[oracle-div] watchlist item parse failed: %s', exc)
            continue
        out.append(entry)
    return out


def _save_watchlist_to_file(path: Path, entries: list[OracleWatchEntry]) -> None:
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
class OracleDivergenceJob:
    job_id: str
    exchange: str
    symbol: str
    mode: str                              # 'live' | 'dry_run'
    entry_mark_price: float
    entry_fair_value: float
    entry_divergence_pct: float
    notional_usd: float
    leverage: int
    qty: float
    created_at: int
    max_hold_ts: int
    status: str = 'open'                   # open | closing | closed_win | closed_stop | closed_timeout | closed_patch | closed_err
    hedge_job_id: Optional[str] = None
    close_mark_price: Optional[float] = None
    close_divergence_pct: Optional[float] = None
    close_reason: Optional[str] = None
    closed_at: Optional[int] = None
    pnl_usd: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'exchange': self.exchange,
            'symbol': self.symbol,
            'mode': self.mode,
            'entry_mark_price': self.entry_mark_price,
            'entry_fair_value': self.entry_fair_value,
            'entry_divergence_pct': self.entry_divergence_pct,
            'notional_usd': self.notional_usd,
            'leverage': self.leverage,
            'qty': self.qty,
            'created_at': self.created_at,
            'max_hold_ts': self.max_hold_ts,
            'status': self.status,
            'hedge_job_id': self.hedge_job_id,
            'close_mark_price': self.close_mark_price,
            'close_divergence_pct': self.close_divergence_pct,
            'close_reason': self.close_reason,
            'closed_at': self.closed_at,
            'pnl_usd': self.pnl_usd,
            'warnings': list(self.warnings),
        }


# ----------------------------------------------------------------------
# 메인 서비스
# ----------------------------------------------------------------------


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


class OracleDivergenceShort:
    """Oracle formula divergence 기반 pre-IPO / 저유동성 perp SHORT 스캐너.

    사용 예::

        service = OracleDivergenceShort(hedge_service=hedge_trade_service, telegram_service=tg)
        await service.start()
        ...
        await service.stop()

    주의:
    - Ventuals / HL HIP-3 pre-IPO 등 실제 실행 인터페이스는 미완성 → NotImplementedError 로 명시.
    - dry_run 경로는 detection + 알림까지 완전 동작. 실자금 결정은 트리플 락 필요.
    """

    def __init__(
        self,
        hedge_service: Any = None,
        telegram_service: Any = None,
        cfg: Optional[OracleDivergenceConfig] = None,
    ) -> None:
        self.cfg = cfg or OracleDivergenceConfig.load()
        self.hedge_service = hedge_service
        self.telegram = telegram_service

        self._jobs: dict[str, OracleDivergenceJob] = {}
        self._lock = asyncio.Lock()

        # 감시/공지/청산 모니터 3개 태스크
        self._scan_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._announce_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # 중복 진입/알림 방지
        self._inflight_keys: set[str] = set()
        self._last_alert_ts: dict[str, float] = {}
        self._alert_cooldown_sec: int = 1800

        # 일일 cap
        self._daily_spent_usd: float = 0.0
        self._daily_reset_epoch: float = _today_midnight_epoch()

        # watchlist: 파일 우선, 없으면 builtin seed 를 파일로 기록하고 사용
        self._watchlist: list[OracleWatchEntry] = []
        self._load_or_seed_watchlist()

        # 공지 스캔 — 이미 본 공지 id 캐시
        self._seen_announcements: set[str] = set()
        self._load_announce_cache()

        # HTTP 클라이언트 (lazy — start 시 생성)
        self._http = None  # type: Optional[Any]

        # 통계
        self._total_scans = 0
        self._total_signals = 0
        self._total_entries = 0
        self._total_dry_run = 0
        self._total_wins = 0
        self._total_stops = 0
        self._total_timeouts = 0
        self._total_patch_exits = 0
        self._total_errors = 0
        self._last_error: str = ''

    # ------------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # lazy import — aiohttp 이 없는 환경에서도 서비스 자체는 import 가능
        try:
            import aiohttp  # type: ignore
            timeout = aiohttp.ClientTimeout(total=15)
            self._http = aiohttp.ClientSession(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[oracle-div] aiohttp unavailable: %s (detection may be degraded)', exc)
            self._http = None

        self._scan_task = asyncio.create_task(
            self._scan_loop(), name='oracle_div_scan',
        )
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name='oracle_div_monitor',
        )
        self._announce_task = asyncio.create_task(
            self._announcement_loop(), name='oracle_div_announce',
        )
        logger.info(
            '[oracle-div] started | enabled=%s dry_run=%s live_confirm=%s '
            'enter>=%.1f%% exit<=%.1f%% stop=%.1f%% notional=$%.0f lev=%dx '
            'daily_cap=$%.0f max_open=%d watch=%d',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.enter_pct, self.cfg.exit_pct, self.cfg.stop_loss_pct,
            self.cfg.notional_usd, self.cfg.leverage, self.cfg.daily_cap_usd,
            self.cfg.max_open, len(self._watchlist),
        )

    async def stop(self) -> None:
        self._running = False
        for t in (self._scan_task, self._monitor_task, self._announce_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug('[oracle-div] task stop err: %s', exc)
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:  # noqa: BLE001
                pass
            self._http = None
        logger.info('[oracle-div] stopped')

    # ------------------------------------------------------------------
    # 상태 조회
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
            'enter_pct': self.cfg.enter_pct,
            'exit_pct': self.cfg.exit_pct,
            'stop_loss_pct': self.cfg.stop_loss_pct,
            'max_hold_days': self.cfg.max_hold_days,
            'notional_usd': self.cfg.notional_usd,
            'leverage': self.cfg.leverage,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self._daily_spent_usd, 2),
            'max_open': self.cfg.max_open,
            'open_jobs_count': len(self._open_jobs()),
            'open_jobs': [j.to_json() for j in self._open_jobs()],
            'watchlist_count': len(self._watchlist),
            'watchlist': [w.to_json() for w in self._watchlist],
            'poll_interval_sec': self.cfg.poll_interval_sec,
            'announce_poll_interval_sec': self.cfg.announcement_poll_interval_sec,
            'total_scans': self._total_scans,
            'total_signals': self._total_signals,
            'total_entries': self._total_entries,
            'total_dry_run': self._total_dry_run,
            'total_wins': self._total_wins,
            'total_stops': self._total_stops,
            'total_timeouts': self._total_timeouts,
            'total_patch_exits': self._total_patch_exits,
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
            logger.debug('[oracle-div] recent_jobs read failed: %s', exc)
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
    # 스캔 루프 — divergence 감지
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
                logger.error('[oracle-div] scan err: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _scan_once(self) -> None:
        self._total_scans += 1
        if self._kill_switch_active():
            logger.debug('[oracle-div] kill switch active, skip scan')
            return
        self._maybe_rollover_daily()
        # 현재 open 중인 (exchange, symbol) 세트 — 중복 진입 방지
        open_keys = {self._key(j.exchange, j.symbol) for j in self._open_jobs()}

        for entry in list(self._watchlist):
            key = self._key(entry.exchange, entry.symbol)
            if key in open_keys:
                continue
            if key in self._inflight_keys:
                continue
            try:
                mark = await self._fetch_mark_price(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    '[oracle-div] %s/%s mark fetch failed: %s',
                    entry.exchange, entry.symbol, exc,
                )
                continue
            if mark is None or mark <= 0:
                continue
            fair = entry.fair_value()
            if fair is None or fair <= 0:
                logger.debug(
                    '[oracle-div] %s/%s skip: no fair value (src=%s)',
                    entry.exchange, entry.symbol, entry.reference_price_src,
                )
                continue
            divergence_pct = (mark - fair) / fair * 100.0
            logger.debug(
                '[oracle-div] %s/%s mark=%.4f fair=%.4f div=%.2f%%',
                entry.exchange, entry.symbol, mark, fair, divergence_pct,
            )
            if divergence_pct < self.cfg.enter_pct:
                continue

            # 신호 발생
            self._total_signals += 1
            await self._handle_signal(entry, mark, fair, divergence_pct)

    async def _handle_signal(
        self,
        entry: OracleWatchEntry,
        mark_price: float,
        fair_value: float,
        divergence_pct: float,
    ) -> None:
        key = self._key(entry.exchange, entry.symbol)
        self._inflight_keys.add(key)
        try:
            # 트리플 락 외에도 개별 게이트 체크
            ok, reason = self._can_enter(self.cfg.notional_usd)
            if not ok:
                logger.info(
                    '[oracle-div] %s/%s signal BLOCKED: %s (div=%.2f%%)',
                    entry.exchange, entry.symbol, reason, divergence_pct,
                )
                await self._alert_once(
                    key=f'block:{key}',
                    text=(
                        f'ℹ️ [oracle-div] signal blocked\n'
                        f'  {entry.exchange}/{entry.symbol} div={divergence_pct:+.2f}% '
                        f'mark={mark_price:.4f} fair={fair_value:.4f}\n'
                        f'  reason={reason}'
                    ),
                )
                return

            if not self.cfg.live_armed:
                await self._record_dry_run(entry, mark_price, fair_value, divergence_pct)
                return

            await self._execute_short(entry, mark_price, fair_value, divergence_pct)
        finally:
            self._inflight_keys.discard(key)

    # ------------------------------------------------------------------
    # 실행 경로
    # ------------------------------------------------------------------

    async def _record_dry_run(
        self,
        entry: OracleWatchEntry,
        mark_price: float,
        fair_value: float,
        divergence_pct: float,
    ) -> None:
        self._total_dry_run += 1
        qty = self.cfg.notional_usd / mark_price if mark_price > 0 else 0.0
        max_hold_ts = int(time.time() + self.cfg.max_hold_days * 86400)
        job = OracleDivergenceJob(
            job_id=f'odiv_{uuid.uuid4().hex[:10]}',
            exchange=entry.exchange,
            symbol=entry.symbol,
            mode='dry_run',
            entry_mark_price=mark_price,
            entry_fair_value=fair_value,
            entry_divergence_pct=divergence_pct,
            notional_usd=self.cfg.notional_usd,
            leverage=self.cfg.leverage,
            qty=qty,
            created_at=int(time.time()),
            max_hold_ts=max_hold_ts,
            status='open',
            warnings=['dry_run'],
        )
        async with self._lock:
            self._jobs[job.job_id] = job
        await self._append_jsonl(job.to_json())
        # dry_run 도 일일 cap 은 태운다 (과도한 dry 신호 억제)
        self._daily_spent_usd += self.cfg.notional_usd
        logger.info(
            '[DRY-ORACLE-DIV] would SHORT %s/%s div=%.2f%% mark=%.4f fair=%.4f '
            'qty=%.6f $%.0f x%d',
            entry.exchange, entry.symbol, divergence_pct, mark_price, fair_value,
            qty, self.cfg.notional_usd, self.cfg.leverage,
        )
        await self._send_telegram(
            f'🧪 [DRY] oracle-div SHORT 신호\n'
            f'  {entry.exchange}/{entry.symbol} div={divergence_pct:+.2f}%\n'
            f'  mark={mark_price:.4f} fair={fair_value:.4f}\n'
            f'  qty={qty:.6f} ${self.cfg.notional_usd:.0f} x{self.cfg.leverage}',
            alert_key='oracle_dry',
        )

    async def _execute_short(
        self,
        entry: OracleWatchEntry,
        mark_price: float,
        fair_value: float,
        divergence_pct: float,
    ) -> None:
        exchange = entry.exchange.lower()

        if exchange in {'ventuals', 'hyperliquid', 'ostium'}:
            # Phase X.1 에서 각 거래소 전용 래퍼/API 키 설정 필요
            msg = (
                f'ventuals/HL HIP-3/ostium integration — Phase X.1 '
                f'(symbol={entry.symbol})'
            )
            logger.error('[oracle-div] %s abort: %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(
                f'⚠️ oracle-div LIVE 미지원 거래소: {entry.exchange}\n'
                f'{msg}'
            )
            # 의도적으로 raise — 상위 handler 가 inflight 해제 후 다음 tick 기다림
            raise NotImplementedError(
                f'ventuals integration — Phase X.1 (got exchange={exchange})'
            )

        if self.hedge_service is None:
            msg = 'hedge_service is None'
            logger.error('[oracle-div] %s abort: %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        qty = self.cfg.notional_usd / mark_price if mark_price > 0 else 0.0
        if qty <= 0:
            msg = f'qty<=0 mark={mark_price}'
            logger.error('[oracle-div] %s abort: %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        max_hold_ts = int(time.time() + self.cfg.max_hold_days * 86400)
        job = OracleDivergenceJob(
            job_id=f'odiv_{uuid.uuid4().hex[:10]}',
            exchange=exchange,
            symbol=entry.symbol,
            mode='live',
            entry_mark_price=mark_price,
            entry_fair_value=fair_value,
            entry_divergence_pct=divergence_pct,
            notional_usd=self.cfg.notional_usd,
            leverage=self.cfg.leverage,
            qty=qty,
            created_at=int(time.time()),
            max_hold_ts=max_hold_ts,
            status='open',
        )

        # CCXT-기반 거래소 (bybit 등) — hedge_service 의 헬퍼를 재사용
        try:
            from backend.exchanges import manager as exchange_manager
        except Exception as exc:  # noqa: BLE001
            msg = f'exchange_manager import failed: {exc}'
            logger.error('[oracle-div] %s abort: %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            return

        instance = exchange_manager.get_instance(exchange, 'swap')
        if instance is None:
            msg = f'{exchange} swap instance unavailable'
            logger.error('[oracle-div] %s abort: %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(f'⚠️ oracle-div {entry.symbol} {msg}')
            return

        # symbol 변환 — Bybit pre-market 은 심볼 규칙이 다를 수 있어 raw-first fallback
        try:
            symbol_futures = exchange_manager.get_symbol(
                ticker=entry.symbol,
                market_type='swap',
                exchange_id=exchange,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                '[oracle-div] get_symbol fallback for %s/%s: %s',
                exchange, entry.symbol, exc,
            )
            symbol_futures = entry.symbol

        try:
            if not getattr(instance, 'markets', None):
                await instance.load_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                '[oracle-div] %s load_markets failed: %s', exchange, exc,
            )

        # qty 정규화
        try:
            if hasattr(instance, 'amount_to_precision'):
                qty = float(instance.amount_to_precision(symbol_futures, qty))
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] normalize qty fallback: %s', exc)

        # 레버리지 설정 (best-effort)
        warnings: list[str] = []
        try:
            prepare = getattr(self.hedge_service, '_prepare_futures_account', None)
            if callable(prepare):
                warnings = await prepare(
                    exchange_instance=instance,
                    symbol=symbol_futures,
                    leverage=self.cfg.leverage,
                ) or []
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'leverage prep failed: {exc}')
            logger.warning('[oracle-div] %s leverage prep: %s', entry.symbol, exc)

        # SHORT 시장가 오픈 (reduceOnly=False)
        try:
            submit = getattr(self.hedge_service, '_submit_market_order', None)
            if not callable(submit):
                raise RuntimeError('hedge_service._submit_market_order unavailable')
            order_result = await submit(
                exchange_instance=instance,
                exchange_name=exchange,
                symbol=symbol_futures,
                side='sell',
                amount=qty,
                market='futures',
                reference_price=mark_price,
            )
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f'submit_market_order exc: {exc}'
            logger.exception('[oracle-div] %s %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            await self._send_telegram(f'⚠️ oracle-div {entry.symbol} {msg}')
            return

        filled_qty = float(order_result.get('filled_qty') or 0.0)
        avg_price = order_result.get('avg_price') or mark_price
        status = str(order_result.get('status') or '').lower()
        err = order_result.get('error')

        if filled_qty <= 0 or err or status not in {'closed', 'filled', 'ok'}:
            msg = f'order failed status={status} filled={filled_qty} err={err}'
            logger.error('[oracle-div] %s %s', entry.symbol, msg)
            self._total_errors += 1
            self._last_error = msg
            job.warnings.append(msg)
            job.status = 'closed_err'
            job.closed_at = int(time.time())
            job.close_reason = 'order_failed'
            await self._append_jsonl(job.to_json())
            await self._send_telegram(f'⚠️ oracle-div {entry.symbol} {msg}')
            return

        job.qty = filled_qty
        job.entry_mark_price = float(avg_price)
        job.warnings = list(warnings)
        job.hedge_job_id = str(order_result.get('order_id') or '') or None

        async with self._lock:
            self._jobs[job.job_id] = job

        self._total_entries += 1
        self._daily_spent_usd += self.cfg.notional_usd
        await self._append_jsonl(job.to_json())
        logger.info(
            '[oracle-div] LIVE SHORT %s/%s @%.4f qty=%.6f div=%.2f%%',
            exchange, entry.symbol, float(avg_price), filled_qty, divergence_pct,
        )
        await self._send_telegram(
            f'🩳 oracle-div SHORT 진입\n'
            f'  {exchange}/{entry.symbol} @{float(avg_price):.4f}\n'
            f'  div={divergence_pct:+.2f}% fair={fair_value:.4f}\n'
            f'  qty={filled_qty:.6f} ${self.cfg.notional_usd:.0f} x{self.cfg.leverage}'
        )

    # ------------------------------------------------------------------
    # 청산 모니터 (exit / stop / timeout / patch)
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._monitor_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error('[oracle-div] monitor err: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _monitor_tick(self) -> None:
        async with self._lock:
            open_jobs = self._open_jobs()
        for job in open_jobs:
            try:
                await self._check_job(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    '[oracle-div] check_job %s err: %s',
                    job.job_id, exc, exc_info=True,
                )

    async def _check_job(self, job: OracleDivergenceJob) -> None:
        now = int(time.time())

        # 1) timeout 최우선
        if now >= job.max_hold_ts:
            await self._close_job(
                job, reason='timeout',
                final_status='closed_timeout',
                close_mark=None, close_divergence=None,
            )
            self._total_timeouts += 1
            return

        # 2) 현재 mark 조회
        entry = self._find_watch(job.exchange, job.symbol)
        if entry is None:
            # watchlist 에서 빠졌으면 공정가 재추정 불가 — fair 는 job 진입시 값으로 대체
            logger.debug(
                '[oracle-div] job %s watch entry missing, using entry_fair',
                job.job_id,
            )
        try:
            mark = await self._fetch_mark_price_from_job(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                '[oracle-div] monitor mark fetch failed %s/%s: %s',
                job.exchange, job.symbol, exc,
            )
            return
        if mark is None or mark <= 0:
            return

        fair = (entry.fair_value() if entry else None) or job.entry_fair_value
        if fair <= 0:
            return
        divergence_pct = (mark - fair) / fair * 100.0

        # 3) TP — divergence <= exit_pct
        if divergence_pct <= self.cfg.exit_pct:
            await self._close_job(
                job, reason='target_converged',
                final_status='closed_win',
                close_mark=mark, close_divergence=divergence_pct,
            )
            self._total_wins += 1
            return

        # 4) SL — mark 가 entry 대비 stop_loss_pct 이상 상승
        if job.entry_mark_price > 0:
            adverse_pct = (mark - job.entry_mark_price) / job.entry_mark_price * 100.0
            if adverse_pct >= self.cfg.stop_loss_pct:
                await self._close_job(
                    job, reason=f'stop_loss_mark_{adverse_pct:.1f}%',
                    final_status='closed_stop',
                    close_mark=mark, close_divergence=divergence_pct,
                )
                self._total_stops += 1
                return

    # ------------------------------------------------------------------
    # 공지 모니터 — funding cap patch 감지
    # ------------------------------------------------------------------

    async def _announcement_loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enabled:
                    await self._announcement_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug('[oracle-div] announcement err: %s', exc)
            await asyncio.sleep(self.cfg.announcement_poll_interval_sec)

    async def _announcement_tick(self) -> None:
        """Bybit / HL / Ventuals 공지 피드에서 funding cap 관련 키워드 감지.

        감지 시: 해당 거래소의 open short 들을 '패치 직전' 으로 간주하여 일괄 청산.
        """
        hits = await self._fetch_announcements()
        for hit in hits:
            note_id = hit.get('id') or f'{hit.get("exchange")}|{hit.get("title")}'
            if not note_id or note_id in self._seen_announcements:
                continue
            self._seen_announcements.add(note_id)
            logger.warning(
                '[oracle-div] funding/oracle patch 추정 공지 감지: %s | %s',
                hit.get('exchange'), hit.get('title'),
            )
            await self._send_telegram(
                f'🚨 [oracle-div] 공지 감지 (funding/oracle 패치 가능성)\n'
                f'  {hit.get("exchange")} | {hit.get("title")}\n'
                f'  → 해당 거래소 open short 즉시 청산 시도'
            )
            await self._close_all_for_exchange(
                str(hit.get('exchange') or '').lower(), reason='announcement_patch',
            )
        self._save_announce_cache()

    async def _fetch_announcements(self) -> list[dict[str, Any]]:
        """Bybit v5 announcements + HL forum (best-effort). 실패해도 무시."""
        out: list[dict[str, Any]] = []
        if self._http is None:
            return out
        # Bybit v5 announcements — public endpoint
        try:
            url = 'https://api.bybit.com/v5/announcements/index?locale=en-US&limit=30'
            async with self._http.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = ((data or {}).get('result') or {}).get('list') or []
                    for item in result:
                        title = str(item.get('title') or '')
                        if self._matches_patch_keywords(title):
                            out.append({
                                'id': f'bybit|{item.get("id") or item.get("url") or title}',
                                'exchange': 'bybit',
                                'title': title,
                                'url': item.get('url') or '',
                            })
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] bybit announce err: %s', exc)
        return out

    def _matches_patch_keywords(self, text: str) -> bool:
        lowered = (text or '').lower()
        for kw in self.cfg.funding_cap_patch_keywords:
            if kw.lower() in lowered:
                return True
        return False

    def _load_announce_cache(self) -> None:
        path = Path(self.cfg.announce_cache_path)
        if not path.exists():
            return
        try:
            with path.open('r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, list):
                self._seen_announcements = {str(x) for x in raw}
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] announce cache load err: %s', exc)

    def _save_announce_cache(self) -> None:
        path = Path(self.cfg.announce_cache_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 캐시 크기 제한 — 최근 500개만 유지
            to_save = list(self._seen_announcements)[-500:]
            tmp = path.with_suffix('.json.tmp')
            with tmp.open('w', encoding='utf-8') as f:
                json.dump(to_save, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] announce cache save err: %s', exc)

    # ------------------------------------------------------------------
    # 수동 API — enter / exit / watchlist
    # ------------------------------------------------------------------

    async def enter_manual(
        self,
        symbol: str,
        exchange: str,
        notional_usd: Optional[float] = None,
    ) -> dict[str, Any]:
        """수동 SHORT 진입. watchlist 에 심볼이 있어야 fair_value 계산 가능."""
        symbol = str(symbol or '').strip().upper()
        exchange = str(exchange or '').strip().lower()
        if not symbol or not exchange:
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'symbol, exchange required'}
        entry = self._find_watch(exchange, symbol)
        if entry is None:
            return {
                'ok': False, 'code': 'NOT_IN_WATCHLIST',
                'message': f'{exchange}/{symbol} not in watchlist (add first)',
            }
        try:
            mark = await self._fetch_mark_price(entry)
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'MARK_FETCH_FAIL',
                    'message': f'{type(exc).__name__}: {exc}'}
        if mark is None or mark <= 0:
            return {'ok': False, 'code': 'NO_MARK', 'message': 'mark price unavailable'}
        fair = entry.fair_value()
        if fair is None or fair <= 0:
            return {'ok': False, 'code': 'NO_FAIR', 'message': 'fair value unavailable'}
        divergence_pct = (mark - fair) / fair * 100.0

        if notional_usd is not None:
            try:
                override = float(notional_usd)
                if override > 0:
                    # 임시 override — 원 cfg 는 변경하지 않고 한번만 사용
                    prev = self.cfg.notional_usd
                    self.cfg.notional_usd = override
                    try:
                        await self._handle_signal(entry, mark, fair, divergence_pct)
                    finally:
                        self.cfg.notional_usd = prev
                    return {'ok': True, 'mode': 'live' if self.cfg.live_armed else 'dry_run',
                            'divergence_pct': round(divergence_pct, 4)}
            except (TypeError, ValueError):
                pass
        await self._handle_signal(entry, mark, fair, divergence_pct)
        return {
            'ok': True,
            'mode': 'live' if self.cfg.live_armed else 'dry_run',
            'divergence_pct': round(divergence_pct, 4),
        }

    async def exit_manual(self, job_id: str, reason: str = 'manual') -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {'ok': False, 'code': 'NOT_FOUND', 'message': f'no job {job_id}'}
            if job.status != 'open':
                return {'ok': False, 'code': 'NOT_OPEN', 'message': f'status={job.status}'}
        # mark 재조회
        try:
            mark = await self._fetch_mark_price_from_job(job)
        except Exception:  # noqa: BLE001
            mark = None
        divergence = None
        if mark and mark > 0 and job.entry_fair_value > 0:
            divergence = (mark - job.entry_fair_value) / job.entry_fair_value * 100.0
        await self._close_job(
            job, reason=reason, final_status='closed_win' if (
                divergence is not None and divergence <= self.cfg.exit_pct
            ) else 'closing',
            close_mark=mark, close_divergence=divergence,
        )
        return {'ok': True, 'job': job.to_json()}

    def add_watch_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            entry = OracleWatchEntry(
                exchange=str(payload.get('exchange') or '').strip().lower(),
                symbol=str(payload.get('symbol') or '').strip().upper(),
                reference_price_src=str(
                    payload.get('reference_price_src') or 'notional_fdv'
                ),
                reference_fdv=float(payload.get('reference_fdv') or 0.0),
                circulating_supply=float(payload.get('circulating_supply') or 0.0),
                reference_price=float(payload.get('reference_price') or 0.0),
                notional_usd=float(payload.get('notional_usd') or 0.0),
                notes=str(payload.get('notes') or ''),
            )
        except (TypeError, ValueError) as exc:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': str(exc)}
        if not entry.exchange or not entry.symbol:
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'exchange and symbol required'}
        # 중복은 overwrite
        self._watchlist = [
            w for w in self._watchlist
            if not (w.exchange == entry.exchange and w.symbol == entry.symbol)
        ]
        self._watchlist.append(entry)
        self._persist_watchlist()
        return {'ok': True, 'entry': entry.to_json(),
                'count': len(self._watchlist)}

    def remove_watch_entry(self, symbol: str, exchange: Optional[str] = None) -> dict[str, Any]:
        symbol_up = str(symbol or '').strip().upper()
        ex_low = str(exchange or '').strip().lower()
        if not symbol_up:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'symbol required'}
        before = len(self._watchlist)
        self._watchlist = [
            w for w in self._watchlist
            if w.symbol != symbol_up or (ex_low and w.exchange != ex_low)
        ]
        removed = before - len(self._watchlist)
        if removed:
            self._persist_watchlist()
        return {'ok': True, 'removed': removed, 'count': len(self._watchlist)}

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------

    def _key(self, exchange: str, symbol: str) -> str:
        return f'{exchange.lower()}|{symbol.upper()}'

    def _open_jobs(self) -> list[OracleDivergenceJob]:
        return [j for j in self._jobs.values() if j.status == 'open']

    def _find_watch(self, exchange: str, symbol: str) -> Optional[OracleWatchEntry]:
        for w in self._watchlist:
            if w.exchange == exchange and w.symbol == symbol:
                return w
        return None

    def _can_enter(self, notional_usd: float) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, 'disabled'
        if self._kill_switch_active():
            return False, 'kill_switch_active'
        self._maybe_rollover_daily()
        if self._daily_spent_usd + notional_usd > self.cfg.daily_cap_usd:
            return False, (
                f'daily_cap exceeded ($%.2f + $%.2f > $%.2f)' %
                (self._daily_spent_usd, notional_usd, self.cfg.daily_cap_usd)
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
                '[oracle-div] daily rollover: spent=$%.2f reset',
                self._daily_spent_usd,
            )
            self._daily_spent_usd = 0.0
            self._daily_reset_epoch = today

    # ------------------------------------------------------------------
    # 가격 조회 — 거래소별 분기
    # ------------------------------------------------------------------

    async def _fetch_mark_price(self, entry: OracleWatchEntry) -> Optional[float]:
        exchange = entry.exchange.lower()
        symbol = entry.symbol
        if exchange == 'hyperliquid':
            return await self._fetch_hyperliquid_mark(symbol)
        if exchange == 'ventuals':
            return await self._fetch_ventuals_mark(symbol)
        if exchange == 'bybit':
            return await self._fetch_bybit_mark(symbol)
        if exchange == 'ostium':
            return await self._fetch_ostium_mark(symbol)
        logger.debug('[oracle-div] unsupported exchange for mark: %s', exchange)
        return None

    async def _fetch_mark_price_from_job(
        self, job: OracleDivergenceJob
    ) -> Optional[float]:
        entry = self._find_watch(job.exchange, job.symbol)
        if entry is not None:
            return await self._fetch_mark_price(entry)
        # watchlist 에서 제거된 경우 — 임시 entry 로 다시 시도
        tmp = OracleWatchEntry(exchange=job.exchange, symbol=job.symbol)
        return await self._fetch_mark_price(tmp)

    async def _fetch_hyperliquid_mark(self, symbol: str) -> Optional[float]:
        """HL info API — /info metaAndAssetCtxs 에서 markPx 추출."""
        if self._http is None:
            return None
        try:
            url = 'https://api.hyperliquid.xyz/info'
            async with self._http.post(
                url, json={'type': 'metaAndAssetCtxs'},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] HL mark err %s: %s', symbol, exc)
            return None
        try:
            # data = [meta, [asset_ctx, ...]]
            meta = data[0] if isinstance(data, list) and data else {}
            ctxs = data[1] if isinstance(data, list) and len(data) > 1 else []
            universe = (meta or {}).get('universe') or []
            for idx, uni in enumerate(universe):
                name = str(uni.get('name') or '').upper()
                if name == symbol.upper() and idx < len(ctxs):
                    mark_raw = ctxs[idx].get('markPx') or ctxs[idx].get('markPrice')
                    if mark_raw is None:
                        return None
                    val = float(mark_raw)
                    return val if val > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] HL parse err %s: %s', symbol, exc)
        return None

    async def _fetch_ventuals_mark(self, symbol: str) -> Optional[float]:
        """Ventuals public mark price — 공식 API 스펙 미공개 → best-effort 엔드포인트 시도.

        엔드포인트 override: 환경변수 VENTUALS_MARK_URL=https://.../markets/{symbol}
        응답에서 'markPrice' | 'mark_px' | 'price' 키를 탐색.
        """
        if self._http is None:
            return None
        override = _env('VENTUALS_MARK_URL')
        urls: list[str] = []
        if override:
            urls.append(override.replace('{symbol}', symbol).replace('{SYMBOL}', symbol))
        urls.extend([
            f'https://api.ventuals.xyz/v1/markets/{symbol}',
            f'https://trade.ventuals.xyz/api/markets/{symbol}',
        ])
        for url in urls:
            try:
                async with self._http.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[oracle-div] ventuals try %s: %s', url, exc)
                continue
            # 다양한 스키마 대응
            candidates = []
            if isinstance(data, dict):
                for key in ('markPrice', 'mark_px', 'markPx', 'price', 'oraclePrice'):
                    if data.get(key) is not None:
                        candidates.append(data[key])
                nested = data.get('data') if isinstance(data.get('data'), dict) else None
                if nested:
                    for key in ('markPrice', 'mark_px', 'markPx', 'price'):
                        if nested.get(key) is not None:
                            candidates.append(nested[key])
            for c in candidates:
                try:
                    v = float(c)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    continue
        return None

    async def _fetch_bybit_mark(self, symbol: str) -> Optional[float]:
        """Bybit v5 linear perp mark price."""
        if self._http is None:
            return None
        try:
            url = (
                f'https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}'
            )
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] bybit mark err %s: %s', symbol, exc)
            return None
        try:
            lst = ((data or {}).get('result') or {}).get('list') or []
            if not lst:
                return None
            mark_raw = lst[0].get('markPrice') or lst[0].get('lastPrice')
            if mark_raw is None:
                return None
            v = float(mark_raw)
            return v if v > 0 else None
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] bybit parse err %s: %s', symbol, exc)
            return None

    async def _fetch_ostium_mark(self, symbol: str) -> Optional[float]:
        """Ostium — public price feed (best-effort). 필요 시 OSTIUM_MARK_URL override."""
        override = _env('OSTIUM_MARK_URL')
        if not override or self._http is None:
            return None
        url = override.replace('{symbol}', symbol).replace('{SYMBOL}', symbol)
        try:
            async with self._http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[oracle-div] ostium mark err %s: %s', symbol, exc)
            return None
        if isinstance(data, dict):
            for key in ('markPrice', 'mark_px', 'price'):
                if data.get(key) is not None:
                    try:
                        v = float(data[key])
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        continue
        return None

    # ------------------------------------------------------------------
    # 청산 헬퍼
    # ------------------------------------------------------------------

    async def _close_job(
        self,
        job: OracleDivergenceJob,
        reason: str,
        final_status: str,
        close_mark: Optional[float],
        close_divergence: Optional[float],
    ) -> None:
        async with self._lock:
            if job.status != 'open':
                return
            job.status = 'closing'

        pnl_usd: Optional[float] = None
        live_closed = False

        if job.mode == 'live' and self.hedge_service is not None:
            try:
                result = await self._live_close_job(job)
                close_filled = float((result or {}).get('filled_qty') or 0.0)
                close_avg = (result or {}).get('avg_price')
                if close_filled > 0 and close_avg:
                    live_closed = True
                    if job.entry_mark_price > 0:
                        # SHORT: PnL = (entry - close) * qty
                        pnl_usd = round(
                            (job.entry_mark_price - float(close_avg)) * close_filled,
                            6,
                        )
                    if close_mark is None:
                        close_mark = float(close_avg)
            except NotImplementedError as exc:
                logger.warning('[oracle-div] close unsupported: %s', exc)
                job.warnings.append(f'close_not_implemented: {exc}')
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    '[oracle-div] live close failed for %s: %s', job.job_id, exc,
                )
                job.warnings.append(f'close_exc: {exc}')

        # mark 기반 가상 PnL 계산 (dry_run / live 모두)
        if pnl_usd is None and close_mark and close_mark > 0 and job.entry_mark_price > 0 and job.qty > 0:
            pnl_usd = round(
                (job.entry_mark_price - close_mark) * job.qty,
                6,
            )

        async with self._lock:
            job.status = final_status
            job.closed_at = int(time.time())
            job.close_reason = reason
            job.close_mark_price = close_mark
            job.close_divergence_pct = close_divergence
            job.pnl_usd = pnl_usd
        await self._append_jsonl(job.to_json())
        logger.info(
            '[oracle-div] CLOSE %s %s/%s reason=%s mark=%s pnl=%s (live_closed=%s)',
            job.job_id, job.exchange, job.symbol, reason,
            close_mark, pnl_usd, live_closed,
        )
        emoji = '✅' if final_status == 'closed_win' else (
            '🟠' if final_status == 'closed_timeout' else '🛑'
        )
        await self._send_telegram(
            f'{emoji} oracle-div CLOSE {job.exchange}/{job.symbol}\n'
            f'  reason={reason} status={final_status}\n'
            f'  entry={job.entry_mark_price:.4f} close={close_mark} pnl={pnl_usd}'
        )

    async def _live_close_job(
        self, job: OracleDivergenceJob,
    ) -> dict[str, Any]:
        """SHORT → BUY reduceOnly 로 청산 (CCXT 호환 거래소만)."""
        exchange = job.exchange.lower()
        if exchange in {'ventuals', 'hyperliquid', 'ostium'}:
            raise NotImplementedError(
                f'ventuals integration — Phase X.1 (close for {exchange})'
            )
        try:
            from backend.exchanges import manager as exchange_manager
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f'exchange_manager import failed: {exc}') from exc

        instance = exchange_manager.get_instance(exchange, 'swap')
        if instance is None:
            raise RuntimeError(f'{exchange} swap instance unavailable')
        try:
            if not getattr(instance, 'markets', None):
                await instance.load_markets()
        except Exception:  # noqa: BLE001
            pass
        try:
            symbol = exchange_manager.get_symbol(
                ticker=job.symbol, market_type='swap', exchange_id=exchange,
            )
        except Exception:  # noqa: BLE001
            symbol = job.symbol
        submit_close = getattr(
            self.hedge_service, '_submit_futures_close_generic_reduce_only', None,
        )
        if not callable(submit_close):
            raise RuntimeError(
                'hedge_service._submit_futures_close_generic_reduce_only unavailable'
            )
        return await submit_close(
            futures_instance=instance,
            exchange_name=exchange,
            symbol=symbol,
            side='buy',
            amount=job.qty,
        )

    async def _close_all_for_exchange(self, exchange: str, reason: str) -> None:
        if not exchange:
            return
        async with self._lock:
            targets = [
                j for j in self._jobs.values()
                if j.status == 'open' and j.exchange.lower() == exchange
            ]
        for job in targets:
            try:
                await self._close_job(
                    job, reason=reason, final_status='closed_patch',
                    close_mark=None, close_divergence=None,
                )
                self._total_patch_exits += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning('[oracle-div] patch close %s err: %s', job.job_id, exc)

    # ------------------------------------------------------------------
    # 파일 IO
    # ------------------------------------------------------------------

    def _load_or_seed_watchlist(self) -> None:
        path = Path(self.cfg.watchlist_path)
        loaded = _load_watchlist_from_file(path)
        if loaded:
            self._watchlist = loaded
            return
        # seed
        self._watchlist = []
        for item in _BUILTIN_WATCHLIST:
            self._watchlist.append(OracleWatchEntry(
                exchange=str(item.get('exchange') or '').lower(),
                symbol=str(item.get('symbol') or '').upper(),
                reference_price_src=str(item.get('reference_price_src') or 'notional_fdv'),
                reference_fdv=float(item.get('reference_fdv') or 0.0),
                circulating_supply=float(item.get('circulating_supply') or 0.0),
                reference_price=float(item.get('reference_price') or 0.0),
                notional_usd=float(item.get('notional_usd') or 0.0),
                notes=str(item.get('notes') or ''),
            ))
        try:
            _save_watchlist_to_file(path, self._watchlist)
            logger.info('[oracle-div] seeded watchlist → %s (%d entries)',
                        path, len(self._watchlist))
        except Exception as exc:  # noqa: BLE001
            logger.warning('[oracle-div] watchlist seed save err: %s', exc)

    def _persist_watchlist(self) -> None:
        try:
            _save_watchlist_to_file(Path(self.cfg.watchlist_path), self._watchlist)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[oracle-div] watchlist save err: %s', exc)

    async def _append_jsonl(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 파일 I/O 는 동기라 짧게 유지 — to_thread 로 offload 는 생략 (payload 작음)
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception as exc:  # noqa: BLE001
            logger.warning('[oracle-div] jsonl append err: %s', exc)

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
            logger.debug('[oracle-div] telegram err: %s', exc)


# ----------------------------------------------------------------------
# 정규표현식 검증 (모듈 import 시 1회 실행) — 키워드 안전성 체크
# ----------------------------------------------------------------------

# 키워드 목록이 소문자 문자열이 맞는지 sanity check
_valid_kw_pat = re.compile(r'^[a-z0-9_\- ]+$')
for _kw in OracleDivergenceConfig().funding_cap_patch_keywords:
    assert _valid_kw_pat.match(_kw), f'invalid patch keyword: {_kw!r}'
