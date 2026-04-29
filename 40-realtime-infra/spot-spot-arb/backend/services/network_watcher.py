"""네트워크 상태 변화 감지 및 텔레그램 알림 서비스.

감시 리스트에 등록된 거래소-티커-네트워크 조합의 입출금 상태를
주기적으로 확인하고, 변화 시 텔레그램 메시지를 전송한다.
브라우저가 꺼져 있어도 백엔드 폴링 루프에서 독립 동작한다.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass

from telegram import Bot
from telegram.error import TelegramError

from backend import config
from backend.exchanges.types import NetworkInfo

logger = logging.getLogger(__name__)

WATCHLIST_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..', 'network_watchlist.json',
)

EXCHANGE_DISPLAY = {
    'binance': 'Binance',
    'bybit': 'Bybit',
    'okx': 'OKX',
    'bitget': 'Bitget',
    'gate': 'Gate',
    'htx': 'HTX',
    'upbit': 'Upbit',
    'coinone': 'Coinone',
    'bithumb': 'Bithumb',
}


@dataclass
class NetworkWatchItem:
    """감시 대상 네트워크."""

    exchange: str
    ticker: str
    network: str

    @property
    def key(self) -> str:
        return f'{self.exchange}:{self.ticker}:{self.network}'


class NetworkWatchService:
    """네트워크 상태 변화를 감시하고 텔레그램으로 알린다."""

    def __init__(self) -> None:
        self._items: list[NetworkWatchItem] = []
        # key -> {'deposit': bool, 'withdraw': bool}
        self._prev_states: dict[str, dict[str, bool]] = {}
        self._bot: Bot | None = None

        if config.TELEGRAM_BOT_TOKEN:
            self._bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

        self._load()

    # ------------------------------------------------------------------
    # 영속화
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if os.path.exists(WATCHLIST_FILE):
                with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._items = [
                    NetworkWatchItem(**item) for item in data.get('items', [])
                ]
                logger.info('Loaded %d network watch items', len(self._items))
        except Exception as exc:
            logger.error('Failed to load network watchlist: %s', exc)

    def _save(self) -> None:
        try:
            data = {'items': [asdict(item) for item in self._items]}
            with open(WATCHLIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error('Failed to save network watchlist: %s', exc)

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def has_items(self) -> bool:
        return len(self._items) > 0

    def get_items(self) -> list[dict]:
        return [asdict(item) for item in self._items]

    def add_item(self, exchange: str, ticker: str, network: str) -> bool:
        """감시 항목을 추가한다. 이미 존재하면 False."""
        item = NetworkWatchItem(
            exchange=exchange.lower(),
            ticker=ticker.upper(),
            network=network,
        )
        if any(i.key == item.key for i in self._items):
            return False
        self._items.append(item)
        self._save()
        logger.info('Network watch added: %s', item.key)
        return True

    def remove_item(self, exchange: str, ticker: str, network: str) -> bool:
        """감시 항목을 제거한다. 존재하지 않으면 False."""
        key = f'{exchange.lower()}:{ticker.upper()}:{network}'
        before = len(self._items)
        self._items = [i for i in self._items if i.key != key]
        if len(self._items) < before:
            self._prev_states.pop(key, None)
            self._save()
            logger.info('Network watch removed: %s', key)
            return True
        return False

    # ------------------------------------------------------------------
    # 상태 변화 감지
    # ------------------------------------------------------------------

    async def check_changes(
        self,
        network_cache: dict[str, dict[str, list[NetworkInfo]]],
    ) -> None:
        """네트워크 캐시에서 상태 변화를 감지하고 텔레그램으로 알린다."""
        if not self._items or self._bot is None:
            return

        # 변화가 감지된 항목: {exchange: [NetworkWatchItem, ...]}
        changed_items: dict[str, list[NetworkWatchItem]] = {}

        for item in self._items:
            ex_cache = network_cache.get(item.exchange, {})
            nets = ex_cache.get(item.ticker, [])

            net_info = next(
                (n for n in nets if n.network == item.network), None,
            )
            if net_info is None:
                continue

            current = {'deposit': net_info.deposit, 'withdraw': net_info.withdraw}
            prev = self._prev_states.get(item.key)

            if prev is None:
                # 첫 조회 — 이전 상태 기록만
                self._prev_states[item.key] = current
                continue

            if current != prev:
                self._prev_states[item.key] = current
                if item.exchange not in changed_items:
                    changed_items[item.exchange] = []
                changed_items[item.exchange].append(item)

        # 변화가 있는 항목만 포함하여 메시지 전송
        for exchange, items in changed_items.items():
            message = self._build_message(exchange, items, network_cache)
            if message:
                await self._send_message(message)

    def _build_message(
        self,
        exchange: str,
        items: list[NetworkWatchItem],
        network_cache: dict[str, dict[str, list[NetworkInfo]]],
    ) -> str:
        """변화가 감지된 항목만 포함한 텔레그램 메시지."""
        display_name = EXCHANGE_DISPLAY.get(exchange, exchange)
        lines = [f'\U0001f4cc{display_name} 지갑', '']

        ex_cache = network_cache.get(exchange, {})

        for item in items:
            nets = ex_cache.get(item.ticker, [])
            net_info = next(
                (n for n in nets if n.network == item.network), None,
            )

            if net_info:
                dep = '\u2705' if net_info.deposit else '\u274c'
                wd = '\u2705' if net_info.withdraw else '\u274c'
                lines.append(
                    f'{item.ticker} ({item.network}): '
                    f'\uc785\uae08{dep} \ucd9c\uae08{wd}',
                )
            else:
                lines.append(f'{item.ticker} ({item.network}): 정보 없음')

        return '\n'.join(lines)

    async def _send_message(self, text: str) -> None:
        """텔레그램 메시지를 전송한다."""
        if self._bot is None or not config.TELEGRAM_CHAT_ID:
            return
        try:
            await self._bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
            )
            logger.info('Network change Telegram alert sent')
        except TelegramError as exc:
            logger.error('Failed to send network change alert: %s', exc)
        except Exception as exc:
            logger.error('Unexpected error sending network change alert: %s', exc)
