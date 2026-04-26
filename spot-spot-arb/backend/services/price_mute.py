"""Price alert mute list persistence service."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

MUTE_LIST_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..', 'price_mute_list.json',
)


@dataclass(frozen=True)
class PriceMuteItem:
    exchange: str
    ticker: str

    @property
    def key(self) -> str:
        return f'{self.exchange}:{self.ticker}'


class PriceMuteService:
    """Stores muted ticker+exchange pairs for price-gap Telegram alerts."""

    def __init__(self) -> None:
        self._items: list[PriceMuteItem] = []
        self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(MUTE_LIST_FILE):
                return
            with open(MUTE_LIST_FILE, 'r', encoding='utf-8') as file:
                data = json.load(file)

            loaded: list[PriceMuteItem] = []
            seen: set[str] = set()
            for raw in data.get('items', []):
                exchange = str(raw.get('exchange', '')).strip().lower()
                ticker = str(raw.get('ticker', '')).strip().upper()
                if not exchange or not ticker:
                    continue
                item = PriceMuteItem(exchange=exchange, ticker=ticker)
                if item.key in seen:
                    continue
                seen.add(item.key)
                loaded.append(item)

            self._items = loaded
            logger.info('Loaded %d price mute items', len(self._items))
        except Exception as exc:
            logger.error('Failed to load price mute list: %s', exc)

    def _save(self) -> None:
        try:
            data = {'items': [asdict(item) for item in self._items]}
            with open(MUTE_LIST_FILE, 'w', encoding='utf-8') as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error('Failed to save price mute list: %s', exc)

    def get_items(self) -> list[dict[str, str]]:
        return [asdict(item) for item in self._items]

    def add_item(self, exchange: str, ticker: str) -> bool:
        item = PriceMuteItem(
            exchange=exchange.lower(),
            ticker=ticker.upper(),
        )
        if any(existing.key == item.key for existing in self._items):
            return False

        self._items.append(item)
        self._save()
        logger.info('Price mute added: %s', item.key)
        return True

    def remove_item(self, exchange: str, ticker: str) -> bool:
        key = f'{exchange.lower()}:{ticker.upper()}'
        before = len(self._items)
        self._items = [item for item in self._items if item.key != key]
        if len(self._items) == before:
            return False

        self._save()
        logger.info('Price mute removed: %s', key)
        return True

    def is_muted(self, exchange: str, ticker: str) -> bool:
        key = f'{exchange.lower()}:{ticker.upper()}'
        return any(item.key == key for item in self._items)
