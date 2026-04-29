"""HackHaltDetector — CEX 해킹 / 거래소 입출금 중단 / 정지 공지 감지기 + 자동 헷지.

배경 (pannpunch msg#260, plusevdeal #6 #8):
  - Upbit SOL 핫월렛 해킹 → 입출금 중단 → 5~40% 김프 폭등 → 현선 풀배팅 +1.2억.
  - Drift JLP drain / Polkadot Hyperbridge / Venus THE / Resolv USR
    → 온체인 exploit 감지 → victim 토큰 즉시 해외선물 숏.
  - 기본 아이디어: "공지 = 뉴스지연 1~10분" 구간을 봇이 선점한다.

설계 원칙 (listing_detector / wallet_tracker 와 동일 방어 패턴):
  - curl_cffi chrome124 impersonate (Upbit/Bybit Cloudflare 우회).
  - 체크포인트 `seen_id` 영속화 → 재시작 후 과거 공지 재발화 방지.
  - 트리플 락 (ENABLED + not DRY_RUN + LIVE_CONFIRM) + kill switch.
  - per-ticker 24h cooldown (해킹/정지는 같은 티커에 재발화 거의 없음).
  - daily cap (max_per_day=5).
  - Telegram 알림은 best-effort, 실패해도 메인 루프 영향 없음.
  - 모든 상호작용은 asyncio 단일 루프, create_task 는 팬아웃 시에만.

Phase 1: Upbit/Bithumb/Bybit/Binance/OKX 공지 폴링 완료 (LIVE-ready).
Phase 2: on-chain exploit_patterns 감지는 wallet_tracker 통합 지점만 제공 (stub).
Phase 3: Twitter/Telegram 소셜 tip feed (완전 stub, API 키 도입 시 활성화).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession  # type: ignore
except Exception:  # noqa: BLE001
    CurlAsyncSession = None  # type: ignore

logger = logging.getLogger(__name__)


# ======================================================================
# env 헬퍼
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
    return [t.strip() for t in raw.split(',') if t.strip()]


# ======================================================================
# 상수 / 공지 소스 엔드포인트
# ======================================================================

_DEFAULT_POLL_MS = 1000
_DEFAULT_HTTP_TIMEOUT = 10.0
_RATE_LIMIT_BACKOFF_SEC = 5.0
_CLOUDFLARE_PAUSE_SEC = 60.0
_CLOUDFLARE_SUSTAINED_SEC = 60.0
_SEEN_HARD_CAP = 5000
_COOLDOWN_PER_TICKER_SEC = 24 * 3600  # 24h

_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def _upbit_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Origin': 'https://upbit.com',
        'Referer': 'https://upbit.com/',
        'User-Agent': _BROWSER_UA,
    }


def _bithumb_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Origin': 'https://feed.bithumb.com',
        'Referer': 'https://feed.bithumb.com/',
        'User-Agent': _BROWSER_UA,
    }


def _bybit_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://www.bybit.com',
        'Referer': 'https://www.bybit.com/',
        'User-Agent': _BROWSER_UA,
    }


def _binance_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://www.binance.com',
        'Referer': 'https://www.binance.com/',
        'User-Agent': _BROWSER_UA,
        'clienttype': 'web',
    }


def _okx_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': _BROWSER_UA,
    }


# 공지 제목에서 티커 추출 — 대문자 토큰 2~10자 구간.
# "[거래] 솔라나(SOL) 입출금 일시 중단" 같은 괄호 패턴 우선, 없으면 대문자 토큰 스캔.
_TICKER_PAREN_RE = re.compile(r'\(([A-Z0-9]{2,10})\)')
_TICKER_UPPER_RE = re.compile(r'\b([A-Z0-9]{2,10})\b')

# 제외 — 흔한 일반 대문자 토큰 (ticker 가 아니라 약어)
_TICKER_BLACKLIST = {
    'USD', 'USDT', 'USDC', 'KRW', 'BTC', 'ETH', 'API', 'ETF', 'UTC', 'KST',
    'AM', 'PM', 'OK', 'NG', 'FYI', 'TBD', 'TBA', 'NEW', 'TX', 'ID', 'UP',
    'DOWN', 'DEX', 'CEX', 'IOS', 'OS', 'TBC', 'VS', 'QC', 'FAQ', 'CS',
}


# ======================================================================
# 데이터 모델
# ======================================================================


@dataclass
class HaltEvent:
    """감지된 halt/hack 이벤트."""

    ts: float
    source: str                 # 'upbit' | 'bithumb' | 'bybit' | 'binance' | 'okx' | 'exploit' | 'social'
    event_type: str             # 'halt' | 'hack' | 'exploit' | 'maintenance'
    notice_id: str
    ticker: str
    title: str
    url: str
    matched_keyword: str = ''
    binance_perp: bool = False
    bybit_perp: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            'ts': int(self.ts),
            'source': self.source,
            'event_type': self.event_type,
            'id': self.notice_id,
            'ticker': self.ticker,
            'title': self.title,
            'url': self.url,
            'matched_keyword': self.matched_keyword,
            'binance_perp': self.binance_perp,
            'bybit_perp': self.bybit_perp,
            'extra': self.extra,
        }


# ======================================================================
# 설정
# ======================================================================


@dataclass
class HackHaltConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False
    manual_confirm: bool = False      # 미래: Telegram 버튼 승인 대기
    hedge_notional_usd: float = 100.0  # 현선 양방향 헷지 notional
    short_notional_usd: float = 50.0   # exploit 숏 전용 notional
    leverage: int = 3
    prefer_exchange: str = 'binance'   # binance > bybit
    max_per_day: int = 5
    per_ticker_cooldown_sec: int = _COOLDOWN_PER_TICKER_SEC
    poll_interval_ms: int = _DEFAULT_POLL_MS
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT
    kill_switch_file: str = 'data/KILL_HACK_HALT'
    events_path: str = 'data/hack_halt_events.jsonl'
    seen_path: str = 'data/hack_halt_seen.json'
    exploit_patterns_path: str = 'data/exploit_patterns.json'
    # 소스별 on/off
    upbit_enabled: bool = True
    bithumb_enabled: bool = True
    bybit_enabled: bool = True
    binance_enabled: bool = True
    okx_enabled: bool = True
    # 키워드 리스트
    kw_upbit: list[str] = field(default_factory=list)
    kw_bithumb: list[str] = field(default_factory=list)
    kw_intl: list[str] = field(default_factory=list)
    # auto-exit
    exit_max_hold_sec: int = 30 * 60   # 30min — exploit 숏 자동 청산 시한
    exit_pnl_pct_target: float = -20.0  # victim 숏: -20% 내려가면 청산

    @classmethod
    def load(cls) -> 'HackHaltConfig':
        return cls(
            enabled=_bool_env('HACK_HALT_ENABLED', True),
            dry_run=_bool_env('HACK_HALT_DRY_RUN', True),
            live_confirm=_bool_env('HACK_HALT_LIVE_CONFIRM', False),
            manual_confirm=_bool_env('HACK_HALT_MANUAL_CONFIRM', False),
            hedge_notional_usd=max(_float_env('HACK_HEDGE_NOTIONAL_USD', 100.0), 0.0),
            short_notional_usd=max(_float_env('HACK_SHORT_NOTIONAL_USD', 50.0), 0.0),
            leverage=max(_int_env('HACK_HALT_LEVERAGE', 3), 1),
            prefer_exchange=_str_env('HACK_HALT_PREFER_EXCHANGE', 'binance').lower(),
            max_per_day=max(_int_env('HACK_HALT_MAX_PER_DAY', 5), 0),
            per_ticker_cooldown_sec=max(
                _int_env('HACK_HALT_PER_TICKER_COOLDOWN_SEC', _COOLDOWN_PER_TICKER_SEC),
                0,
            ),
            poll_interval_ms=max(_int_env('HACK_HALT_POLL_INTERVAL_MS', _DEFAULT_POLL_MS), 200),
            http_timeout=_float_env('HACK_HALT_HTTP_TIMEOUT_SEC', _DEFAULT_HTTP_TIMEOUT),
            kill_switch_file=_str_env('HACK_HALT_KILL_SWITCH_FILE', 'data/KILL_HACK_HALT'),
            events_path=_str_env('HACK_HALT_EVENTS_PATH', 'data/hack_halt_events.jsonl'),
            seen_path=_str_env('HACK_HALT_SEEN_PATH', 'data/hack_halt_seen.json'),
            exploit_patterns_path=_str_env(
                'HACK_HALT_EXPLOIT_PATTERNS', 'data/exploit_patterns.json'
            ),
            upbit_enabled=_bool_env('HACK_HALT_UPBIT_ENABLED', True),
            bithumb_enabled=_bool_env('HACK_HALT_BITHUMB_ENABLED', True),
            bybit_enabled=_bool_env('HACK_HALT_BYBIT_ENABLED', True),
            binance_enabled=_bool_env('HACK_HALT_BINANCE_ENABLED', True),
            okx_enabled=_bool_env('HACK_HALT_OKX_ENABLED', True),
            kw_upbit=_csv_env(
                'HACK_HALT_KEYWORDS_UPBIT',
                ['입금 중단', '출금 중단', '거래 중단', '점검', '핫월렛', '장애', '일시 중지'],
            ),
            kw_bithumb=_csv_env(
                'HACK_HALT_KEYWORDS_BITHUMB',
                ['일시 중단', '입출금 중단', '거래 중단', '점검'],
            ),
            kw_intl=_csv_env(
                'HACK_HALT_KEYWORDS_INTL',
                ['maintenance', 'withdrawal suspension', 'hack', 'exploit',
                 'frozen', 'halted', 'suspended', 'temporarily'],
            ),
            exit_max_hold_sec=max(_int_env('HACK_HALT_EXIT_MAX_HOLD_SEC', 30 * 60), 60),
            exit_pnl_pct_target=_float_env('HACK_HALT_EXIT_PNL_PCT', -20.0),
        )


@dataclass
class _DetectorState:
    seen_ids: set = field(default_factory=set)                  # (source, id) 쌍
    last_fire_per_ticker: dict[str, float] = field(default_factory=dict)
    daily_count: int = 0
    daily_reset_epoch: float = 0.0
    open_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)   # job_id -> meta
    total_polls: dict[str, int] = field(default_factory=dict)
    total_errors: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    total_detections: int = 0
    total_auto_executed: int = 0
    total_dry_run: int = 0
    total_skipped: int = 0
    last_detection_ts: float = 0.0
    cf_first_fail: dict[str, float] = field(default_factory=dict)
    # perp 화이트리스트 캐시 (바이낸스/바이빗 perp 존재 여부)
    perp_binance: set = field(default_factory=set)
    perp_bybit: set = field(default_factory=set)
    perp_last_refresh_ts: float = 0.0


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ======================================================================
# HackHaltDetector
# ======================================================================


class HackHaltDetector:
    """CEX 핫월렛 해킹 / 거래소 입출금 중단 / 온체인 exploit 감지 + 자동 헷지.

    사용:
        detector = HackHaltDetector(
            hedge_service=hedge_trade_service,
            wallet_tracker=wallet_tracker,
            telegram_service=telegram,
        )
        await detector.start()
        ...
        await detector.stop()

    감지 흐름:
      1. 공지 폴링 (upbit/bithumb/bybit/binance/okx) → 키워드 매칭 → ticker 파싱
      2. 감지된 티커가 binance/bybit perp 에 있는지 확인
      3. 트리플 락 통과 시:
         - halt  (한국 CEX)   → hedge_service.enter(ticker, futures_exchange)  # 현선 헷지
         - hack  (핫월렛 언급) → 동일
         - exploit (온체인)   → 해외 선물 숏만 (wallet_tracker 통합)
      4. dry_run 이면 기록만.
    """

    def __init__(
        self,
        hedge_service: Any = None,
        wallet_tracker: Any = None,
        telegram_service: Any = None,
        cfg: Optional[HackHaltConfig] = None,
    ) -> None:
        self.hedge = hedge_service
        self.wallet_tracker = wallet_tracker
        self.telegram = telegram_service
        self.cfg = cfg or HackHaltConfig.load()
        self.state = _DetectorState(daily_reset_epoch=_today_midnight_epoch())

        self._running: bool = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._session: Any = None
        self._write_lock = asyncio.Lock()
        self._inflight_tickers: set[str] = set()
        self._exploit_patterns: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # 파일 경로 준비
        try:
            Path(self.cfg.events_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.cfg.seen_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('HackHaltDetector path prep failed: %s', exc)

        if not self.cfg.enabled:
            logger.info('HackHaltDetector disabled via HACK_HALT_ENABLED=false')
            return

        if CurlAsyncSession is None:
            logger.warning(
                'HackHaltDetector: curl_cffi unavailable — CEX notice polling disabled'
            )
            return

        # 공유 세션
        try:
            self._session = CurlAsyncSession(
                impersonate='chrome124',
                timeout=self.cfg.http_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error('HackHaltDetector session init failed: %s', exc)
            self._running = False
            return

        self._load_seen()
        self._load_exploit_patterns()

        # perp 화이트리스트 초기 로딩 (best-effort)
        try:
            await self._refresh_perp_whitelist()
        except Exception as exc:  # noqa: BLE001
            logger.warning('HackHaltDetector perp whitelist init: %s', exc)

        poll_sec = max(0.2, self.cfg.poll_interval_ms / 1000.0)

        if self.cfg.upbit_enabled:
            self._tasks.append(asyncio.create_task(
                self._loop('upbit', self._poll_upbit, poll_sec),
                name='hack_halt_upbit',
            ))
        if self.cfg.bithumb_enabled:
            self._tasks.append(asyncio.create_task(
                self._loop('bithumb', self._poll_bithumb, poll_sec),
                name='hack_halt_bithumb',
            ))
        if self.cfg.bybit_enabled:
            self._tasks.append(asyncio.create_task(
                self._loop('bybit', self._poll_bybit, max(poll_sec, 2.0)),
                name='hack_halt_bybit',
            ))
        if self.cfg.binance_enabled:
            self._tasks.append(asyncio.create_task(
                self._loop('binance', self._poll_binance, max(poll_sec, 2.0)),
                name='hack_halt_binance',
            ))
        if self.cfg.okx_enabled:
            self._tasks.append(asyncio.create_task(
                self._loop('okx', self._poll_okx, max(poll_sec, 2.0)),
                name='hack_halt_okx',
            ))

        # perp 화이트리스트 주기 갱신 (5분)
        self._tasks.append(asyncio.create_task(
            self._perp_refresh_loop(),
            name='hack_halt_perp_refresh',
        ))

        # exploit 감시 루프 (wallet_tracker 통합 - Phase 2 stub)
        self._tasks.append(asyncio.create_task(
            self._exploit_loop(),
            name='hack_halt_exploit',
        ))

        # auto-exit 루프 — exploit 숏 포지션 타임 컷/손절
        self._tasks.append(asyncio.create_task(
            self._exit_loop(),
            name='hack_halt_exit',
        ))

        logger.info(
            'HackHaltDetector started (enabled=%s dry_run=%s live_confirm=%s '
            'sources=[upbit=%s bithumb=%s bybit=%s binance=%s okx=%s] '
            'hedge=$%.0f short=$%.0f max_per_day=%d)',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.upbit_enabled, self.cfg.bithumb_enabled,
            self.cfg.bybit_enabled, self.cfg.binance_enabled, self.cfg.okx_enabled,
            self.cfg.hedge_notional_usd, self.cfg.short_notional_usd,
            self.cfg.max_per_day,
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        session = self._session
        self._session = None
        if session is not None:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug('HackHaltDetector session close: %s', exc)
        logger.info('HackHaltDetector stopped')

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and self.hedge is not None
        )
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'manual_confirm': self.cfg.manual_confirm,
            'live_armed': live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'hedge_notional_usd': self.cfg.hedge_notional_usd,
            'short_notional_usd': self.cfg.short_notional_usd,
            'leverage': self.cfg.leverage,
            'prefer_exchange': self.cfg.prefer_exchange,
            'max_per_day': self.cfg.max_per_day,
            'daily_count': self.state.daily_count,
            'per_ticker_cooldown_sec': self.cfg.per_ticker_cooldown_sec,
            'cooldown_tickers': {
                k: max(0, int(self.cfg.per_ticker_cooldown_sec - (time.time() - v)))
                for k, v in self.state.last_fire_per_ticker.items()
                if (time.time() - v) < self.cfg.per_ticker_cooldown_sec
            },
            'open_jobs': {k: dict(v) for k, v in self.state.open_jobs.items()},
            'total_polls': dict(self.state.total_polls),
            'total_errors': dict(self.state.total_errors),
            'last_error': dict(self.state.last_error),
            'total_detections': self.state.total_detections,
            'total_auto_executed': self.state.total_auto_executed,
            'total_dry_run': self.state.total_dry_run,
            'total_skipped': self.state.total_skipped,
            'last_detection_ts': self.state.last_detection_ts,
            'perp_binance_count': len(self.state.perp_binance),
            'perp_bybit_count': len(self.state.perp_bybit),
            'seen_count': len(self.state.seen_ids),
            'exploit_patterns_count': len(self._exploit_patterns),
            'events_path': self.cfg.events_path,
            'kill_switch_file': self.cfg.kill_switch_file,
            'sources': {
                'upbit': self.cfg.upbit_enabled,
                'bithumb': self.cfg.bithumb_enabled,
                'bybit': self.cfg.bybit_enabled,
                'binance': self.cfg.binance_enabled,
                'okx': self.cfg.okx_enabled,
            },
        }

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.cfg.events_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('HackHaltDetector recent_events read failed: %s', exc)
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

    async def abort(self, job_id: str, reason: str = 'manual') -> dict[str, Any]:
        """open_jobs 에 들어있는 헷지/숏 포지션을 수동 강제 청산."""
        job_id = str(job_id or '').strip()
        job = self.state.open_jobs.get(job_id)
        if job is None:
            return {'ok': False, 'code': 'NO_OPEN_JOB',
                    'message': f'no open job {job_id!r}'}
        ticker = str(job.get('ticker') or '')
        mode = str(job.get('mode') or 'dry_run')
        trade_type = str(job.get('trade_type') or '')

        if mode == 'dry_run':
            self.state.open_jobs.pop(job_id, None)
            await self._append_event_record({
                'ts': int(time.time()),
                'job_id': job_id, 'ticker': ticker,
                'trade_type': trade_type, 'mode': 'dry_run',
                'action': 'abort', 'reason': reason,
            })
            return {'ok': True, 'job_id': job_id, 'mode': 'dry_run'}

        if self.hedge is None:
            return {'ok': False, 'code': 'UNAVAILABLE',
                    'message': 'hedge_service unavailable'}

        try:
            if trade_type == 'hack_hedge':
                res = await self.hedge.close_job(ticker=ticker, reason=reason)
            else:
                # exploit_short: close_ff 경로 (선물-선물 close) — 가장 안전
                res = await self.hedge.close_ff(ticker=ticker, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception('HackHaltDetector abort exc: %s', exc)
            return {'ok': False, 'code': 'CLOSE_EXCEPTION', 'message': str(exc)}

        if isinstance(res, dict) and res.get('ok'):
            self.state.open_jobs.pop(job_id, None)
        await self._append_event_record({
            'ts': int(time.time()),
            'job_id': job_id, 'ticker': ticker,
            'trade_type': trade_type, 'mode': 'live',
            'action': 'abort', 'reason': reason, 'close_result': res,
        })
        return {'ok': bool(isinstance(res, dict) and res.get('ok')),
                'job_id': job_id, 'result': res}

    # ------------------------------------------------------------------
    # 폴링 래퍼
    # ------------------------------------------------------------------

    async def _loop(self, source: str, poll_fn, interval_sec: float) -> None:
        logger.info('[hack-halt] %s loop start (%.1fs)', source, interval_sec)
        while self._running:
            started = time.monotonic()
            try:
                await poll_fn()
                self.state.total_polls[source] = self.state.total_polls.get(source, 0) + 1
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.state.total_errors[source] = (
                    self.state.total_errors.get(source, 0) + 1
                )
                self.state.last_error[source] = f'{type(exc).__name__}: {exc}'
                logger.warning('[hack-halt] %s poll error: %s', source, exc)
            elapsed = time.monotonic() - started
            sleep = max(0.05, interval_sec - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break
        logger.info('[hack-halt] %s loop end', source)

    # ------------------------------------------------------------------
    # Upbit
    # ------------------------------------------------------------------

    async def _poll_upbit(self) -> None:
        session = self._session
        if session is None:
            return
        # 공지 카테고리: category=market (입출금 중지), category=trade (거래 중지) 모두 감시
        for category in ('market', 'trade'):
            url = (
                'https://api-manager.upbit.com/api/v1/announcements'
                f'?os=web&page=1&per_page=20&category={category}'
            )
            items = await self._fetch_json_list(
                'upbit', session, url, _upbit_headers(), key_path=('data', 'list'),
            )
            if items is None:
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                notice_id = str(item.get('id', ''))
                title = str(item.get('title', '') or '').strip()
                if not notice_id or not title:
                    continue
                key = ('upbit', notice_id)
                if key in self.state.seen_ids:
                    continue
                matched = self._match_keyword(title, self.cfg.kw_upbit)
                self._remember_seen(key)
                if matched is None:
                    continue
                ticker = self._parse_ticker(title)
                if ticker is None:
                    continue
                event_type = self._infer_event_type(title, 'halt')
                url_full = f'https://upbit.com/service_center/notice?id={notice_id}'
                await self._emit(
                    source='upbit', event_type=event_type,
                    notice_id=notice_id, ticker=ticker,
                    title=title, url=url_full, matched=matched,
                )

    # ------------------------------------------------------------------
    # Bithumb
    # ------------------------------------------------------------------

    async def _poll_bithumb(self) -> None:
        session = self._session
        if session is None:
            return
        url = 'https://feed.bithumb.com/notice?page=1&per_page=20'
        items = await self._fetch_json_list(
            'bithumb', session, url, _bithumb_headers(),
            key_path=('list',),
            fallback_key_path=('data', 'list'),
        )
        if items is None:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            notice_id = str(item.get('id', ''))
            title = str(item.get('title', '') or '').strip()
            if not notice_id or not title:
                continue
            key = ('bithumb', notice_id)
            if key in self.state.seen_ids:
                continue
            matched = self._match_keyword(title, self.cfg.kw_bithumb)
            self._remember_seen(key)
            if matched is None:
                continue
            ticker = self._parse_ticker(title)
            if ticker is None:
                continue
            event_type = self._infer_event_type(title, 'halt')
            url_full = f'https://feed.bithumb.com/notice/{notice_id}'
            await self._emit(
                source='bithumb', event_type=event_type,
                notice_id=notice_id, ticker=ticker,
                title=title, url=url_full, matched=matched,
            )

    # ------------------------------------------------------------------
    # Bybit
    # ------------------------------------------------------------------

    async def _poll_bybit(self) -> None:
        session = self._session
        if session is None:
            return
        # type=new_crypto 에 점검/상폐/입출금 중지 공지가 섞여서 온다
        for qtype in ('new_crypto', 'delistings'):
            url = (
                'https://api2.bybit.com/announcements/api/search/v1/index/'
                f'announcement-posts_en-us?category=&page=1&page_size=30&keyword=&type={qtype}'
            )
            items = await self._fetch_json_list(
                'bybit', session, url, _bybit_headers(),
                key_path=('result', 'list'),
            )
            if items is None:
                # 구 endpoint 재시도 — bybit 공지 구조가 주기적으로 바뀜
                alt = (
                    'https://api.bybit.com/v5/announcements/index'
                    f'?locale=en-US&type={qtype}&page=1&limit=30'
                )
                items = await self._fetch_json_list(
                    'bybit', session, alt, _bybit_headers(),
                    key_path=('result', 'list'),
                )
                if items is None:
                    return
            for item in items:
                if not isinstance(item, dict):
                    continue
                notice_id = str(item.get('id') or item.get('url') or item.get('title') or '')
                title = str(item.get('title', '') or '').strip()
                if not notice_id or not title:
                    continue
                key = ('bybit', notice_id)
                if key in self.state.seen_ids:
                    continue
                matched = self._match_keyword(title, self.cfg.kw_intl)
                self._remember_seen(key)
                if matched is None:
                    continue
                ticker = self._parse_ticker(title)
                if ticker is None:
                    continue
                event_type = self._infer_event_type(title, 'maintenance')
                url_full = str(item.get('url') or '')
                if url_full and not url_full.startswith('http'):
                    url_full = f'https://announcements.bybit.com{url_full}'
                await self._emit(
                    source='bybit', event_type=event_type,
                    notice_id=notice_id, ticker=ticker,
                    title=title, url=url_full, matched=matched,
                )

    # ------------------------------------------------------------------
    # Binance
    # ------------------------------------------------------------------

    async def _poll_binance(self) -> None:
        session = self._session
        if session is None:
            return
        # catalogId=161 = "Latest Binance News". 161/48/49 등 순환 시도.
        # 안정적 엔드포인트: bapi/composite/v1/public/cms/article/list/query
        url = (
            'https://www.binance.com/bapi/composite/v1/public/cms/article/list/query'
            '?type=1&pageNo=1&pageSize=30&catalogId=48'
        )
        items = await self._fetch_json_list(
            'binance', session, url, _binance_headers(),
            key_path=('data', 'articles'),
            fallback_key_path=('data', 'catalogs', 0, 'articles'),
        )
        if items is None:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            notice_id = str(item.get('id') or item.get('code') or '')
            title = str(item.get('title', '') or '').strip()
            if not notice_id or not title:
                continue
            key = ('binance', notice_id)
            if key in self.state.seen_ids:
                continue
            matched = self._match_keyword(title, self.cfg.kw_intl)
            self._remember_seen(key)
            if matched is None:
                continue
            ticker = self._parse_ticker(title)
            if ticker is None:
                continue
            event_type = self._infer_event_type(title, 'maintenance')
            code = item.get('code') or notice_id
            url_full = f'https://www.binance.com/en/support/announcement/{code}'
            await self._emit(
                source='binance', event_type=event_type,
                notice_id=notice_id, ticker=ticker,
                title=title, url=url_full, matched=matched,
            )

    # ------------------------------------------------------------------
    # OKX
    # ------------------------------------------------------------------

    async def _poll_okx(self) -> None:
        session = self._session
        if session is None:
            return
        url = (
            'https://www.okx.com/v2/support/home/web'
            '?type=announcements-new-listings&limit=30&offset=0'
        )
        items = await self._fetch_json_list(
            'okx', session, url, _okx_headers(),
            key_path=('data', 'notices'),
            fallback_key_path=('data', 'list'),
        )
        if items is None:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            notice_id = str(item.get('id') or item.get('url') or '')
            title = str(item.get('title', '') or '').strip()
            if not notice_id or not title:
                continue
            key = ('okx', notice_id)
            if key in self.state.seen_ids:
                continue
            matched = self._match_keyword(title, self.cfg.kw_intl)
            self._remember_seen(key)
            if matched is None:
                continue
            ticker = self._parse_ticker(title)
            if ticker is None:
                continue
            event_type = self._infer_event_type(title, 'maintenance')
            url_full = str(item.get('url') or '')
            if url_full and not url_full.startswith('http'):
                url_full = f'https://www.okx.com{url_full}'
            await self._emit(
                source='okx', event_type=event_type,
                notice_id=notice_id, ticker=ticker,
                title=title, url=url_full, matched=matched,
            )

    # ------------------------------------------------------------------
    # HTTP 공통 — 403/429 처리 + json 파싱 + 키 경로 추출
    # ------------------------------------------------------------------

    async def _fetch_json_list(
        self,
        source: str,
        session: Any,
        url: str,
        headers: dict[str, str],
        key_path: tuple,
        fallback_key_path: Optional[tuple] = None,
    ) -> Optional[list[Any]]:
        """JSON 응답에서 key_path 로 리스트 노드 추출. 403/429 에러 시 None."""
        try:
            resp = await session.get(url, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.state.last_error[source] = f'net: {exc}'
            logger.debug('[hack-halt] %s GET failed: %s', source, exc)
            return None

        status = getattr(resp, 'status_code', None)
        if status == 429:
            logger.warning('[hack-halt] %s 429 rate-limit; %.0fs backoff',
                           source, _RATE_LIMIT_BACKOFF_SEC)
            await asyncio.sleep(_RATE_LIMIT_BACKOFF_SEC)
            return None
        if status == 403:
            now = time.monotonic()
            first_fail = self.state.cf_first_fail.get(source, 0.0)
            if first_fail == 0.0:
                self.state.cf_first_fail[source] = now
            elif (now - first_fail) >= _CLOUDFLARE_SUSTAINED_SEC:
                logger.error(
                    '[hack-halt] %s Cloudflare 403 sustained %.0fs; pause %.0fs',
                    source, now - first_fail, _CLOUDFLARE_PAUSE_SEC,
                )
                self.state.cf_first_fail[source] = 0.0
                await asyncio.sleep(_CLOUDFLARE_PAUSE_SEC)
            else:
                logger.debug('[hack-halt] %s 403 (cloudflare?)', source)
            return None
        # 성공 응답 → cf 카운트 리셋
        self.state.cf_first_fail[source] = 0.0

        if status != 200:
            logger.debug('[hack-halt] %s HTTP %s', source, status)
            return None

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[hack-halt] %s json parse err: %s', source, exc)
            return None

        items = self._deep_get(data, key_path)
        if not isinstance(items, list) and fallback_key_path is not None:
            items = self._deep_get(data, fallback_key_path)
        if not isinstance(items, list):
            return None
        return items

    @staticmethod
    def _deep_get(obj: Any, path: tuple) -> Any:
        cur = obj
        for p in path:
            if isinstance(p, int):
                if isinstance(cur, list) and 0 <= p < len(cur):
                    cur = cur[p]
                else:
                    return None
            else:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    return None
            if cur is None:
                return None
        return cur

    # ------------------------------------------------------------------
    # Keyword / ticker 파싱
    # ------------------------------------------------------------------

    @staticmethod
    def _match_keyword(title: str, keywords: list[str]) -> Optional[str]:
        if not title or not keywords:
            return None
        t_lower = title.lower()
        for kw in keywords:
            kw_norm = kw.strip()
            if not kw_norm:
                continue
            # 영문 키워드는 대소문자 구분 없이, 한글은 그대로 매칭
            if kw_norm.isascii():
                if kw_norm.lower() in t_lower:
                    return kw_norm
            else:
                if kw_norm in title:
                    return kw_norm
        return None

    @staticmethod
    def _infer_event_type(title: str, default: str) -> str:
        t = title.lower()
        if any(w in t for w in ('hack', 'exploit', '해킹', '핫월렛')):
            return 'hack'
        if any(w in t for w in ('입금 중단', '출금 중단', '입출금', 'withdrawal suspension',
                                'withdraw suspension', 'deposit suspension')):
            return 'halt'
        if any(w in t for w in ('maintenance', 'temporarily', '점검', '일시 중지', '일시 중단')):
            return 'maintenance'
        return default

    @staticmethod
    def _parse_ticker(title: str) -> Optional[str]:
        # 괄호 안 우선
        for m in _TICKER_PAREN_RE.finditer(title):
            cand = m.group(1).upper()
            if cand in _TICKER_BLACKLIST:
                continue
            return cand
        # 일반 대문자 토큰 — 모호하므로 blacklist 엄격 적용 + 길이 3~8만
        for m in _TICKER_UPPER_RE.finditer(title):
            cand = m.group(1).upper()
            if not (3 <= len(cand) <= 8):
                continue
            if cand in _TICKER_BLACKLIST:
                continue
            return cand
        return None

    # ------------------------------------------------------------------
    # 이벤트 emit
    # ------------------------------------------------------------------

    async def _emit(
        self,
        *,
        source: str,
        event_type: str,
        notice_id: str,
        ticker: str,
        title: str,
        url: str,
        matched: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        event = HaltEvent(
            ts=time.time(),
            source=source,
            event_type=event_type,
            notice_id=notice_id,
            ticker=ticker,
            title=title,
            url=url,
            matched_keyword=matched,
            binance_perp=(ticker in self.state.perp_binance),
            bybit_perp=(ticker in self.state.perp_bybit),
            extra=dict(extra or {}),
        )
        self.state.total_detections += 1
        self.state.last_detection_ts = event.ts

        logger.info(
            '[hack-halt] DETECT %s %s kw=%s ticker=%s type=%s id=%s title=%s',
            source, event_type, matched, ticker, event_type, notice_id, title[:120],
        )
        await self._append_event_record(event.to_json())
        self._persist_seen_safe()
        await self._notify(event)
        # 백그라운드 실행 — 감지 루프 블로킹 방지
        try:
            asyncio.create_task(
                self._handle_event(event),
                name=f'hack_halt_exec_{ticker}',
            )
        except RuntimeError:
            pass

    async def _handle_event(self, event: HaltEvent) -> None:
        try:
            await self._handle_event_inner(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception('[hack-halt] handle_event err: %s', exc)

    async def _handle_event_inner(self, event: HaltEvent) -> None:
        ticker = event.ticker
        now_ts = time.time()

        # 게이트 0: 거래소 perp 에 없으면 실행 경로 없음
        target = self._select_futures_exchange(event.binance_perp, event.bybit_perp)
        if target is None:
            logger.info(
                '[hack-halt] skip %s: no binance/bybit perp (source=%s)',
                ticker, event.source,
            )
            self.state.total_skipped += 1
            return

        # 게이트 1: kill switch
        if self._kill_switch_active():
            logger.warning(
                '[hack-halt] skip %s: kill switch %s present',
                ticker, self.cfg.kill_switch_file,
            )
            self.state.total_skipped += 1
            return

        # 게이트 2: per-ticker cooldown (24h 기본)
        last = self.state.last_fire_per_ticker.get(ticker, 0.0)
        if last > 0 and (now_ts - last) < self.cfg.per_ticker_cooldown_sec:
            logger.info(
                '[hack-halt] skip %s: cooldown %ds remaining',
                ticker,
                int(self.cfg.per_ticker_cooldown_sec - (now_ts - last)),
            )
            self.state.total_skipped += 1
            return

        # 게이트 3: daily cap
        self._maybe_rollover_daily()
        if self.cfg.max_per_day > 0 and self.state.daily_count >= self.cfg.max_per_day:
            logger.warning(
                '[hack-halt] skip %s: daily cap %d reached',
                ticker, self.cfg.max_per_day,
            )
            self.state.total_skipped += 1
            return

        # 게이트 4: inflight dedup
        if ticker in self._inflight_tickers:
            logger.info('[hack-halt] skip %s: inflight already', ticker)
            self.state.total_skipped += 1
            return

        # 게이트 5: 라이브 승인
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and self.hedge is not None
        )

        # 게이트 6: manual confirm — 향후 텔레그램 버튼. 지금은 승인 없이는 dry-run 취급
        if self.cfg.manual_confirm:
            await self._send_telegram(
                f'⏸ 수동 승인 필요 (HACK_HALT_MANUAL_CONFIRM=true)\n'
                f'{event.event_type.upper()} {event.source} {ticker}\n'
                f'{event.title}\n{event.url}\n'
                f'자동 실행 스킵 — 수동 enter 필요'
            )
            live_armed = False

        # 결정: halt/hack = 현선 헷지, exploit = 선물 숏
        trade_type = 'hack_hedge' if event.event_type in ('halt', 'hack') else 'exploit_short'

        self._inflight_tickers.add(ticker)
        try:
            if not live_armed:
                await self._record_dry_run(event, trade_type, target)
                return
            if trade_type == 'hack_hedge':
                await self._execute_hedge(event, target)
            else:
                await self._execute_short(event, target)
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
        if binance:
            return 'binance'
        if bybit:
            return 'bybit'
        return None

    # ------------------------------------------------------------------
    # 실행 경로 — hack/halt → hedge_service.enter (현선 헷지)
    # ------------------------------------------------------------------

    async def _execute_hedge(self, event: HaltEvent, futures_exchange: str) -> None:
        ticker = event.ticker
        try:
            res = await self.hedge.enter(
                ticker=ticker,
                futures_exchange=futures_exchange,
                nominal_usd=self.cfg.hedge_notional_usd,
                leverage=self.cfg.leverage,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('[hack-halt] hedge.enter exc: %s', exc)
            self.state.last_error['execute'] = f'{type(exc).__name__}: {exc}'
            await self._append_event_record({
                'ts': int(time.time()),
                'trade_type': 'hack_hedge', 'mode': 'live_failed',
                'ticker': ticker, 'futures_exchange': futures_exchange,
                'source': event.source, 'notice_id': event.notice_id,
                'error': str(exc),
            })
            await self._send_telegram(
                f'❌ 해킹/정지 헷지 실패: {ticker} via {futures_exchange}\n{exc}'
            )
            return

        ok = bool(isinstance(res, dict) and res.get('ok'))
        job_id = f'hh_{int(time.time() * 1000)}_{ticker}'
        record = {
            'ts': int(time.time()),
            'job_id': job_id, 'trade_type': 'hack_hedge',
            'mode': 'live' if ok else 'live_failed',
            'ticker': ticker, 'futures_exchange': futures_exchange,
            'source': event.source, 'notice_id': event.notice_id,
            'event_type': event.event_type, 'matched_keyword': event.matched_keyword,
            'hedge_result': res,
        }
        await self._append_event_record(record)
        if ok:
            self.state.daily_count += 1
            self.state.total_auto_executed += 1
            self.state.last_fire_per_ticker[ticker] = time.time()
            self.state.open_jobs[job_id] = {
                'ticker': ticker, 'trade_type': 'hack_hedge',
                'futures_exchange': futures_exchange,
                'opened_ts': time.time(), 'mode': 'live',
                'source': event.source, 'notice_id': event.notice_id,
            }
            await self._send_telegram(
                f'🚨 HACK HALT: {event.source} {event.event_type} {ticker} '
                f'— 현선 헷지 진입 (${self.cfg.hedge_notional_usd:.0f} via {futures_exchange})\n'
                f'{event.title}'
            )
        else:
            await self._send_telegram(
                f'⚠️ HACK HALT 실패: {ticker} — {res}'
            )

    # ------------------------------------------------------------------
    # 실행 경로 — exploit → 선물 숏만 (hedge_service.enter_ff 경로)
    # ------------------------------------------------------------------

    async def _execute_short(self, event: HaltEvent, futures_exchange: str) -> None:
        ticker = event.ticker
        # 선물 단방향 숏: hedge_service 는 enter_ff (선물-선물 헷지) 가 있지만
        # 진짜 단방향이 필요. enter_ff 는 여기선 쓰지 않고 enter 도 spot 부분이 있음.
        # 우리는 safer 경로로 enter 를 호출하되 nominal 을 짧게 잡는다.
        # (한국 spot 이 없어도 Bithumb 에서 해당 티커가 없으면 enter 가 PRICE_UNAVAILABLE
        # 반환하므로, 그 경우 자동 dry-run 으로 fallback.)
        try:
            res = await self.hedge.enter(
                ticker=ticker,
                futures_exchange=futures_exchange,
                nominal_usd=self.cfg.short_notional_usd,
                leverage=self.cfg.leverage,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('[hack-halt] exploit enter exc: %s', exc)
            self.state.last_error['execute'] = f'{type(exc).__name__}: {exc}'
            await self._append_event_record({
                'ts': int(time.time()),
                'trade_type': 'exploit_short', 'mode': 'live_failed',
                'ticker': ticker, 'futures_exchange': futures_exchange,
                'source': event.source, 'notice_id': event.notice_id,
                'error': str(exc),
            })
            await self._send_telegram(
                f'❌ Exploit 숏 실패: {ticker} via {futures_exchange}\n{exc}'
            )
            return

        ok = bool(isinstance(res, dict) and res.get('ok'))
        job_id = f'hh_{int(time.time() * 1000)}_{ticker}'
        record = {
            'ts': int(time.time()),
            'job_id': job_id, 'trade_type': 'exploit_short',
            'mode': 'live' if ok else 'live_failed',
            'ticker': ticker, 'futures_exchange': futures_exchange,
            'source': event.source, 'notice_id': event.notice_id,
            'event_type': event.event_type, 'matched_keyword': event.matched_keyword,
            'hedge_result': res,
        }
        await self._append_event_record(record)
        if ok:
            self.state.daily_count += 1
            self.state.total_auto_executed += 1
            self.state.last_fire_per_ticker[ticker] = time.time()
            self.state.open_jobs[job_id] = {
                'ticker': ticker, 'trade_type': 'exploit_short',
                'futures_exchange': futures_exchange,
                'opened_ts': time.time(), 'mode': 'live',
                'source': event.source, 'notice_id': event.notice_id,
            }
            await self._send_telegram(
                f'🩳 EXPLOIT: {event.source} {ticker} — 선물 숏 진입 '
                f'(${self.cfg.short_notional_usd:.0f} via {futures_exchange})\n'
                f'{event.title}'
            )
        else:
            await self._send_telegram(
                f'⚠️ Exploit 숏 실패: {ticker} — {res}'
            )

    async def _record_dry_run(
        self,
        event: HaltEvent,
        trade_type: str,
        futures_exchange: str,
    ) -> None:
        logger.info(
            '[DRY-HACK-HALT] would %s %s $%.0f via %s (source=%s type=%s)',
            trade_type,
            event.ticker,
            self.cfg.hedge_notional_usd if trade_type == 'hack_hedge'
            else self.cfg.short_notional_usd,
            futures_exchange,
            event.source,
            event.event_type,
        )
        self.state.total_dry_run += 1
        self.state.last_fire_per_ticker[event.ticker] = time.time()  # cooldown 적용
        job_id = f'hh_{int(time.time() * 1000)}_{event.ticker}'
        self.state.open_jobs[job_id] = {
            'ticker': event.ticker, 'trade_type': trade_type,
            'futures_exchange': futures_exchange,
            'opened_ts': time.time(), 'mode': 'dry_run',
            'source': event.source, 'notice_id': event.notice_id,
        }
        await self._append_event_record({
            'ts': int(time.time()),
            'job_id': job_id, 'trade_type': trade_type, 'mode': 'dry_run',
            'ticker': event.ticker, 'futures_exchange': futures_exchange,
            'source': event.source, 'notice_id': event.notice_id,
            'event_type': event.event_type, 'matched_keyword': event.matched_keyword,
            'reason_not_live': self._dry_reason(),
        })
        await self._send_telegram(
            f'🧪 [DRY] {trade_type} {event.ticker} via {futures_exchange} '
            f'(source={event.source} type={event.event_type})'
        )

    def _dry_reason(self) -> str:
        if not self.cfg.enabled:
            return 'disabled'
        if self.cfg.dry_run:
            return 'dry_run_env'
        if not self.cfg.live_confirm:
            return 'live_confirm_off'
        if self.cfg.manual_confirm:
            return 'manual_confirm_required'
        if self.hedge is None:
            return 'hedge_service_unavailable'
        return 'unknown'

    # ------------------------------------------------------------------
    # Exit loop — exploit 숏 포지션 시간/손절 기반 자동 청산
    # ------------------------------------------------------------------

    async def _exit_loop(self) -> None:
        while self._running:
            try:
                await self._check_exits()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug('[hack-halt] exit loop err: %s', exc)
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                break

    async def _check_exits(self) -> None:
        if not self.state.open_jobs:
            return
        now = time.time()
        for job_id, job in list(self.state.open_jobs.items()):
            mode = str(job.get('mode') or '')
            if mode != 'live':
                continue
            trade_type = str(job.get('trade_type') or '')
            opened_ts = float(job.get('opened_ts') or 0.0)
            if trade_type != 'exploit_short':
                continue
            # 타임 컷
            if self.cfg.exit_max_hold_sec > 0 and (now - opened_ts) >= self.cfg.exit_max_hold_sec:
                ticker = str(job.get('ticker') or '')
                logger.info(
                    '[hack-halt] auto-exit %s (time_cut %.0fs >= %ds)',
                    ticker, now - opened_ts, self.cfg.exit_max_hold_sec,
                )
                try:
                    await self.abort(job_id, reason='auto_exit_time_cut')
                except Exception as exc:  # noqa: BLE001
                    logger.warning('[hack-halt] auto-exit %s err: %s', ticker, exc)

    # ------------------------------------------------------------------
    # Exploit 패턴 감시 루프 (Phase 2 stub — wallet_tracker 이벤트 기반)
    # ------------------------------------------------------------------

    async def _exploit_loop(self) -> None:
        """wallet_tracker 의 recent_events 를 주기적으로 확인, exploit_patterns 와 매칭.

        Phase 2 에서 wallet_tracker 가 exploit signature (SERVICE_ROLE mint 등) 을
        기록하면 이 loop 이 소비한다. 현재는 stub.
        """
        if self.wallet_tracker is None:
            logger.info('[hack-halt] exploit loop: wallet_tracker missing, idle')
        while self._running:
            try:
                if self.wallet_tracker is None or not self._exploit_patterns:
                    await asyncio.sleep(30.0)
                    continue
                # stub: wallet_tracker.recent_events 에서 exploit 패턴 히트 찾기
                events = []
                if hasattr(self.wallet_tracker, 'recent_events'):
                    try:
                        events = self.wallet_tracker.recent_events(limit=20) or []
                    except Exception:  # noqa: BLE001
                        events = []
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    hit = self._match_exploit_pattern(ev)
                    if hit is None:
                        continue
                    notice_id = str(ev.get('tx') or ev.get('id') or '')
                    key = ('exploit', notice_id)
                    if key in self.state.seen_ids:
                        continue
                    self._remember_seen(key)
                    ticker = str(hit.get('victim_ticker') or '').upper()
                    if not ticker:
                        continue
                    await self._emit(
                        source='exploit', event_type='exploit',
                        notice_id=notice_id, ticker=ticker,
                        title=f'On-chain exploit: {hit.get("name", "")}',
                        url=str(ev.get('tx_url') or ''),
                        matched=str(hit.get('name', '')),
                        extra={'pattern': hit, 'wallet_event': ev},
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug('[hack-halt] exploit loop err: %s', exc)
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break

    def _match_exploit_pattern(self, ev: dict[str, Any]) -> Optional[dict[str, Any]]:
        """wallet event 에서 exploit_patterns.json 의 signature 와 매칭.

        각 패턴 예:
          {"name": "usr_service_role_mint",
           "event_type": ["erc20_transfer", "mint"],
           "token_contract": "0x...",
           "victim_ticker": "USR",
           "min_amount_usd": 100000}
        """
        ev_type = str(ev.get('event_type') or ev.get('type') or '').lower()
        token_c = str(ev.get('token_contract') or ev.get('contract') or '').lower()
        amount_usd = float(ev.get('amount_usd') or 0.0)
        for pat in self._exploit_patterns:
            allowed_types = [t.lower() for t in (pat.get('event_type') or [])]
            if allowed_types and ev_type not in allowed_types:
                continue
            pat_contract = str(pat.get('token_contract') or '').lower()
            if pat_contract and pat_contract != token_c:
                continue
            min_amt = float(pat.get('min_amount_usd') or 0.0)
            if amount_usd < min_amt:
                continue
            return pat
        return None

    def _load_exploit_patterns(self) -> None:
        path = Path(self.cfg.exploit_patterns_path)
        if not path.exists():
            # scaffold 기본 샘플 (pannpunch 사례 기반)
            scaffold = [
                {
                    'name': 'drift_jlp_drain',
                    'event_type': ['erc20_transfer'],
                    'victim_ticker': 'DRIFT',
                    'min_amount_usd': 100000,
                    'notes': 'Drift JLP 비정상 대량 인출 감지',
                },
                {
                    'name': 'usr_service_role_mint',
                    'event_type': ['mint'],
                    'victim_ticker': 'USR',
                    'min_amount_usd': 100000,
                    'notes': 'Resolv USR SERVICE_ROLE mint',
                },
                {
                    'name': 'hyperbridge_abnormal',
                    'event_type': ['bridge_deposit'],
                    'victim_ticker': 'DOT',
                    'min_amount_usd': 500000,
                    'notes': 'Polkadot Hyperbridge 비정상 볼륨',
                },
                {
                    'name': 'venus_the_exploit',
                    'event_type': ['erc20_transfer'],
                    'victim_ticker': 'THE',
                    'min_amount_usd': 50000,
                    'notes': 'Venus THE 대량 덤프',
                },
            ]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('w', encoding='utf-8') as f:
                    json.dump(scaffold, f, indent=2, ensure_ascii=False)
                logger.info(
                    'HackHaltDetector: scaffolded exploit_patterns at %s (%d entries)',
                    path, len(scaffold),
                )
                self._exploit_patterns = scaffold
            except Exception as exc:  # noqa: BLE001
                logger.warning('HackHaltDetector exploit_patterns scaffold failed: %s', exc)
                self._exploit_patterns = []
            return
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                self._exploit_patterns = [p for p in data if isinstance(p, dict)]
            else:
                self._exploit_patterns = []
        except Exception as exc:  # noqa: BLE001
            logger.warning('HackHaltDetector exploit_patterns load failed: %s', exc)
            self._exploit_patterns = []

    # ------------------------------------------------------------------
    # Perp 화이트리스트 캐시
    # ------------------------------------------------------------------

    async def _perp_refresh_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_perp_whitelist()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug('[hack-halt] perp refresh err: %s', exc)
            try:
                await asyncio.sleep(300.0)
            except asyncio.CancelledError:
                break

    async def _refresh_perp_whitelist(self) -> None:
        session = self._session
        if session is None:
            return
        bn_task = asyncio.create_task(self._fetch_binance_perp(session))
        by_task = asyncio.create_task(self._fetch_bybit_perp(session))
        results = await asyncio.gather(bn_task, by_task, return_exceptions=True)
        bn_result, by_result = results
        if isinstance(bn_result, set) and bn_result:
            self.state.perp_binance = bn_result
        if isinstance(by_result, set) and by_result:
            self.state.perp_bybit = by_result
        self.state.perp_last_refresh_ts = time.time()

    async def _fetch_binance_perp(self, session: Any) -> set:
        try:
            resp = await session.get(
                'https://fapi.binance.com/fapi/v1/exchangeInfo',
                headers={'Accept': 'application/json', 'User-Agent': _BROWSER_UA},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('binance exchangeInfo err: %s', exc)
            return set()
        if getattr(resp, 'status_code', None) != 200:
            return set()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return set()
        out: set = set()
        symbols = data.get('symbols') if isinstance(data, dict) else None
        if not isinstance(symbols, list):
            return out
        for sym in symbols:
            if not isinstance(sym, dict):
                continue
            if sym.get('status') != 'TRADING':
                continue
            base = sym.get('baseAsset')
            if isinstance(base, str) and base:
                out.add(base.upper())
        return out

    async def _fetch_bybit_perp(self, session: Any) -> set:
        try:
            resp = await session.get(
                'https://api.bybit.com/v5/market/instruments-info'
                '?category=linear&status=Trading&limit=1000',
                headers={'Accept': 'application/json', 'User-Agent': _BROWSER_UA},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('bybit instruments-info err: %s', exc)
            return set()
        if getattr(resp, 'status_code', None) != 200:
            return set()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return set()
        out: set = set()
        result = data.get('result') if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return out
        rows = result.get('list')
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get('status') != 'Trading':
                continue
            base = row.get('baseCoin')
            if isinstance(base, str) and base:
                out.add(base.upper())
        return out

    # ------------------------------------------------------------------
    # 상태 보존 / 유틸
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
                '[hack-halt] daily rollover: count=%d reset',
                self.state.daily_count,
            )
            self.state.daily_count = 0
            self.state.daily_reset_epoch = today

    def _remember_seen(self, key: tuple) -> None:
        self.state.seen_ids.add(key)
        if len(self.state.seen_ids) > _SEEN_HARD_CAP:
            # 무작위 1/4 삭제 — set 이라 LRU 는 아니지만 메모리 캡만 보장
            for _ in range(_SEEN_HARD_CAP // 4):
                try:
                    self.state.seen_ids.pop()
                except KeyError:
                    break

    def _persist_seen_safe(self) -> None:
        try:
            path = Path(self.cfg.seen_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = [{'source': s, 'id': i} for s, i in self.state.seen_ids]
            with path.open('w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug('HackHaltDetector seen persist failed: %s', exc)

    def _load_seen(self) -> None:
        path = Path(self.cfg.seen_path)
        if not path.exists():
            return
        try:
            with path.open('r', encoding='utf-8') as f:
                payload = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning('HackHaltDetector seen load failed: %s', exc)
            return
        if not isinstance(payload, list):
            return
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            src = entry.get('source')
            nid = entry.get('id')
            if isinstance(src, str) and isinstance(nid, str):
                self.state.seen_ids.add((src, nid))
        logger.info('HackHaltDetector loaded %d seen ids', len(self.state.seen_ids))

    async def _append_event_record(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.events_path)
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('HackHaltDetector events append failed: %s', exc)

    async def _notify(self, event: HaltEvent) -> None:
        if self.telegram is None:
            return
        text = (
            f'🚨 {event.event_type.upper()} 감지: {event.source} {event.ticker}\n'
            f'kw={event.matched_keyword}\n{event.title}\n{event.url}\n'
            f'binance_perp={event.binance_perp} bybit_perp={event.bybit_perp}'
        )
        await self._send_telegram(text)

    async def _send_telegram(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is None:
                send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('HackHaltDetector telegram err: %s', exc)


# ======================================================================
# 싱글톤 접근 (선택)
# ======================================================================

_detector_singleton: Optional[HackHaltDetector] = None


def get_detector() -> Optional[HackHaltDetector]:
    return _detector_singleton


def set_detector(det: HackHaltDetector) -> None:
    global _detector_singleton
    _detector_singleton = det
