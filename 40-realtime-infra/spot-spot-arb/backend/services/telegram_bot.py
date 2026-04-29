"""텔레그램 알림 서비스."""

import asyncio
import json
import logging
import os
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from backend.exchanges.types import GapResult
from backend.exchanges.bithumb_private import fetch_bithumb_orderbook
from backend.exchanges.manager import fetch_orderbook, KRW_EXCHANGES
from backend.services.gap_calculator import calculate_impact_gap
from backend.services.price_mute import PriceMuteService
from backend.utils.cooldown import CooldownManager
from backend import config

logger = logging.getLogger(__name__)


def _load_alert_filters() -> tuple[set[str], tuple[str, ...]]:
    """`alert_filters.json` 을 로드해 (disabled_keys, disabled_prefixes) 반환.

    파일이 없거나 파싱 실패 시 빈 필터(패스스루).
    """
    # repo root = backend/services/telegram_bot.py 에서 3단계 위
    candidates = [
        Path(__file__).resolve().parent.parent.parent / 'alert_filters.json',
        Path(os.getcwd()) / 'alert_filters.json',
    ]
    for path in candidates:
        try:
            if path.is_file():
                with path.open('r', encoding='utf-8') as fh:
                    data = json.load(fh)
                keys = set(data.get('disabled_keys') or [])
                prefixes = tuple(data.get('disabled_prefixes') or [])
                logger.info(
                    'alert_filters loaded: %d keys, %d prefixes (%s)',
                    len(keys), len(prefixes), path,
                )
                return keys, prefixes
        except Exception as exc:
            logger.warning('Failed to load alert_filters.json at %s: %s', path, exc)
    return set(), tuple()

# 거래소 표시 이름 매핑
EXCHANGE_DISPLAY = {
    'binance': 'Binance',
    'bybit': 'Bybit',
    'okx': 'OKX',
    'bitget': 'Bitget',
    'gate': 'Gate',
    'htx': 'HTX',
    'upbit': 'Upbit',
    'coinone': 'Coinone',
}


class TelegramAlertService:
    """갭 임계값 초과 시 텔레그램 메시지를 전송한다."""

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._cooldown = CooldownManager()
        self._price_mute = PriceMuteService()
        self._disabled_keys, self._disabled_prefixes = _load_alert_filters()

        if config.TELEGRAM_BOT_TOKEN:
            self._bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        else:
            logger.warning('TELEGRAM_BOT_TOKEN not configured — alerts disabled')

    def reload_filters(self) -> None:
        """필터 설정 핫 리로드."""
        self._disabled_keys, self._disabled_prefixes = _load_alert_filters()

    def _is_filtered(self, alert_key: str | None) -> bool:
        if not alert_key:
            return False
        if alert_key in self._disabled_keys:
            return True
        for prefix in self._disabled_prefixes:
            if alert_key.startswith(prefix):
                return True
        return False

    @property
    def price_mute(self) -> PriceMuteService:
        return self._price_mute

    async def check_and_alert(self, gap_results: dict[str, GapResult]) -> None:
        """갭 결과를 검사하고 임계값 초과 시 알림을 전송한다.

        Args:
            gap_results: 티커 -> GapResult 매핑
        """
        if self._bot is None:
            return

        for ticker, result in gap_results.items():
            await self._process_ticker(ticker, result)

    async def _process_ticker(self, ticker: str, result: GapResult) -> None:
        """단일 티커에 대해 알림 여부를 결정하고 메시지를 전송한다.

        기본 2단계 필터:
        1단계: 1호가 갭이 FLOOR < gap <= THRESHOLD인 후보 수집
        2단계: 후보별 5호가 오더북 조회 → impact gap 재계산 → 여전히 유효한 것만 알림
        """
        # --- 1단계: 1호가 갭 스크리닝 ---
        # (exchange_name, market_type, 1호가 gap)
        candidates: list[tuple[str, str, float]] = []

        for exchange_name, ex_data in result.exchanges.items():
            if self._price_mute.is_muted(exchange_name, ticker):
                continue

            if (
                ex_data.spot_gap is not None
                and config.GAP_ALERT_FLOOR < ex_data.spot_gap <= config.GAP_ALERT_THRESHOLD
            ):
                entity_key = f'{ticker}_{exchange_name}_spot'
                if self._cooldown.can_alert(entity_key):
                    candidates.append((exchange_name, 'spot', ex_data.spot_gap))

            if (
                ex_data.futures_gap is not None
                and config.GAP_ALERT_FLOOR < ex_data.futures_gap <= config.GAP_ALERT_THRESHOLD
            ):
                entity_key = f'{ticker}_{exchange_name}_futures'
                if self._cooldown.can_alert(entity_key):
                    candidates.append((exchange_name, 'futures', ex_data.futures_gap))

        if not candidates:
            return

        # impact 재검증을 끄면 1호가 기준만으로 즉시 알림
        if not config.ENABLE_IMPACT_CHECK:
            alerts: dict[str, list[tuple[str, float, float | None]]] = {
                'spot': [],
                'futures': [],
            }
            for exchange_name, market_type, bbo_gap in candidates:
                entity_key = f'{ticker}_{exchange_name}_{market_type}'
                self._cooldown.record_alert(entity_key)
                alerts[market_type].append((exchange_name, bbo_gap, None))
            message = self._build_message(ticker, alerts)
            await self._send_message(message)
            return

        # --- 2단계: impact price 검증 ---
        usdt_krw = result.bithumb.usdt_krw_last
        if usdt_krw is None or usdt_krw <= 0:
            return

        # 빗썸 ask 오더북은 후보가 있을 때 1회만 조회
        bithumb_asks = await fetch_bithumb_orderbook(ticker, depth=5)

        # 후보별 해외 bid 오더북 병렬 조회
        async def _check_candidate(
            exchange_name: str, market_type: str, bbo_gap: float,
        ) -> tuple[str, str, float, float | None] | None:
            is_krw = exchange_name in KRW_EXCHANGES
            foreign_bids = await fetch_orderbook(
                exchange_name, ticker,
                market_type='swap' if market_type == 'futures' else 'spot',
                depth=5,
            )
            impact = calculate_impact_gap(
                bithumb_asks=bithumb_asks,
                foreign_bids=foreign_bids,
                usdt_krw=usdt_krw,
                volume_usd=config.IMPACT_CHECK_VOLUME_USD,
                is_krw_exchange=is_krw,
            )
            if impact is None:
                return None
            return (exchange_name, market_type, bbo_gap, impact)

        tasks = [
            _check_candidate(ex, mt, gap) for ex, mt, gap in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # alerts: {market_type: [(exchange_name, bbo_gap, impact_gap), ...]}
        alerts: dict[str, list[tuple[str, float, float | None]]] = {
            'spot': [],
            'futures': [],
        }
        for res in results:
            if isinstance(res, Exception):
                logger.debug('Impact check error: %s', res)
                continue
            if res is None:
                continue
            exchange_name, market_type, bbo_gap, impact_gap = res
            entity_key = f'{ticker}_{exchange_name}_{market_type}'
            self._cooldown.record_alert(entity_key)
            alerts[market_type].append((exchange_name, bbo_gap, impact_gap))

        if not alerts['spot'] and not alerts['futures']:
            return

        message = self._build_message(ticker, alerts)
        await self._send_message(message)

    def _build_message(
        self,
        ticker: str,
        alerts: dict[str, list[tuple[str, float, float | None]]],
    ) -> str:
        """텔레그램 메시지를 조립한다.

        Format:
            🚨 빗썸 역프 알림

            [BTC]
            현물 갭
            Binance: 9,780 (impact: 9,740)

            현선 갭
            Binance: 9,700 (impact: 9,660)
        """
        lines = ['🚨 빗썸 역프 알림', '', f'[{ticker}]']

        if alerts['spot']:
            lines.append('현물 갭')
            for exchange_name, gap, impact in sorted(
                alerts['spot'],
                key=lambda x: x[2] if x[2] is not None else x[1],
            ):
                display = EXCHANGE_DISPLAY.get(exchange_name, exchange_name)
                if impact is None:
                    lines.append(f'{display}: {gap:,.0f}')
                else:
                    lines.append(f'{display}: {gap:,.0f} (impact: {impact:,.0f})')

        if alerts['futures']:
            if alerts['spot']:
                lines.append('')
            lines.append('현선 갭')
            for exchange_name, gap, impact in sorted(
                alerts['futures'],
                key=lambda x: x[2] if x[2] is not None else x[1],
            ):
                display = EXCHANGE_DISPLAY.get(exchange_name, exchange_name)
                if impact is None:
                    lines.append(f'{display}: {gap:,.0f}')
                else:
                    lines.append(f'{display}: {gap:,.0f} (impact: {impact:,.0f})')

        return '\n'.join(lines)

    async def _send_message(self, text: str, alert_key: str | None = None) -> None:
        """텔레그램 메시지를 전송한다.

        Args:
            text: 전송할 메시지.
            alert_key: 필터링용 키(옵션). disabled_keys 또는 disabled_prefixes 에
                매칭되면 silent drop. 없으면 기존대로 항상 발송 (backward-compat).
        """
        if self._is_filtered(alert_key):
            logger.debug('Telegram alert filtered: key=%s', alert_key)
            return
        if self._bot is None or not config.TELEGRAM_CHAT_ID:
            return
        try:
            await self._bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
            )
            logger.info('Telegram alert sent')
        except TelegramError as exc:
            logger.error('Failed to send Telegram alert: %s', exc)
        except Exception as exc:
            logger.error('Unexpected error sending Telegram alert: %s', exc)
