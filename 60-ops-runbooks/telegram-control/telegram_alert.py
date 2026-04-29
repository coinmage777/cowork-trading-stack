"""
Telegram Alert Module
=====================
거래 발생, 에러, 일일 요약을 텔레그램으로 알림.

사용:
  alerter = TelegramAlert(bot_token, chat_id)
  await alerter.send("메시지")

config.yaml:
  telegram:
    enabled: true
    bot_token: "${TG_BOT_TOKEN}"
    chat_id: "${TG_CHAT_ID}"
"""

import asyncio
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramAlert:

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def send(self, text: str, parse_mode: str = "HTML"):
        """텔레그램 메시지 전송"""
        if not self.bot_token or not self.chat_id:
            return
        try:
            s = await self._get_session()
            async with s.post(f"{self._base}/sendMessage", json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }) as r:
                if r.status != 200:
                    logger.debug(f"[tg] send failed: {r.status}")
        except Exception as e:
            logger.debug(f"[tg] send error: {e}")

    async def trade_entry(self, exchange: str, symbol: str, direction: str,
                          margin: float, group: str = ""):
        grp = f" [{group}]" if group else ""
        await self.send(
            f"📈 <b>ENTRY</b>{grp}\n"
            f"{exchange} | {symbol} {direction}\n"
            f"Margin: ${margin:.0f}"
        )

    async def trade_exit(self, exchange: str, symbol: str, pnl_pct: float,
                         reason: str, margin: float):
        icon = "✅" if pnl_pct > 0 else "❌"
        await self.send(
            f"{icon} <b>EXIT</b>\n"
            f"{exchange} | {symbol} {pnl_pct:+.2f}%\n"
            f"Reason: {reason} | Margin: ${margin:.0f}"
        )

    async def daily_summary(self, total_pnl: float, exchange_count: int,
                            trade_count: int, volume: float):
        icon = "🟢" if total_pnl >= 0 else "🔴"
        await self.send(
            f"{icon} <b>Daily Summary</b>\n"
            f"PnL: ${total_pnl:+.2f}\n"
            f"Exchanges: {exchange_count} | Trades: {trade_count}\n"
            f"Volume: ${volume:,.0f}"
        )

    async def alert(self, message: str, level: str = "info"):
        icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        await self.send(f"{icons.get(level, 'ℹ️')} {message}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
