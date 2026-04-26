"""4단계 폴링 루프 — BBO(3s), 네트워크(10s), 출금한도(30s), 티커 탐색(5m).

전체 코인 갭 감시: fetch_tickers()로 거래소당 1회 호출 → 전체 갭 계산 → 텔레그램 알림.
브라우저(WebSocket) 연결 없이도 텔레그램 알림이 독립 동작한다.
"""

import asyncio
import logging
import time

from backend.exchanges.types import (
    BBO,
    BithumbData,
    ExchangeData,
    GapResult,
    NetworkInfo,
    WithdrawalLimit,
)
from backend.exchanges import manager as exchange_manager
from backend.exchanges.bithumb_private import (
    discover_bithumb_tickers,
    fetch_all_bithumb_bbos,
    fetch_all_bithumb_network_info,
    fetch_usdt_krw,
    fetch_withdrawal_limit,
)
from backend.services.gap_calculator import build_gap_result
from backend.services.network_watcher import NetworkWatchService
from backend.services.telegram_bot import TelegramAlertService
from backend import config

logger = logging.getLogger(__name__)


class PollerService:
    """4개 주기로 데이터를 폴링하여 공유 상태를 갱신한다.

    티커 관리 이원화:
      - _all_tickers: 빗썸 전체 상장 코인 (자동 탐색, 텔레그램 알림 대상)
      - _ws_tickers: WebSocket 구독 티커 (대시보드 상세 정보용)
    """

    def __init__(self) -> None:
        self._all_tickers: set[str] = set()
        self._ws_tickers: set[str] = set()
        self._state: dict[str, GapResult] = {}
        self._withdrawal_cache: dict[str, WithdrawalLimit | None] = {}
        # 네트워크 캐시: {exchange_name: {currency: [NetworkInfo, ...]}}
        self._network_cache: dict[str, dict[str, list[NetworkInfo]]] = {}
        self._telegram = TelegramAlertService()
        self._network_watcher = NetworkWatchService()
        self._tasks: list[asyncio.Task] = []
        self._immediate_withdrawal_tasks: dict[str, asyncio.Task] = {}
        self._running = False

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    @property
    def state(self) -> dict[str, GapResult]:
        return self._state

    @property
    def network_cache(self) -> dict[str, dict[str, list[NetworkInfo]]]:
        return self._network_cache

    @property
    def network_watcher(self) -> NetworkWatchService:
        return self._network_watcher

    def get_price_mute_items(self) -> list[dict[str, str]]:
        return self._telegram.price_mute.get_items()

    def add_price_mute_item(self, exchange: str, ticker: str) -> bool:
        return self._telegram.price_mute.add_item(exchange, ticker)

    def remove_price_mute_item(self, exchange: str, ticker: str) -> bool:
        return self._telegram.price_mute.remove_item(exchange, ticker)

    def subscribe_ticker(self, ticker: str) -> None:
        """WebSocket 구독 티커를 추가한다 (대시보드용)."""
        key = ticker.upper()
        is_new = key not in self._ws_tickers
        self._ws_tickers.add(key)
        logger.info('WS ticker subscribed: %s', key)
        if is_new:
            self._schedule_immediate_withdrawal_refresh(key)

    def unsubscribe_ticker(self, ticker: str) -> None:
        """WebSocket 구독 티커를 제거한다."""
        key = ticker.upper()
        self._ws_tickers.discard(key)
        task = self._immediate_withdrawal_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        logger.info('WS ticker unsubscribed: %s', key)

    def get_all_tickers(self) -> list[str]:
        """빗썸 전체 상장 티커 목록을 반환한다."""
        return sorted(self._all_tickers)

    def get_ws_tickers(self) -> list[str]:
        """현재 WebSocket 구독 중인 티커 목록을 반환한다."""
        return sorted(self._ws_tickers)

    async def start(self) -> None:
        """4개 폴링 루프를 비동기 태스크로 시작한다."""
        if self._running:
            return
        self._running = True

        # 최초 티커 탐색 (루프 시작 전)
        try:
            self._all_tickers = await discover_bithumb_tickers()
            logger.info('Initial ticker discovery: %d tickers', len(self._all_tickers))
        except Exception as exc:
            logger.error('Initial ticker discovery failed: %s', exc)

        self._tasks = [
            asyncio.create_task(self._bbo_loop(), name='poller_bbo'),
            asyncio.create_task(self._network_loop(), name='poller_network'),
            asyncio.create_task(self._withdrawal_loop(), name='poller_withdrawal'),
            asyncio.create_task(self._discovery_loop(), name='poller_discovery'),
            asyncio.create_task(self._loan_support_loop(), name='poller_loan_support'),
        ]
        logger.info('PollerService started with %d tickers', len(self._all_tickers))

    async def stop(self) -> None:
        """모든 폴링 태스크를 취소한다."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        immediate_tasks = list(self._immediate_withdrawal_tasks.values())
        for task in immediate_tasks:
            task.cancel()
        if immediate_tasks:
            await asyncio.gather(*immediate_tasks, return_exceptions=True)
        self._immediate_withdrawal_tasks.clear()
        logger.info('PollerService stopped')

    # ------------------------------------------------------------------
    # 폴링 루프
    # ------------------------------------------------------------------

    async def _bbo_loop(self) -> None:
        """BBO 폴링 루프 — 3초마다 전체 코인 갭 계산.

        API 호출: 빗썸 fetch_tickers 1회 + USDT/KRW 1회
                 + 거래소당 spot 1회 + futures 1회 = ~17회/사이클
        """
        while self._running:
            start = time.monotonic()
            if self._all_tickers or self._ws_tickers:
                try:
                    await self._poll_all_bbos()
                    await self._telegram.check_and_alert(self._state)
                except Exception as exc:
                    logger.error('BBO loop error: %s', exc)

            elapsed = time.monotonic() - start
            sleep_time = max(0.0, config.BBO_POLL_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

    async def _network_loop(self) -> None:
        """네트워크 정보 폴링 루프 — 10초마다 조회.

        ws_tickers(대시보드) 또는 network_watcher(감시 리스트)가 있으면 동작.
        브라우저가 꺼져 있어도 감시 리스트 항목이 있으면 폴링을 계속한다.
        """
        while self._running:
            start = time.monotonic()
            if self._ws_tickers or self._network_watcher.has_items():
                try:
                    await self._poll_all_networks()
                    # 네트워크 감시 리스트 상태 변화 확인
                    await self._network_watcher.check_changes(self._network_cache)
                except Exception as exc:
                    logger.error('Network loop error: %s', exc)

            elapsed = time.monotonic() - start
            sleep_time = max(0.0, config.NETWORK_POLL_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

    async def _withdrawal_loop(self) -> None:
        """출금한도 폴링 루프 — 30초마다 _ws_tickers에 대해서만 조회 (대시보드 전용)."""
        while self._running:
            start = time.monotonic()
            # Withdrawal limit is a Bithumb-only value; skip non-Bithumb tickers.
            ws_tickers = [ticker for ticker in self._ws_tickers if ticker in self._all_tickers]
            if ws_tickers:
                try:
                    await asyncio.gather(
                        *[self._poll_withdrawal(ticker) for ticker in ws_tickers],
                        return_exceptions=True,
                    )
                except Exception as exc:
                    logger.error('Withdrawal loop error: %s', exc)

            elapsed = time.monotonic() - start
            sleep_time = max(0.0, config.WITHDRAWAL_LIMIT_POLL_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

    async def _discovery_loop(self) -> None:
        """티커 탐색 루프 — 5분마다 빗썸 상장 목록 갱신."""
        while self._running:
            await asyncio.sleep(config.TICKER_DISCOVERY_INTERVAL)
            try:
                new_tickers = await discover_bithumb_tickers()
                added = new_tickers - self._all_tickers
                removed = self._all_tickers - new_tickers
                self._all_tickers = new_tickers

                if added:
                    logger.info('New tickers discovered: %s', sorted(added))
                if removed:
                    logger.info('Tickers delisted: %s', sorted(removed))
                    for t in removed:
                        self._state.pop(t, None)
                        self._withdrawal_cache.pop(t, None)
                        task = self._immediate_withdrawal_tasks.pop(t, None)
                        if task and not task.done():
                            task.cancel()
            except Exception as exc:
                logger.error('Discovery loop error: %s', exc)

    async def _loan_support_loop(self) -> None:
        """Crypto loan 지원 캐시를 저빈도로 갱신한다."""
        while self._running:
            await asyncio.sleep(config.LOAN_CACHE_REFRESH_INTERVAL)
            try:
                await exchange_manager.refresh_exchange_loan_cache()
            except Exception as exc:
                logger.error('Loan support loop error: %s', exc)

    # ------------------------------------------------------------------
    # Bulk 폴링 헬퍼
    # ------------------------------------------------------------------

    async def _poll_all_bbos(self) -> None:
        """전체 코인 BBO를 bulk로 수집하고 갭을 계산한다.

        1. 빗썸 fetch_tickers() → 전체 KRW 쌍 BBO
        2. USDT/KRW 조회
        3. 해외 거래소별 fetch_all_bbos() (spot + futures 각 1회)
        4. 전체 티커에 대해 갭 계산
        """
        # 병렬 조회: 빗썸 BBO + USDT/KRW + 거래소별 bulk BBO
        bithumb_task = asyncio.create_task(fetch_all_bithumb_bbos())
        usdt_krw_task = asyncio.create_task(fetch_usdt_krw())
        exchange_tasks: dict[str, asyncio.Task] = {}
        for name in exchange_manager.ALL_EXCHANGES:
            exchange_tasks[name] = asyncio.create_task(
                exchange_manager.fetch_all_bbos(name)
            )

        all_tasks = [bithumb_task, usdt_krw_task] + list(exchange_tasks.values())
        await asyncio.gather(*all_tasks, return_exceptions=True)

        # 결과 수집
        bithumb_bbos: dict[str, BBO] = {}
        if not isinstance(bithumb_task.result(), Exception):
            bithumb_bbos = bithumb_task.result()

        usdt_krw: float | None = None
        if not isinstance(usdt_krw_task.result(), Exception):
            usdt_krw = usdt_krw_task.result()

        # 거래소별 BBO: {exchange: {ticker: {"spot": BBO|None, "futures": BBO|None}}}
        exchange_bbos: dict[str, dict[str, dict[str, BBO | None]]] = {}
        for name, task in exchange_tasks.items():
            if not isinstance(task.result(), Exception):
                exchange_bbos[name] = task.result()
            else:
                exchange_bbos[name] = {}

        # Build results for both discovered Bithumb listings and WS-only tickers.
        tickers_to_process = self._all_tickers | self._ws_tickers
        for ticker in tickers_to_process:
            bithumb_bbo = bithumb_bbos.get(ticker)

            # 기존 withdrawal_limit 보존
            existing = self._state.get(ticker)
            withdrawal_limit = self._withdrawal_cache.get(ticker)
            if withdrawal_limit is None and existing:
                withdrawal_limit = existing.bithumb.withdrawal_limit

            bithumb_networks = self._network_cache.get('bithumb', {}).get(ticker, [])

            bithumb_data = BithumbData(
                ask=bithumb_bbo.ask if bithumb_bbo is not None else None,
                usdt_krw_last=usdt_krw,
                withdrawal_limit=withdrawal_limit,
                networks=bithumb_networks,
            )

            # 거래소 데이터 조립
            exchange_data_map: dict[str, ExchangeData] = {}
            for exchange_name in exchange_manager.ALL_EXCHANGES:
                ex_bbos = exchange_bbos.get(exchange_name, {}).get(ticker, {})
                spot_bbo = ex_bbos.get('spot') if isinstance(ex_bbos, dict) else None
                futures_bbo = ex_bbos.get('futures') if isinstance(ex_bbos, dict) else None

                cached_networks = self._network_cache.get(exchange_name, {}).get(ticker, [])

                exchange_data_map[exchange_name] = ExchangeData(
                    exchange=exchange_name,
                    spot_bbo=spot_bbo,
                    futures_bbo=futures_bbo,
                    spot_supported=spot_bbo is not None,
                    futures_supported=futures_bbo is not None,
                    networks=cached_networks,
                    margin=exchange_manager.get_spot_margin_info(exchange_name, ticker),
                    loan=exchange_manager.get_crypto_loan_info(exchange_name, ticker),
                )

            gap_result = build_gap_result(ticker, bithumb_data, exchange_data_map)
            self._state[ticker] = gap_result

    async def _poll_all_networks(self) -> None:
        """전체 통화 네트워크 정보를 bulk로 수집한다."""
        # 빗썸 + 거래소별 bulk 네트워크 조회
        bithumb_task = asyncio.create_task(fetch_all_bithumb_network_info())
        exchange_tasks: dict[str, asyncio.Task] = {}
        for name in exchange_manager.ALL_EXCHANGES:
            exchange_tasks[name] = asyncio.create_task(
                exchange_manager.fetch_all_network_info(name)
            )

        all_tasks = [bithumb_task] + list(exchange_tasks.values())
        await asyncio.gather(*all_tasks, return_exceptions=True)

        # 빗썸 결과
        if not isinstance(bithumb_task.result(), Exception):
            self._network_cache['bithumb'] = bithumb_task.result()

        # 거래소 결과
        for name, task in exchange_tasks.items():
            if not isinstance(task.result(), Exception):
                self._network_cache[name] = task.result()

        logger.debug('Network cache updated for all exchanges')

    async def _poll_withdrawal(self, ticker: str) -> None:
        """단일 티커의 빗썸 출금한도를 조회한다 (대시보드 전용)."""
        limit = await fetch_withdrawal_limit(ticker)
        self._withdrawal_cache[ticker] = limit

        if ticker in self._state:
            existing = self._state[ticker]
            updated_bithumb = BithumbData(
                ask=existing.bithumb.ask,
                usdt_krw_last=existing.bithumb.usdt_krw_last,
                withdrawal_limit=limit,
                networks=existing.bithumb.networks,
            )
            self._state[ticker] = GapResult(
                ticker=existing.ticker,
                timestamp=existing.timestamp,
                bithumb=updated_bithumb,
                exchanges=existing.exchanges,
            )
        logger.debug('Withdrawal limit updated for %s', ticker)

    def _schedule_immediate_withdrawal_refresh(self, ticker: str) -> None:
        """새 구독 티커의 출금한도를 즉시 1회 조회한다."""
        if not self._running or ticker not in self._all_tickers:
            return

        existing_task = self._immediate_withdrawal_tasks.get(ticker)
        if existing_task and not existing_task.done():
            return

        task = asyncio.create_task(
            self._poll_withdrawal(ticker),
            name=f'poller_withdrawal_immediate_{ticker.lower()}',
        )
        self._immediate_withdrawal_tasks[ticker] = task

        def _cleanup(done_task: asyncio.Task, symbol: str = ticker) -> None:
            current = self._immediate_withdrawal_tasks.get(symbol)
            if current is done_task:
                self._immediate_withdrawal_tasks.pop(symbol, None)

            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return

            if exc:
                logger.debug('Immediate withdrawal refresh failed for %s: %s', symbol, exc)

        task.add_done_callback(_cleanup)
