"""상장 감지기 (Listing Detector) — Phase 1: detect-only.

Upbit / Bithumb 공지 API 를 고빈도 폴링하여 신규 상장 공지를 조기에 감지한다.
감지된 이벤트는 로그, Telegram, JSONL 파일로 기록된다. 이번 단계에서는
**어떠한 주문/포지션 실행도 수행하지 않는다** (Phase 2-3 에서 도입).

- Upbit announcements: `api-manager.upbit.com/api/v1/announcements` (Cloudflare 보호)
- Bithumb notice feed: `feed.bithumb.com/notice`
- 양쪽 모두 `curl_cffi.requests.AsyncSession(impersonate='chrome124')` 필수

이 모듈은 theddari_scanner.py / auto_trigger.py 와 동일한 패턴을 따른다:
- 순수 데이터 레이어 (상위 서비스에 역의존 없음)
- HTTP 에러로 루프가 죽지 않도록 전부 catch + log + continue
- `status()` 로 대시보드가 현황을 조회
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
from typing import Any, Awaitable, Callable, Union

# in-process listener (Phase 2+ executor 용)
ListingListener = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]

from curl_cffi.requests import AsyncSession as CurlAsyncSession

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 환경 설정
# ----------------------------------------------------------------------

_DEFAULT_UPBIT_POLL_MS = 300
_DEFAULT_BITHUMB_POLL_MS = 700
_DEFAULT_PERP_REFRESH_SEC = 300.0
_DEFAULT_HTTP_TIMEOUT = 8.0
_DEFAULT_HISTORY_PATH = 'data/listing_events.jsonl'
_DEFAULT_SEEN_PATH = 'data/listing_seen.json'
_DEFAULT_EXCHANGE_WHITELIST = 'upbit,bithumb'

# 감지 후 한 번 기록하면 다시 알리지 않을 최대 seen 항목 수 (메모리 상한)
_SEEN_HARD_CAP = 5000

# Cloudflare 403 이 지속될 때 얼마나 쉴 것인가
_CLOUDFLARE_PAUSE_SEC = 60.0
_CLOUDFLARE_SUSTAINED_SEC = 60.0

# 429 rate-limit 시 backoff
_RATE_LIMIT_BACKOFF_SEC = 5.0


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
# 정규식 — 공지 제목에서 신규 KRW 상장만 추려낸다
# ----------------------------------------------------------------------

# Upbit: `[거래] 칩(CHIP) KRW, BTC 마켓 디지털 자산 추가` 같은 패턴
_UPBIT_TITLE_RE = re.compile(
    r'\[(?:거래|신규|마켓 추가)\].*?\(([A-Z0-9]{2,10})\).*?KRW'
)
# Bithumb: `[신규 상장] 칩(CHIP) 원화 마켓 추가` / `원화 마켓 신규 상장 (XXX)` 등
_BITHUMB_TITLE_RE = re.compile(
    r'(?:신규|원화 마켓).*?상장.*?\(([A-Z0-9]{2,10})\)'
)


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------


@dataclass
class ListingEvent:
    """감지된 신규 상장 이벤트."""

    ts: float
    exchange: str                  # 'upbit' | 'bithumb'
    notice_id: str                 # 원본 공지 id (str 로 정규화)
    ticker: str                    # 파싱된 base symbol (예: 'CHIP')
    title: str
    url: str
    binance_perp: bool = False
    bybit_perp: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            'ts': int(self.ts),
            'exchange': self.exchange,
            'id': self.notice_id,
            'ticker': self.ticker,
            'title': self.title,
            'url': self.url,
            'binance_perp': self.binance_perp,
            'bybit_perp': self.bybit_perp,
        }


@dataclass
class _PerpCache:
    """Binance/Bybit USDT-M perp base symbol 화이트리스트."""

    binance: set[str] = field(default_factory=set)
    bybit: set[str] = field(default_factory=set)
    last_refresh_ts: float = 0.0


# ----------------------------------------------------------------------
# HTTP 헤더
# ----------------------------------------------------------------------

_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def _upbit_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://upbit.com',
        'Referer': 'https://upbit.com/',
        'User-Agent': _BROWSER_UA,
        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    }


def _bithumb_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://feed.bithumb.com',
        'Referer': 'https://feed.bithumb.com/',
        'User-Agent': _BROWSER_UA,
        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }


def _plain_headers() -> dict[str, str]:
    return {
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': _BROWSER_UA,
    }


# ----------------------------------------------------------------------
# 감지기 본체
# ----------------------------------------------------------------------


class ListingDetector:
    """Upbit + Bithumb 공지 고빈도 폴링 → 신규 상장 티커 감지.

    사용 예:
        detector = ListingDetector(telegram_service=telegram)
        await detector.start()
        ...
        print(detector.status())
        await detector.stop()

    Phase 1 정책: **실행 경로 없음**. 감지된 이벤트는
    log + Telegram + JSONL append 로만 전달된다. Phase 2 에서 poller 를
    주입하여 사전 선물 포지션 헷지, Phase 3 에서 binance/bybit 실주문을
    도입한다.
    """

    def __init__(
        self,
        poller: Any = None,
        telegram_service: Any = None,
        *,
        upbit_poll_ms: int | None = None,
        bithumb_poll_ms: int | None = None,
        perp_refresh_sec: float | None = None,
        http_timeout: float | None = None,
        history_path: str | None = None,
        seen_path: str | None = None,
        enabled: bool | None = None,
        exchange_whitelist: str | None = None,
    ) -> None:
        # poller 는 Phase 2 에서 사용할 준비용 — Phase 1 에서는 무시
        self.poller = poller
        self.telegram = telegram_service

        self.enabled: bool = (
            enabled
            if enabled is not None
            else _bool_env('LISTING_DETECTOR_ENABLED', True)
        )
        upbit_ms = (
            upbit_poll_ms
            if upbit_poll_ms is not None
            else _int_env('LISTING_UPBIT_POLL_MS', _DEFAULT_UPBIT_POLL_MS)
        )
        bithumb_ms = (
            bithumb_poll_ms
            if bithumb_poll_ms is not None
            else _int_env('LISTING_BITHUMB_POLL_MS', _DEFAULT_BITHUMB_POLL_MS)
        )
        self.upbit_poll_sec: float = max(0.1, upbit_ms / 1000.0)
        self.bithumb_poll_sec: float = max(0.1, bithumb_ms / 1000.0)
        self.perp_refresh_sec: float = (
            perp_refresh_sec
            if perp_refresh_sec is not None
            else _float_env('LISTING_PERP_WHITELIST_REFRESH_SEC', _DEFAULT_PERP_REFRESH_SEC)
        )
        self.http_timeout: float = (
            http_timeout
            if http_timeout is not None
            else _float_env('LISTING_HTTP_TIMEOUT_SEC', _DEFAULT_HTTP_TIMEOUT)
        )
        self.history_path: Path = Path(
            history_path
            if history_path is not None
            else _str_env('LISTING_HISTORY_PATH', _DEFAULT_HISTORY_PATH)
        )
        self.seen_path: Path = Path(
            seen_path
            if seen_path is not None
            else _str_env('LISTING_SEEN_PATH', _DEFAULT_SEEN_PATH)
        )

        whitelist_raw = (
            exchange_whitelist
            if exchange_whitelist is not None
            else _str_env('LISTING_EXCHANGE_WHITELIST', _DEFAULT_EXCHANGE_WHITELIST)
        )
        exchanges = {e.strip().lower() for e in whitelist_raw.split(',') if e.strip()}
        self.upbit_enabled: bool = 'upbit' in exchanges
        self.bithumb_enabled: bool = 'bithumb' in exchanges

        # 런타임 상태
        self._seen_ids: set[tuple[str, str]] = set()
        self._perp_cache: _PerpCache = _PerpCache()
        self._session: CurlAsyncSession | None = None
        self._upbit_task: asyncio.Task | None = None
        self._bithumb_task: asyncio.Task | None = None
        self._perp_task: asyncio.Task | None = None
        self._running: bool = False
        self._write_lock = asyncio.Lock()

        # 통계
        self._total_polls_upbit: int = 0
        self._total_polls_bithumb: int = 0
        self._total_detections: int = 0
        self._last_detection_ts: float = 0.0
        self._last_upbit_id: str = ''
        self._last_bithumb_id: str = ''
        self._last_error_upbit: str = ''
        self._last_error_bithumb: str = ''
        self._total_errors_upbit: int = 0
        self._total_errors_bithumb: int = 0

        # Cloudflare 403 연속 카운트
        self._cf_first_fail_upbit: float = 0.0
        self._cf_first_fail_bithumb: float = 0.0

        # 인프로세스 리스너 (Phase 2 executor 등). 감지 즉시 콜백.
        # 콜백은 동기/async 둘 다 허용. 예외는 catch 해서 다른 리스너 보호.
        self._listeners: list[ListingListener] = []

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if not self.enabled:
            logger.info('ListingDetector disabled via env; not starting loop')
            return

        self._running = True
        self._session = CurlAsyncSession(
            impersonate='chrome124',
            timeout=self.http_timeout,
        )

        # seen_ids 영속화 복원 (재시작 시 중복 알림 방지)
        self._load_seen()

        # history 파일용 디렉토리 생성
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('ListingDetector path setup failed: %s', exc)

        # perp 화이트리스트 초기 로딩 (루프 진입 전 best-effort)
        try:
            await self._refresh_perp_whitelist()
        except Exception as exc:  # noqa: BLE001
            logger.warning('ListingDetector initial perp whitelist failed: %s', exc)

        if self.upbit_enabled:
            self._upbit_task = asyncio.create_task(
                self._upbit_loop(), name='listing_detector_upbit'
            )
        if self.bithumb_enabled:
            self._bithumb_task = asyncio.create_task(
                self._bithumb_loop(), name='listing_detector_bithumb'
            )
        self._perp_task = asyncio.create_task(
            self._perp_loop(), name='listing_detector_perp'
        )

        logger.info(
            'ListingDetector started (upbit=%s %.2fs, bithumb=%s %.2fs, perp_refresh=%.0fs)',
            self.upbit_enabled,
            self.upbit_poll_sec,
            self.bithumb_enabled,
            self.bithumb_poll_sec,
            self.perp_refresh_sec,
        )

    async def stop(self) -> None:
        self._running = False
        tasks = [self._upbit_task, self._bithumb_task, self._perp_task]
        self._upbit_task = None
        self._bithumb_task = None
        self._perp_task = None
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        session = self._session
        self._session = None
        if session is not None:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug('ListingDetector session close: %s', exc)

        logger.info('ListingDetector stopped')

    def status(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'running': self._running,
            'upbit_enabled': self.upbit_enabled,
            'bithumb_enabled': self.bithumb_enabled,
            'upbit_poll_sec': self.upbit_poll_sec,
            'bithumb_poll_sec': self.bithumb_poll_sec,
            'total_polls_upbit': self._total_polls_upbit,
            'total_polls_bithumb': self._total_polls_bithumb,
            'total_errors_upbit': self._total_errors_upbit,
            'total_errors_bithumb': self._total_errors_bithumb,
            'total_detections': self._total_detections,
            'last_detection_ts': self._last_detection_ts,
            'last_upbit_id': self._last_upbit_id,
            'last_bithumb_id': self._last_bithumb_id,
            'last_error_upbit': self._last_error_upbit,
            'last_error_bithumb': self._last_error_bithumb,
            'perp_whitelist_binance_count': len(self._perp_cache.binance),
            'perp_whitelist_bybit_count': len(self._perp_cache.bybit),
            'perp_whitelist_last_refresh': self._perp_cache.last_refresh_ts,
            'seen_count': len(self._seen_ids),
            'history_path': str(self.history_path),
            'seen_path': str(self.seen_path),
        }

    def add_listener(self, listener: ListingListener) -> None:
        """신규 상장 감지 시 호출될 in-process 콜백을 등록한다.

        콜백 시그니처: ``fn(event_json: dict) -> None | Awaitable[None]``
        예외가 발생해도 다른 리스너 실행은 계속된다 (감지 루프 보호).
        """
        if listener is None:
            return
        if listener in self._listeners:
            return
        self._listeners.append(listener)

    def remove_listener(self, listener: ListingListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """history jsonl 에서 최근 N 개를 읽는다 (대시보드 용)."""
        if limit <= 0 or not self.history_path.exists():
            return []
        try:
            with self.history_path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('ListingDetector recent_events read failed: %s', exc)
            return []
        out: list[dict[str, Any]] = []
        # jsonl 끝에서부터 거꾸로 최대 limit 건 파싱
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
    # 폴링 루프
    # ------------------------------------------------------------------

    async def _upbit_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                await self._poll_upbit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._total_errors_upbit += 1
                self._last_error_upbit = f'{type(exc).__name__}: {exc}'
                logger.warning('ListingDetector upbit poll error: %s', exc)
            elapsed = time.monotonic() - started
            sleep = max(0.0, self.upbit_poll_sec - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                raise

    async def _bithumb_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                await self._poll_bithumb()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._total_errors_bithumb += 1
                self._last_error_bithumb = f'{type(exc).__name__}: {exc}'
                logger.warning('ListingDetector bithumb poll error: %s', exc)
            elapsed = time.monotonic() - started
            sleep = max(0.0, self.bithumb_poll_sec - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                raise

    async def _perp_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_perp_whitelist()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('ListingDetector perp refresh error: %s', exc)
            try:
                await asyncio.sleep(max(30.0, self.perp_refresh_sec))
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Upbit
    # ------------------------------------------------------------------

    async def _poll_upbit(self) -> None:
        session = self._session
        if session is None:
            return
        url = (
            'https://api-manager.upbit.com/api/v1/announcements'
            '?os=web&page=1&per_page=20&category=trade'
        )
        try:
            resp = await session.get(url, headers=_upbit_headers())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._total_errors_upbit += 1
            self._last_error_upbit = f'net: {exc}'
            logger.debug('upbit notice GET failed: %s', exc)
            return

        status = getattr(resp, 'status_code', None)
        if status == 429:
            logger.warning('upbit notice 429 rate-limit; backoff %.0fs', _RATE_LIMIT_BACKOFF_SEC)
            await asyncio.sleep(_RATE_LIMIT_BACKOFF_SEC)
            return
        if status == 403:
            now = time.monotonic()
            if self._cf_first_fail_upbit == 0.0:
                self._cf_first_fail_upbit = now
            elif (now - self._cf_first_fail_upbit) >= _CLOUDFLARE_SUSTAINED_SEC:
                logger.error(
                    'upbit Cloudflare 403 sustained %.0fs; pausing %.0fs',
                    now - self._cf_first_fail_upbit,
                    _CLOUDFLARE_PAUSE_SEC,
                )
                self._cf_first_fail_upbit = 0.0
                await asyncio.sleep(_CLOUDFLARE_PAUSE_SEC)
                return
            logger.debug('upbit notice 403 (cloudflare?) — continuing')
            return
        self._cf_first_fail_upbit = 0.0  # 성공적으로 응답 왔으면 리셋

        if status != 200:
            logger.debug('upbit notice HTTP %s', status)
            return

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('upbit notice json parse failed: %s', exc)
            return

        self._total_polls_upbit += 1

        data_node = data.get('data') if isinstance(data, dict) else None
        if not isinstance(data_node, dict):
            return
        items = data_node.get('list') or data_node.get('notices') or []
        if not isinstance(items, list):
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            notice_id = str(item.get('id', ''))
            title = str(item.get('title', '') or '').strip()
            if not notice_id or not title:
                continue
            key = ('upbit', notice_id)
            if key in self._seen_ids:
                continue
            # 정규식 파싱 — 매칭 안 되면 seen 으로만 기록하고 넘어감
            ticker = self._parse_upbit_ticker(title)
            self._remember_seen(key)
            self._last_upbit_id = notice_id
            if ticker is None:
                continue
            url = self._upbit_notice_url(notice_id)
            await self._emit_event('upbit', notice_id, ticker, title, url)

    def _parse_upbit_ticker(self, title: str) -> str | None:
        m = _UPBIT_TITLE_RE.search(title)
        if not m:
            return None
        return m.group(1).upper()

    def _upbit_notice_url(self, notice_id: str) -> str:
        return f'https://upbit.com/service_center/notice?id={notice_id}'

    # ------------------------------------------------------------------
    # Bithumb
    # ------------------------------------------------------------------

    async def _poll_bithumb(self) -> None:
        session = self._session
        if session is None:
            return
        # feed.bithumb.com/notice 는 Next.js HTML 페이지라 JSON 파싱 실패 → 카운터 0 고착.
        # 실제 API 는 api.bithumb.com/v1/notices (flat list, item 에 `pc_url` 포함, `id` 없음).
        url = 'https://api.bithumb.com/v1/notices?count=20'
        try:
            resp = await session.get(url, headers=_bithumb_headers())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._total_errors_bithumb += 1
            self._last_error_bithumb = f'net: {exc}'
            logger.debug('bithumb notice GET failed: %s', exc)
            return

        # 요청 자체는 성공 (서버 응답 받음) — 카운터 증가 (200 이외 status 포함)
        self._total_polls_bithumb += 1
        status = getattr(resp, 'status_code', None)
        if status == 429:
            logger.warning('bithumb notice 429 rate-limit; backoff %.0fs', _RATE_LIMIT_BACKOFF_SEC)
            await asyncio.sleep(_RATE_LIMIT_BACKOFF_SEC)
            return
        if status == 403:
            now = time.monotonic()
            if self._cf_first_fail_bithumb == 0.0:
                self._cf_first_fail_bithumb = now
            elif (now - self._cf_first_fail_bithumb) >= _CLOUDFLARE_SUSTAINED_SEC:
                logger.error(
                    'bithumb Cloudflare 403 sustained %.0fs; pausing %.0fs',
                    now - self._cf_first_fail_bithumb,
                    _CLOUDFLARE_PAUSE_SEC,
                )
                self._cf_first_fail_bithumb = 0.0
                await asyncio.sleep(_CLOUDFLARE_PAUSE_SEC)
                return
            logger.debug('bithumb notice 403 (cloudflare?) — continuing')
            return
        self._cf_first_fail_bithumb = 0.0

        if status != 200:
            logger.debug('bithumb notice HTTP %s', status)
            return

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('bithumb notice json parse failed: %s', exc)
            return

        # api.bithumb.com/v1/notices 는 flat list 반환. 과거 feed.bithumb.com 구조
        # ({list: [...]} / {data: {list: [...]}}) 도 방어적으로 지원.
        items: Any = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get('list')
            if items is None:
                inner = data.get('data')
                if isinstance(inner, dict):
                    items = inner.get('list') or inner.get('notices')
        if not isinstance(items, list):
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            # api.bithumb.com 는 `id` 필드 없고 `pc_url` 끝자리가 공지 id.
            # 예: "https://feed.bithumb.com/notice/1652762" → "1652762"
            notice_id = str(item.get('id') or '').strip()
            if not notice_id:
                pc_url = str(item.get('pc_url') or '').strip()
                if pc_url:
                    notice_id = pc_url.rstrip('/').rsplit('/', 1)[-1]
            title = str(item.get('title', '') or '').strip()
            if not notice_id or not title:
                continue
            key = ('bithumb', notice_id)
            if key in self._seen_ids:
                continue
            ticker = self._parse_bithumb_ticker(title)
            self._remember_seen(key)
            self._last_bithumb_id = notice_id
            if ticker is None:
                continue
            url = self._bithumb_notice_url(notice_id)
            await self._emit_event('bithumb', notice_id, ticker, title, url)

    def _parse_bithumb_ticker(self, title: str) -> str | None:
        m = _BITHUMB_TITLE_RE.search(title)
        if not m:
            return None
        return m.group(1).upper()

    def _bithumb_notice_url(self, notice_id: str) -> str:
        return f'https://feed.bithumb.com/notice/{notice_id}'

    # ------------------------------------------------------------------
    # 이벤트 기록/알림
    # ------------------------------------------------------------------

    async def _emit_event(
        self,
        exchange: str,
        notice_id: str,
        ticker: str,
        title: str,
        url: str,
    ) -> None:
        event = ListingEvent(
            ts=time.time(),
            exchange=exchange,
            notice_id=notice_id,
            ticker=ticker,
            title=title,
            url=url,
            binance_perp=(ticker in self._perp_cache.binance),
            bybit_perp=(ticker in self._perp_cache.bybit),
        )

        self._total_detections += 1
        self._last_detection_ts = event.ts

        logger.info(
            '[listing] %s %s id=%s binance=%s bybit=%s title=%s',
            exchange,
            ticker,
            notice_id,
            event.binance_perp,
            event.bybit_perp,
            title[:120],
        )

        await self._append_history(event)
        self._persist_seen_safe()
        await self._notify(event)
        await self._dispatch_listeners(event)

    async def _dispatch_listeners(self, event: ListingEvent) -> None:
        """등록된 in-process 리스너에 이벤트를 전달한다 (Phase 2+ executor 용).

        각 리스너는 동기/async 모두 허용. 예외는 catch 후 로그만 남긴다 —
        하나의 리스너 실패가 감지 루프 전체를 막지 않도록 보호.
        """
        if not self._listeners:
            return
        payload = event.to_json()
        for listener in list(self._listeners):
            try:
                result = listener(payload)
                if asyncio.iscoroutine(result):
                    # 감지 루프가 executor 실행을 기다리지 않도록 백그라운드 태스크로 분리
                    asyncio.create_task(
                        self._run_listener_coro(result, listener),
                        name='listing_listener_dispatch',
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'ListingDetector listener sync error: %s (listener=%s)',
                    exc,
                    getattr(listener, '__name__', repr(listener)),
                )

    @staticmethod
    async def _run_listener_coro(coro: Awaitable[None], listener: ListingListener) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'ListingDetector listener async error: %s (listener=%s)',
                exc,
                getattr(listener, '__name__', repr(listener)),
            )

    async def _append_history(self, event: ListingEvent) -> None:
        async with self._write_lock:
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                with self.history_path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(event.to_json(), ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('ListingDetector history append failed: %s', exc)

    async def _notify(self, event: ListingEvent) -> None:
        if self.telegram is None:
            return
        binance_mark = 'present' if event.binance_perp else 'absent'
        bybit_mark = 'present' if event.bybit_perp else 'absent'
        text = (
            '🚨 상장 감지!\n'
            f'{event.exchange} {event.ticker}\n'
            f'{event.title}\n'
            f'{event.url}\n'
            f'Binance futures: {binance_mark}\n'
            f'Bybit futures: {bybit_mark}'
        )
        try:
            # futures_futures_scanner 와 동일한 컨벤션 사용
            send = getattr(self.telegram, '_send_message', None)
            if send is None:
                send = getattr(self.telegram, 'send_message', None)
            if send is None:
                logger.debug('ListingDetector telegram has no send method')
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('ListingDetector telegram err: %s', exc)

    # ------------------------------------------------------------------
    # Perp 화이트리스트 캐시
    # ------------------------------------------------------------------

    async def _refresh_perp_whitelist(self) -> None:
        session = self._session
        if session is None:
            return
        binance_task = asyncio.create_task(self._fetch_binance_perp(session))
        bybit_task = asyncio.create_task(self._fetch_bybit_perp(session))
        results = await asyncio.gather(binance_task, bybit_task, return_exceptions=True)

        bn_result, by_result = results
        if isinstance(bn_result, set) and bn_result:
            self._perp_cache.binance = bn_result
        elif isinstance(bn_result, Exception):
            logger.debug('binance perp fetch err: %s', bn_result)

        if isinstance(by_result, set) and by_result:
            self._perp_cache.bybit = by_result
        elif isinstance(by_result, Exception):
            logger.debug('bybit perp fetch err: %s', by_result)

        self._perp_cache.last_refresh_ts = time.time()
        logger.debug(
            'ListingDetector perp cache: binance=%d bybit=%d',
            len(self._perp_cache.binance),
            len(self._perp_cache.bybit),
        )

    async def _fetch_binance_perp(self, session: CurlAsyncSession) -> set[str]:
        url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
        try:
            resp = await session.get(url, headers=_plain_headers())
        except Exception as exc:  # noqa: BLE001
            logger.debug('binance exchangeInfo GET failed: %s', exc)
            return set()
        if getattr(resp, 'status_code', None) != 200:
            return set()
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('binance exchangeInfo json parse failed: %s', exc)
            return set()
        out: set[str] = set()
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

    async def _fetch_bybit_perp(self, session: CurlAsyncSession) -> set[str]:
        url = (
            'https://api.bybit.com/v5/market/instruments-info'
            '?category=linear&status=Trading&limit=1000'
        )
        try:
            resp = await session.get(url, headers=_plain_headers())
        except Exception as exc:  # noqa: BLE001
            logger.debug('bybit instruments-info GET failed: %s', exc)
            return set()
        if getattr(resp, 'status_code', None) != 200:
            return set()
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('bybit instruments-info json parse failed: %s', exc)
            return set()
        out: set[str] = set()
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
    # seen_ids 영속화
    # ------------------------------------------------------------------

    def _remember_seen(self, key: tuple[str, str]) -> None:
        self._seen_ids.add(key)
        # 하드 캡 초과 시 오래된 1/4 제거 (순서가 없는 set 이라 임의 pop)
        if len(self._seen_ids) > _SEEN_HARD_CAP:
            for _ in range(_SEEN_HARD_CAP // 4):
                try:
                    self._seen_ids.pop()
                except KeyError:
                    break

    def _persist_seen_safe(self) -> None:
        try:
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [{'exchange': ex, 'id': nid} for ex, nid in self._seen_ids]
            with self.seen_path.open('w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug('ListingDetector seen persist failed: %s', exc)

    def _load_seen(self) -> None:
        if not self.seen_path.exists():
            return
        try:
            with self.seen_path.open('r', encoding='utf-8') as f:
                payload = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning('ListingDetector seen load failed: %s', exc)
            return
        if not isinstance(payload, list):
            return
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            ex = entry.get('exchange')
            nid = entry.get('id')
            if isinstance(ex, str) and isinstance(nid, str):
                self._seen_ids.add((ex, nid))
        logger.info('ListingDetector loaded %d seen ids', len(self._seen_ids))


# ----------------------------------------------------------------------
# 싱글톤 접근 (선택)
# ----------------------------------------------------------------------

_detector_singleton: ListingDetector | None = None
_detector_singleton_lock = asyncio.Lock()


async def get_detector() -> ListingDetector:
    global _detector_singleton
    if _detector_singleton is not None:
        return _detector_singleton
    async with _detector_singleton_lock:
        if _detector_singleton is None:
            _detector_singleton = ListingDetector()
    return _detector_singleton
