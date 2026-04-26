"""
Candle Data Fetcher
거래소별 캔들(OHLCV) 데이터 조회

multi-perp-dex 래퍼에 캔들 메서드가 없으므로
거래소 API를 직접 호출하여 캔들 데이터를 가져옴
"""

import aiohttp
import time
from typing import Optional


class CandleFetcher:
    """거래소별 캔들 데이터 조회 베이스"""

    async def get_candles(self, symbol: str, interval: int = 1, limit: int = 1000) -> list[dict]:
        """
        캔들 데이터 조회

        Parameters:
            symbol: 코인 심볼 (e.g., "BTC", "ETH")
            interval: 캔들 타임프레임 (분)
            limit: 조회할 캔들 수

        Returns:
            list of dict with keys: open, high, low, close, volume, timestamp
        """
        raise NotImplementedError


class HyperliquidCandleFetcher(CandleFetcher):
    """Hyperliquid 캔들 데이터 조회"""

    BASE_URL = "https://api.hyperliquid.xyz/info"

    # 타임프레임 매핑 (분 → Hyperliquid API 포맷)
    INTERVAL_MAP = {
        1: "1m",
        3: "3m",
        5: "5m",
        15: "15m",
        30: "30m",
        60: "1h",
        240: "4h",
        1440: "1d",
    }

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    async def get_candles(self, symbol: str, interval: int = 1, limit: int = 1000) -> list[dict]:
        tf = self.INTERVAL_MAP.get(interval, "1m")

        # 시작 시간 계산 (밀리초)
        now_ms = int(time.time() * 1000)
        interval_ms = interval * 60 * 1000
        start_ms = now_ms - (limit * interval_ms)

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol.upper(),
                "interval": tf,
                "startTime": start_ms,
                "endTime": now_ms,
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.BASE_URL, json=payload, proxy=self.proxy) as resp:
                data = await resp.json()

        candles = []
        for c in data:
            candles.append({
                "timestamp": c.get("t", 0),
                "open": float(c.get("o", 0)),
                "high": float(c.get("h", 0)),
                "low": float(c.get("l", 0)),
                "close": float(c.get("c", 0)),
                "volume": float(c.get("v", 0)),
            })

        return candles


class BackpackCandleFetcher(CandleFetcher):
    """Backpack 캔들 데이터 조회"""

    BASE_URL = "https://api.backpack.exchange/api/v1/klines"

    INTERVAL_MAP = {
        1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m",
        60: "1h", 240: "4h", 1440: "1d",
    }

    async def get_candles(self, symbol: str, interval: int = 1, limit: int = 1000) -> list[dict]:
        tf = self.INTERVAL_MAP.get(interval, "1m")
        # Backpack uses "BTC_USDC_PERP" format
        bp_symbol = f"{symbol.upper()}_USDC_PERP"

        now_ms = int(time.time() * 1000)
        interval_ms = interval * 60 * 1000
        start_ms = now_ms - (limit * interval_ms)

        params = {
            "symbol": bp_symbol,
            "interval": tf,
            "startTime": start_ms,
            "endTime": now_ms,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(self.BASE_URL, params=params) as resp:
                data = await resp.json()

        candles = []
        for c in data:
            candles.append({
                "timestamp": c.get("startTime", 0),
                "open": float(c.get("open", 0)),
                "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)),
                "close": float(c.get("close", 0)),
                "volume": float(c.get("volume", 0)),
            })

        return candles


class GenericCandleFetcher(CandleFetcher):
    """
    일반 거래소용 - Hyperliquid API를 기본으로 사용
    대부분의 DEX가 Hyperliquid 기반이므로 이걸 fallback으로 사용
    """

    def __init__(self, proxy: Optional[str] = None):
        self._hl = HyperliquidCandleFetcher(proxy=proxy)

    async def get_candles(self, symbol: str, interval: int = 1, limit: int = 1000) -> list[dict]:
        return await self._hl.get_candles(symbol, interval, limit)


class DecibelCandleFetcher(CandleFetcher):
    """Decibel 캔들 데이터 조회 (Aptos 기반 DEX)"""

    BASE_URL = "https://api.mainnet.aptoslabs.com/decibel/api/v1/candlesticks"

    INTERVAL_MAP = {
        1: "1m", 15: "15m", 60: "1h", 240: "4h", 1440: "1d",
    }

    def __init__(self, proxy: Optional[str] = None, api_key: Optional[str] = None):
        self.proxy = proxy
        self.api_key = api_key

    async def get_candles(self, symbol: str, interval: int = 1, limit: int = 1000) -> list[dict]:
        tf = self.INTERVAL_MAP.get(interval, "1m")
        # Decibel은 market_address 필요 — 심볼로 직접 조회 불가하므로 Hyperliquid fallback
        # (캔들은 BTC/ETH 가격이라 거래소 무관하게 동일)
        return await HyperliquidCandleFetcher(proxy=self.proxy).get_candles(symbol, interval, limit)


# 거래소별 캔들 fetcher 매핑
CANDLE_FETCHER_MAP = {
    "hyperliquid": HyperliquidCandleFetcher,
    "backpack": BackpackCandleFetcher,
    "decibel": DecibelCandleFetcher,
    # 나머지 거래소는 GenericCandleFetcher 사용
}


def get_candle_fetcher(exchange_name: str, **kwargs) -> CandleFetcher:
    """거래소에 맞는 CandleFetcher 반환"""
    fetcher_class = CANDLE_FETCHER_MAP.get(exchange_name, GenericCandleFetcher)
    return fetcher_class(**kwargs)
