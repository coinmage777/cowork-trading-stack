"""theddari.com GraphQL 기반 광역 김프/역프 스캐너.

200+ 티커의 Bithumb(KRW) vs Binance(USDT) 갭을 10초 주기로 샘플링해
메모리 상에 보관한다. auto_trigger.py 가 이 모듈의 `extreme_signals()` 를
읽어 hedge 후보를 선별한다.

- 외부 호출자: auto_trigger 등 상위 서비스가 주기적으로 `status()` /
  `extreme_signals()` / `get_kimp()` 를 조회한다.
- 본 모듈은 순수 데이터 레이어다. hedge_trade_service, auto_trigger 등에
  역의존성이 없어야 한다 (순환 import 방지).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import aiohttp  # noqa: F401 — kept for type compatibility
from curl_cffi.requests import AsyncSession as CurlAsyncSession

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 환경 설정
# ----------------------------------------------------------------------

_DEFAULT_API_URL = 'https://api.theddari.com/graphql'
_DEFAULT_POLL_INTERVAL = 10.0
_DEFAULT_EXTREME_THRESHOLD_BPS = 200.0  # 10000 기준 +/- 200 = 2%
_DEFAULT_MAX_TICKERS = 400
_DEFAULT_KRW_RATE_TTL = 30.0             # USDT/KRW 캐시 유지 시간 (초)
_DEFAULT_MARKET_TTL = 3600.0             # 거래소 티커 교집합 캐시 (초)
_DEFAULT_HTTP_TIMEOUT = 10.0
_DEFAULT_CHUNK_SIZE = 200                # 한 번의 POST 당 ticker 수 상한


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


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------


@dataclass
class KimpSnapshot:
    """단일 티커의 최신 김프 샘플."""

    ticker: str
    kimp: float                      # 10000 기준 (10200 = +2% 김프, 9800 = -2% 역프)
    direction: str                   # 'kimp' | 'reverse' | 'flat'
    kr_price: float
    overseas_price: float
    usdt_krw: float
    ts: float


@dataclass
class ScannerStatus:
    enabled: bool = True
    running: bool = False
    total_tickers: int = 0
    last_poll_ts: float = 0.0
    last_poll_elapsed: float = 0.0
    total_polls: int = 0
    total_errors: int = 0
    extreme_count: int = 0
    last_error: str = ''
    krw_rate: float = 0.0
    krw_rate_ts: float = 0.0
    market_intersection_ts: float = 0.0


# ----------------------------------------------------------------------
# GraphQL 정의 (최소 셋)
# ----------------------------------------------------------------------


_QUERY_EXCHANGE_MARKET = (
    'query ExchangeMarket { exchangeMarket { '
    'bithumb { krw } upbit { krw } binance { usdt } } }'
)

_QUERY_KRW_RATE = 'query KrwRate { krwRate { rate } }'

_QUERY_EXCHANGE_TICKERS = (
    'query ExchangeTickers($tickers: [String!]!) { '
    'exchangeTickers(tickers: $tickers) { result tickers { market price } } }'
)


_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def _default_headers() -> dict[str, str]:
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://theddari.com',
        'Referer': 'https://theddari.com/',
        'User-Agent': _BROWSER_UA,
        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    }


# ----------------------------------------------------------------------
# 스캐너 본체
# ----------------------------------------------------------------------


class TheddariScanner:
    """theddari.com GraphQL 스캐너.

    사용 예:
        scanner = TheddariScanner()
        await scanner.start()
        ...
        signals = scanner.extreme_signals(limit=10)
        await scanner.stop()
    """

    def __init__(
        self,
        *,
        api_url: str | None = None,
        poll_interval: float | None = None,
        extreme_threshold_bps: float | None = None,
        max_tickers: int | None = None,
        krw_rate_ttl: float | None = None,
        market_ttl: float | None = None,
        http_timeout: float | None = None,
        chunk_size: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.api_url: str = (
            api_url
            if api_url is not None
            else os.getenv('THEDDARI_API_URL', _DEFAULT_API_URL)
        )
        self.poll_interval: float = (
            poll_interval
            if poll_interval is not None
            else _float_env('THEDDARI_POLL_INTERVAL_SEC', _DEFAULT_POLL_INTERVAL)
        )
        self.extreme_threshold: float = (
            extreme_threshold_bps
            if extreme_threshold_bps is not None
            else _float_env('THEDDARI_EXTREME_THRESHOLD', _DEFAULT_EXTREME_THRESHOLD_BPS)
        )
        self.max_tickers: int = (
            max_tickers
            if max_tickers is not None
            else _int_env('THEDDARI_MAX_TICKERS', _DEFAULT_MAX_TICKERS)
        )
        self.krw_rate_ttl: float = (
            krw_rate_ttl
            if krw_rate_ttl is not None
            else _float_env('THEDDARI_KRW_RATE_TTL_SEC', _DEFAULT_KRW_RATE_TTL)
        )
        self.market_ttl: float = (
            market_ttl
            if market_ttl is not None
            else _float_env('THEDDARI_MARKET_TTL_SEC', _DEFAULT_MARKET_TTL)
        )
        self.http_timeout: float = (
            http_timeout
            if http_timeout is not None
            else _float_env('THEDDARI_HTTP_TIMEOUT_SEC', _DEFAULT_HTTP_TIMEOUT)
        )
        self.chunk_size: int = (
            chunk_size
            if chunk_size is not None
            else _int_env('THEDDARI_CHUNK_SIZE', _DEFAULT_CHUNK_SIZE)
        )
        self.enabled: bool = (
            enabled
            if enabled is not None
            else _bool_env('THEDDARI_ENABLED', True)
        )

        if self.chunk_size <= 0:
            self.chunk_size = _DEFAULT_CHUNK_SIZE
        if self.max_tickers <= 0:
            self.max_tickers = _DEFAULT_MAX_TICKERS
        if self.poll_interval <= 0:
            self.poll_interval = _DEFAULT_POLL_INTERVAL

        # 런타임 상태
        self._intersection: list[str] = []     # 공통 상장 심볼 (대문자, e.g. BTC)
        self._intersection_ts: float = 0.0
        self._krw_rate: float = 0.0
        self._krw_rate_ts: float = 0.0
        self._snapshots: dict[str, KimpSnapshot] = {}

        self._total_polls: int = 0
        self._total_errors: int = 0
        self._last_error: str = ''
        self._last_poll_ts: float = 0.0
        self._last_poll_elapsed: float = 0.0

        self._session: CurlAsyncSession | None = None
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """폴링 루프를 비동기 태스크로 기동한다."""
        if self._running:
            return
        if not self.enabled:
            logger.info('TheddariScanner disabled via env; not starting loop')
            return

        self._running = True
        # curl_cffi로 브라우저 TLS 임퍼소네이트 (Cloudflare 우회)
        self._session = CurlAsyncSession(
            impersonate='chrome124',
            headers=_default_headers(),
            timeout=self.http_timeout,
        )

        # 최초 교집합 수집은 루프 진입 전에 시도 (실패해도 루프는 진행)
        try:
            await self._refresh_intersection(force=True)
        except Exception as exc:  # noqa: BLE001 — 루프 진입 자체는 막지 않는다
            logger.warning(
                'TheddariScanner initial market discovery failed: %s', exc
            )

        self._task = asyncio.create_task(
            self._run_loop(), name='theddari_scanner_loop'
        )
        logger.info(
            'TheddariScanner started (interval=%.1fs, extreme=%.0fbps, max=%d)',
            self.poll_interval,
            self.extreme_threshold,
            self.max_tickers,
        )

    async def stop(self) -> None:
        """폴링 루프를 취소하고 세션을 정리한다."""
        self._running = False
        task = self._task
        self._task = None
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
                logger.debug('TheddariScanner session close error: %s', exc)

        logger.info('TheddariScanner stopped')

    def status(self) -> dict[str, Any]:
        """현재 스캐너 상태 스냅샷."""
        extreme_count = sum(
            1
            for snap in self._snapshots.values()
            if abs(snap.kimp - 10000.0) >= self.extreme_threshold
        )
        return {
            'enabled': self.enabled,
            'running': self._running,
            'api_url': self.api_url,
            'poll_interval': self.poll_interval,
            'extreme_threshold': self.extreme_threshold,
            'max_tickers': self.max_tickers,
            'intersection_count': len(self._intersection),
            'snapshot_count': len(self._snapshots),
            'total_tickers': len(self._snapshots),
            'total_polls': self._total_polls,
            'total_errors': self._total_errors,
            'last_error': self._last_error,
            'last_poll_ts': self._last_poll_ts,
            'last_poll_elapsed': self._last_poll_elapsed,
            'extreme_count': extreme_count,
            'krw_rate': self._krw_rate,
            'krw_rate_ts': self._krw_rate_ts,
            'market_intersection_ts': self._intersection_ts,
        }

    def extreme_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        """|kimp - 10000| 가 임계치 이상인 티커를 심각도 내림차순으로 반환한다."""
        if limit <= 0:
            return []
        out: list[tuple[float, KimpSnapshot]] = []
        threshold = self.extreme_threshold
        for snap in self._snapshots.values():
            severity = abs(snap.kimp - 10000.0)
            if severity >= threshold:
                out.append((severity, snap))
        out.sort(key=lambda item: item[0], reverse=True)
        result: list[dict[str, Any]] = []
        for _, snap in out[:limit]:
            result.append(
                {
                    'ticker': snap.ticker,
                    'kimp': snap.kimp,
                    'direction': snap.direction,
                    'kr_price': snap.kr_price,
                    'overseas_price': snap.overseas_price,
                    'usdt_krw': snap.usdt_krw,
                    'ts': snap.ts,
                }
            )
        return result

    def get_kimp(self, ticker: str) -> float | None:
        """특정 티커의 최신 kimp 값. 없으면 None."""
        if not ticker:
            return None
        snap = self._snapshots.get(ticker.upper())
        return snap.kimp if snap is not None else None

    def get_snapshot(self, ticker: str) -> KimpSnapshot | None:
        """특정 티커의 전체 스냅샷(디버깅/상세 용)."""
        if not ticker:
            return None
        return self._snapshots.get(ticker.upper())

    def all_snapshots(self) -> dict[str, KimpSnapshot]:
        """현재 메모리에 보관된 전체 스냅샷의 얕은 복사본."""
        return dict(self._snapshots)

    async def refresh_intersection(self) -> int:
        """외부에서 티커 교집합 캐시를 강제로 재수집한다."""
        return await self._refresh_intersection(force=True)

    # ------------------------------------------------------------------
    # 폴링 루프
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """10초 주기 메인 루프. 단일 사이클 실패가 전체를 멈추지 않는다."""
        while self._running:
            started = time.monotonic()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._total_errors += 1
                self._last_error = f'{type(exc).__name__}: {exc}'
                logger.warning('TheddariScanner poll error: %s', exc)

            elapsed = time.monotonic() - started
            self._last_poll_elapsed = elapsed
            sleep_time = max(0.0, self.poll_interval - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        """한 사이클: 교집합 갱신(필요 시) → KRW rate → 티커 배치 가격 → kimp 계산."""
        await self._refresh_intersection(force=False)
        if not self._intersection:
            logger.debug('TheddariScanner intersection empty; skip cycle')
            return

        krw_rate = await self._get_krw_rate()
        if krw_rate is None or krw_rate <= 0:
            logger.debug('TheddariScanner krw_rate unavailable; skip cycle')
            return

        markets = self._build_ticker_params(self._intersection)
        if not markets:
            return

        price_map = await self._fetch_ticker_prices(markets)
        if not price_map:
            return

        now = time.time()
        updated: dict[str, KimpSnapshot] = {}
        threshold = self.extreme_threshold
        extreme_hits = 0

        for symbol in self._intersection:
            kr_key = f'bithumb_KRW-{symbol}'
            ov_key = f'binance_{symbol}-USDT'
            kr_price = price_map.get(kr_key)
            overseas_price = price_map.get(ov_key)
            if kr_price is None or overseas_price is None:
                continue
            if kr_price <= 0 or overseas_price <= 0:
                continue

            denom = overseas_price * krw_rate
            if denom <= 0:
                continue
            kimp = kr_price / denom * 10000.0
            if not (kimp == kimp):  # NaN 방어
                continue

            if kimp > 10000.0:
                direction = 'kimp'
            elif kimp < 10000.0:
                direction = 'reverse'
            else:
                direction = 'flat'

            snap = KimpSnapshot(
                ticker=symbol,
                kimp=kimp,
                direction=direction,
                kr_price=kr_price,
                overseas_price=overseas_price,
                usdt_krw=krw_rate,
                ts=now,
            )
            updated[symbol] = snap
            if abs(kimp - 10000.0) >= threshold:
                extreme_hits += 1

        # 사이클 완료: 상태 일괄 교체 (partial poll 도 반영)
        self._snapshots = updated
        self._total_polls += 1
        self._last_poll_ts = now
        logger.debug(
            'TheddariScanner cycle: tickers=%d extreme=%d krw=%.2f',
            len(updated),
            extreme_hits,
            krw_rate,
        )

    # ------------------------------------------------------------------
    # GraphQL 호출
    # ------------------------------------------------------------------

    async def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """GraphQL POST. 실패 시 None 반환 (호출자가 스킵)."""
        session = self._session
        if session is None:
            raise RuntimeError('TheddariScanner session not initialized')

        payload: dict[str, Any] = {
            'operationName': operation_name,
            'query': query,
        }
        if variables is not None:
            payload['variables'] = variables

        try:
            resp = await session.post(self.api_url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    'theddari %s HTTP %s: %s',
                    operation_name,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            data = resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('theddari %s request failed: %s', operation_name, exc)
            return None

        if not isinstance(data, dict):
            logger.warning('theddari %s: non-dict response', operation_name)
            return None
        if data.get('errors'):
            logger.warning(
                'theddari %s GraphQL errors: %s',
                operation_name,
                str(data.get('errors'))[:200],
            )
            return None
        result = data.get('data')
        if not isinstance(result, dict):
            return None
        return result

    # ------------------------------------------------------------------
    # 보조 캐시: 교집합, KRW rate
    # ------------------------------------------------------------------

    async def _refresh_intersection(self, *, force: bool) -> int:
        """bithumb.krw ∩ binance.usdt 교집합을 캐시한다."""
        now = time.monotonic()
        if (
            not force
            and self._intersection
            and (now - self._intersection_ts) < self.market_ttl
        ):
            return len(self._intersection)

        async with self._lock:
            # 락 내부에서 재검사 (중복 호출 방어)
            now2 = time.monotonic()
            if (
                not force
                and self._intersection
                and (now2 - self._intersection_ts) < self.market_ttl
            ):
                return len(self._intersection)

            data = await self._graphql('ExchangeMarket', _QUERY_EXCHANGE_MARKET)
            if data is None:
                return len(self._intersection)

            market = data.get('exchangeMarket') or {}
            bithumb_krw = _extract_symbols(market.get('bithumb'), 'krw')
            binance_usdt = _extract_symbols(market.get('binance'), 'usdt')

            if not bithumb_krw or not binance_usdt:
                logger.warning(
                    'theddari ExchangeMarket empty set (bithumb=%d, binance=%d)',
                    len(bithumb_krw),
                    len(binance_usdt),
                )
                return len(self._intersection)

            intersection = sorted(bithumb_krw & binance_usdt)
            if len(intersection) > self.max_tickers:
                # 알파벳 정렬 상단을 잘라낸다. BTC/ETH 등 대표 심볼 우선 보존 가능.
                intersection = intersection[: self.max_tickers]

            added = len(intersection) - len(self._intersection)
            self._intersection = intersection
            self._intersection_ts = time.monotonic()

            logger.info(
                'TheddariScanner intersection refreshed: %d tickers (delta %+d)',
                len(intersection),
                added,
            )
            return len(intersection)

    async def _get_krw_rate(self) -> float | None:
        """USDT/KRW rate. TTL 내라면 캐시된 값 사용."""
        now = time.monotonic()
        if self._krw_rate > 0 and (now - self._krw_rate_ts) < self.krw_rate_ttl:
            return self._krw_rate

        data = await self._graphql('KrwRate', _QUERY_KRW_RATE)
        if data is None:
            # 네트워크 오류라면 기존 캐시가 있을 때 TTL 외여도 fallback 사용
            if self._krw_rate > 0:
                logger.debug(
                    'theddari KrwRate failed; reusing stale rate %.2f',
                    self._krw_rate,
                )
                return self._krw_rate
            return None

        node = data.get('krwRate') or {}
        raw_rate = node.get('rate')
        try:
            rate = float(raw_rate) if raw_rate is not None else 0.0
        except (TypeError, ValueError):
            rate = 0.0

        if rate <= 0:
            logger.warning('theddari KrwRate returned non-positive: %r', raw_rate)
            if self._krw_rate > 0:
                return self._krw_rate
            return None

        self._krw_rate = rate
        self._krw_rate_ts = now
        return rate

    # ------------------------------------------------------------------
    # Ticker batch fetch
    # ------------------------------------------------------------------

    def _build_ticker_params(self, symbols: Iterable[str]) -> list[str]:
        """교집합 심볼 리스트를 (bithumb_KRW-X, binance_USDT-X) 페어 리스트로 편다."""
        params: list[str] = []
        for sym in symbols:
            if not sym:
                continue
            params.append(f'bithumb_KRW-{sym}')
            params.append(f'binance_{sym}-USDT')
        return params

    async def _fetch_ticker_prices(self, markets: list[str]) -> dict[str, float]:
        """배치 사이즈를 나눠 가격을 조회하고 normalize 한 market→price 맵을 반환."""
        prices: dict[str, float] = {}
        chunk = max(1, self.chunk_size)
        tasks = []
        for start in range(0, len(markets), chunk):
            batch = markets[start : start + chunk]
            tasks.append(self._fetch_ticker_chunk(batch))

        if not tasks:
            return prices

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.debug('TheddariScanner chunk failed: %s', res)
                continue
            if not res:
                continue
            prices.update(res)
        return prices

    async def _fetch_ticker_chunk(self, tickers: list[str]) -> dict[str, float]:
        data = await self._graphql(
            'ExchangeTickers',
            _QUERY_EXCHANGE_TICKERS,
            variables={'tickers': tickers},
        )
        if data is None:
            return {}
        node = data.get('exchangeTickers') or {}
        rows = node.get('tickers') or []
        out: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            market = row.get('market')
            price_raw = row.get('price')
            if not isinstance(market, str):
                continue
            try:
                price = float(price_raw) if price_raw is not None else 0.0
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            # Binance 응답은 'binance_BTCUSDT' 처럼 dash 없이 올 수 있음.
            canonical = _canonicalize_market_key(market)
            if canonical is None:
                continue
            out[canonical] = price
        return out


# ----------------------------------------------------------------------
# 유틸리티
# ----------------------------------------------------------------------


def _extract_symbols(node: Any, quote_key: str) -> set[str]:
    """{'krw': [...]} / {'usdt': [...]} 형태에서 심볼 집합을 추출.

    API 가 대소문자 키를 섞어 보낼 수 있어 key insensitive 로 탐색한다.
    """
    if not isinstance(node, dict):
        return set()
    target = quote_key.lower()
    raw: Any = None
    for k, v in node.items():
        if isinstance(k, str) and k.lower() == target:
            raw = v
            break
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.add(item.strip().upper())
    return out


_DOMESTIC_EXCHANGES = {'upbit', 'bithumb', 'coinone', 'korbit'}
_KNOWN_QUOTES = ('USDT', 'USDC', 'BUSD', 'KRW', 'USD', 'BTC', 'ETH')


def _canonicalize_market_key(market: str) -> str | None:
    """theddari market 키를 표준형으로 맞춘다.

    규칙:
      - 국내 거래소 (upbit/bithumb/coinone/korbit): `{exchange}_KRW-{SYMBOL}`
      - 해외 거래소 (binance 등): `{exchange}_{SYMBOL}-{QUOTE}`

    실제 응답 사례:
      - 'bithumb_KRW-BTC'           → 'bithumb_KRW-BTC'
      - 'binance_BTC-USDT'          → 'binance_BTC-USDT'
      - 'binance_BTCUSDT'           → 'binance_BTC-USDT'
      - 'upbit_KRW-XRP'             → 'upbit_KRW-XRP'
    """
    if not market or '_' not in market:
        return None
    exchange, _, rest = market.partition('_')
    exchange = exchange.lower()
    if not rest:
        return None

    if '-' in rest:
        a, _, b = rest.partition('-')
        a, b = a.upper(), b.upper()
        if not a or not b:
            return None
    else:
        upper = rest.upper()
        a = None
        b = None
        for q in _KNOWN_QUOTES:
            if upper.endswith(q) and len(upper) > len(q):
                a = upper[: -len(q)]
                b = q
                break
        if a is None or b is None:
            return None

    # 도메인 규칙에 맞춰 순서 정규화
    if exchange in _DOMESTIC_EXCHANGES:
        # 국내: QUOTE-BASE. a 또는 b 중 known quote 찾아서 quote 먼저.
        if a in _KNOWN_QUOTES and b not in _KNOWN_QUOTES:
            return f'{exchange}_{a}-{b}'
        if b in _KNOWN_QUOTES and a not in _KNOWN_QUOTES:
            return f'{exchange}_{b}-{a}'
        return f'{exchange}_{a}-{b}'
    else:
        # 해외: BASE-QUOTE. known quote 가 뒤로.
        if a in _KNOWN_QUOTES and b not in _KNOWN_QUOTES:
            return f'{exchange}_{b}-{a}'
        if b in _KNOWN_QUOTES and a not in _KNOWN_QUOTES:
            return f'{exchange}_{a}-{b}'
        return f'{exchange}_{a}-{b}'


# ----------------------------------------------------------------------
# 싱글톤 접근 (선택적)
# ----------------------------------------------------------------------


_scanner_singleton: TheddariScanner | None = None
_scanner_singleton_lock = asyncio.Lock()


async def get_scanner() -> TheddariScanner:
    """프로세스 전역 싱글톤 접근자. 필요한 호출자만 사용하면 된다."""
    global _scanner_singleton
    if _scanner_singleton is not None:
        return _scanner_singleton
    async with _scanner_singleton_lock:
        if _scanner_singleton is None:
            _scanner_singleton = TheddariScanner()
    return _scanner_singleton
