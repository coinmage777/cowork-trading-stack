"""
GRVT WebSocket Client

Wraps pysdk's GrvtCcxtWS callback-based pattern into cache-based pattern
matching other exchange WS clients in this project.

Usage:
    client = GrvtWSClient(api_key, account_id, secret_key)
    await client.connect()

    # Subscribe to streams
    await client.subscribe_ticker("BTC_USDT_Perp")
    await client.subscribe_orderbook("BTC_USDT_Perp")
    await client.subscribe_position()

    # Get cached data
    price = client.get_mark_price("BTC_USDT_Perp")
    orderbook = client.get_orderbook("BTC_USDT_Perp")
    position = client.get_position("BTC_USDT_Perp")
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Any, Callable

from pysdk.grvt_ccxt_ws import GrvtCcxtWS
from pysdk.grvt_ccxt_env import GrvtEnv, GrvtWSEndpointType
from pysdk.grvt_ccxt_utils import rand_uint32

logger = logging.getLogger(__name__)

def create_grvt_ws_logger(name: str, filename: str, level=logging.ERROR) -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        fh = logging.FileHandler(f"logs/{filename}")
        fh.setLevel(level)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


class GrvtWSClient:
    """
    WebSocket client for GRVT exchange.

    Wraps pysdk's GrvtCcxtWS (callback-based) into cache-based pattern.
    Supports:
        - Mark price (via ticker stream)
        - Orderbook (via book stream)
        - Position (via position stream)
        - Open orders (via order/state stream)
    """

    def __init__(
        self,
        api_key: str,
        account_id: str,
        secret_key: str,
        env: str = "prod"
    ):
        self.api_key = api_key
        self.account_id = account_id
        self.secret_key = secret_key
        self.env = GrvtEnv(env)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[GrvtCcxtWS] = None
        self._connected = False
        self._logger = create_grvt_ws_logger("grvt_ws", "grvt_ws.log")

        # Caches
        self._prices: Dict[str, float] = {}  # symbol -> mark_price
        self._price_ts: Dict[str, float] = {}  # symbol -> timestamp
        self._orderbooks: Dict[str, Dict[str, Any]] = {}  # symbol -> orderbook
        self._orderbook_ts: Dict[str, float] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position
        self._position_ts: Dict[str, float] = {}
        self._collateral: Optional[Dict[str, Any]] = None
        self._collateral_ts: float = 0
        self._open_orders: Dict[str, list] = {}  # symbol -> orders
        self._open_orders_ts: Dict[str, float] = {}

        # Subscribed symbols
        self._ticker_subs: set = set()
        self._book_subs: set = set()
        self._position_subscribed = False
        self._order_subscribed = False

        # Events for data ready (first data received)
        self._position_event: asyncio.Event = asyncio.Event()
        self._orders_event: asyncio.Event = asyncio.Event()

    @property
    def connected(self) -> bool:
        if not self._ws:
            return False
        return self._ws.are_endpoints_connected([
            GrvtWSEndpointType.MARKET_DATA,
            GrvtWSEndpointType.TRADE_DATA,
            GrvtWSEndpointType.TRADE_DATA_RPC_FULL,  # Required for rpc_create_order
        ])

    async def connect(self) -> bool:
        """Initialize WS connection"""
        try:
            self._loop = asyncio.get_running_loop()
            self._ws = GrvtCcxtWS(
                self.env,
                self._loop,
                self._logger,
                parameters={
                    "api_key": self.api_key,
                    "trading_account_id": self.account_id,
                    "private_key": self.secret_key,
                }
            )
            await self._ws.initialize()

            # Wait for connection
            for _ in range(10):
                if self.connected:
                    self._connected = True
                    return True
                await asyncio.sleep(0.5)

            self._logger.warning("GRVT WS connection timeout")
            return False

        except Exception as e:
            self._logger.error(f"GRVT WS connect error: {e}")
            return False

    async def close(self):
        """Close WS connection"""
        if self._ws:
            try:
                await self._ws.__aexit__()
                # Clean up internal session to prevent __del__ error
                if hasattr(self._ws, '_session') and self._ws._session:
                    if not self._ws._session.closed:
                        await self._ws._session.close()
                    self._ws._session = None
            except Exception as e:
                self._logger.error(f"GRVT WS close error: {e}")
            self._ws = None
            self._connected = False

    # ========== Callbacks ==========

    async def _on_ticker(self, message: dict):
        """Callback for ticker stream (mini.s or ticker.s)"""
        try:
            feed = message.get("feed", {})
            instrument = feed.get("instrument") or message.get("selector", "").split("@")[0]
            mark_price = feed.get("mark_price")

            if instrument and mark_price:
                self._prices[instrument] = float(mark_price)
                self._price_ts[instrument] = time.time()
        except Exception as e:
            self._logger.error(f"_on_ticker error: {e}")

    async def _on_orderbook(self, message: dict):
        """Callback for orderbook stream (book.s)"""
        try:
            feed = message.get("feed", {})
            instrument = feed.get("instrument") or message.get("selector", "").split("@")[0]

            bids = []
            asks = []

            # Parse bids
            for bid in feed.get("bids", []):
                price = float(bid.get("price", 0))
                size = float(bid.get("size", 0))
                if price > 0:
                    bids.append([price, size])

            # Parse asks
            for ask in feed.get("asks", []):
                price = float(ask.get("price", 0))
                size = float(ask.get("size", 0))
                if price > 0:
                    asks.append([price, size])

            if instrument:
                self._orderbooks[instrument] = {"bids": bids, "asks": asks}
                self._orderbook_ts[instrument] = time.time()

        except Exception as e:
            self._logger.error(f"_on_orderbook error: {e}")

    async def _on_position(self, message: dict):
        """Callback for position stream"""
        try:
            feed = message.get("feed", {})
            instrument = feed.get("instrument")

            if not instrument:
                return

            size = feed.get("size", "0")
            entry_price = feed.get("entry_price", "0")
            unrealized_pnl = feed.get("unrealized_pnl", "0")

            # Determine side from size sign
            size_val = float(size.replace("-", "")) if size else 0
            if size_val == 0:
                # Position closed
                if instrument in self._positions:
                    del self._positions[instrument]
            else:
                side = "short" if "-" in size else "long"
                self._positions[instrument] = {
                    "symbol": instrument,
                    "side": side,
                    "size": str(size_val),
                    "entry_price": entry_price,
                    "unrealized_pnl": unrealized_pnl,
                    "liquidation_price": feed.get("est_liquidation_price",None),
                    "raw_data": feed,
                }
            self._position_ts[instrument] = time.time()

            # Mark position data as ready
            if not self._position_event.is_set():
                self._position_event.set()

        except Exception as e:
            self._logger.error(f"_on_position error: {e}")

    async def _on_order(self, message: dict):
        """Callback for order stream"""
        try:
            feed = message.get("feed", {})

            # Extract order info
            order_id = feed.get("order_id")
            legs = feed.get("legs", [])
            state = feed.get("state", {})
            status = state.get("status", "")

            if not legs:
                return

            instrument = legs[0].get("instrument")
            if not instrument:
                return

            # Initialize order list for symbol if needed
            if instrument not in self._open_orders:
                self._open_orders[instrument] = []

            if status in ("OPEN", "PENDING"):
                # Add/update order
                order_info = {
                    "id": order_id,
                    "symbol": instrument,
                    "size": legs[0].get("size"),
                    "price": legs[0].get("limit_price"),
                    "side": "buy" if legs[0].get("is_buying_asset") else "sell"
                }
                # Update or add
                existing = [o for o in self._open_orders[instrument] if o["id"] == order_id]
                if existing:
                    existing[0].update(order_info)
                else:
                    self._open_orders[instrument].append(order_info)
            else:
                # Remove closed/cancelled/filled order
                self._open_orders[instrument] = [
                    o for o in self._open_orders[instrument]
                    if o.get("id") != order_id
                ]

            self._open_orders_ts[instrument] = time.time()

            # Mark orders data as ready
            if not self._orders_event.is_set():
                self._orders_event.set()

        except Exception as e:
            self._logger.error(f"_on_order error: {e}")

    # ========== Subscribe methods ==========

    async def subscribe_ticker(self, symbol: str, rate: str = "500"):
        """Subscribe to ticker stream for mark price"""
        if not self._ws or symbol in self._ticker_subs:
            return

        await self._ws.subscribe(
            "mini.s",  # mini snapshot - includes mark_price
            self._on_ticker,
            params={"instrument": symbol, "rate": rate}
        )
        self._ticker_subs.add(symbol)

    async def subscribe_orderbook(self, symbol: str, rate: str = "500", depth: str = "10"):
        """Subscribe to orderbook stream"""
        if not self._ws or symbol in self._book_subs:
            return

        await self._ws.subscribe(
            "book.s",  # book snapshot
            self._on_orderbook,
            params={"instrument": symbol, "rate": rate, "depth": depth}
        )
        self._book_subs.add(symbol)

    async def subscribe_position(self):
        """Subscribe to position stream"""
        if not self._ws or self._position_subscribed:
            return

        await self._ws.subscribe(
            "position",
            self._on_position,
            params={}
        )
        self._position_subscribed = True
        # Set event immediately - cache starts as None (no position)
        # If position exists, snapshot will update cache
        if not self._position_event.is_set():
            self._position_event.set()

    async def subscribe_orders(self):
        """Subscribe to order stream"""
        if not self._ws or self._order_subscribed:
            return

        await self._ws.subscribe(
            "order",
            self._on_order,
            params={}
        )
        self._order_subscribed = True
        # Mark as ready immediately (empty = no orders until data arrives)
        if not self._orders_event.is_set():
            self._orders_event.set()

    # ========== Get methods (from cache) ==========

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get cached mark price"""
        return self._prices.get(symbol)

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached orderbook"""
        return self._orderbooks.get(symbol)

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached position"""
        return self._positions.get(symbol)

    def get_open_orders(self, symbol: str) -> Optional[list]:
        """Get cached open orders"""
        return self._open_orders.get(symbol)

    def get_collateral(self) -> Optional[Dict[str, Any]]:
        """Get cached collateral (not available via WS, always None)"""
        return self._collateral

    # ========== Cache age check ==========

    def is_price_fresh(self, symbol: str, max_age_sec: float = 5.0) -> bool:
        """Check if cached price is fresh"""
        ts = self._price_ts.get(symbol, 0)
        return (time.time() - ts) < max_age_sec

    def is_orderbook_fresh(self, symbol: str, max_age_sec: float = 5.0) -> bool:
        """Check if cached orderbook is fresh"""
        ts = self._orderbook_ts.get(symbol, 0)
        return (time.time() - ts) < max_age_sec

    def is_position_fresh(self, symbol: str, max_age_sec: float = 5.0) -> bool:
        """Check if cached position is fresh"""
        ts = self._position_ts.get(symbol, 0)
        return (time.time() - ts) < max_age_sec

    def is_orders_fresh(self, symbol: str, max_age_sec: float = 5.0) -> bool:
        """Check if cached orders are fresh"""
        ts = self._open_orders_ts.get(symbol, 0)
        return (time.time() - ts) < max_age_sec

    # ========== Data ready check ==========

    def is_position_ready(self) -> bool:
        """Check if position data has been received at least once"""
        return self._position_event.is_set()

    def is_orders_ready(self) -> bool:
        """Check if orders data has been received at least once"""
        return self._orders_event.is_set()

    async def wait_position_ready(self, timeout: float = 5.0) -> bool:
        """Wait until position data is available"""
        if self._position_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._position_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orders_ready(self, timeout: float = 5.0) -> bool:
        """Wait until orders data is available"""
        if self._orders_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._orders_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ========== Trading methods (via WS RPC) ==========

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        order_type: str = 'market'
    ) -> Optional[str]:
        """
        Create order via WS RPC.
        Returns client_order_id on success.
        """
        if not self._ws:
            return None

        try:
            # Ensure order stream is subscribed for updates
            if not self._order_subscribed:
                await self.subscribe_orders()

            client_order_id = str(rand_uint32())
            params = {"client_order_id": client_order_id}

            if price is not None:
                order_type = 'limit'

            # Use pysdk's rpc_create_order
            await self._ws.rpc_create_order(
                symbol=symbol,
                order_type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=params
            )

            return client_order_id

        except Exception as e:
            self._logger.error(f"WS create_order error: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel single order via WS RPC"""
        if not self._ws:
            return False

        try:
            await self._ws.rpc_cancel_order(id=order_id)
            return True
        except Exception as e:
            self._logger.error(f"WS cancel_order error: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """Cancel all orders via WS RPC"""
        if not self._ws:
            return False

        try:
            params = {}
            if symbol:
                # Extract base from symbol like "BTC_USDT_Perp"
                parts = symbol.split("_")
                if len(parts) >= 2:
                    params = {"kind": "PERPETUAL", "base": parts[0], "quote": parts[1]}

            await self._ws.rpc_cancel_all_orders(params=params)
            return True
        except Exception as e:
            self._logger.error(f"WS cancel_all_orders error: {e}")
            return False


# Singleton pool for sharing connections
GRVT_WS_POOL: Dict[str, GrvtWSClient] = {}


async def get_grvt_ws_client(
    api_key: str,
    account_id: str,
    secret_key: str,
    env: str = "prod"
) -> GrvtWSClient:
    """Get or create shared WS client"""
    pool_key = f"{account_id}_{env}"

    if pool_key not in GRVT_WS_POOL:
        client = GrvtWSClient(api_key, account_id, secret_key, env)
        await client.connect()
        GRVT_WS_POOL[pool_key] = client

    return GRVT_WS_POOL[pool_key]


async def release_grvt_ws_client(account_id: str, env: str = "prod", force_close: bool = False) -> None:
    """
    Release a WS client from pool.

    Args:
        account_id: Account ID
        env: Environment (prod/testnet)
        force_close: True면 연결 종료 및 풀에서 제거
    """
    if not force_close:
        return  # Keep connection alive for reuse

    pool_key = f"{account_id}_{env}"
    if pool_key in GRVT_WS_POOL:
        client = GRVT_WS_POOL.pop(pool_key)
        await client.close()
        logger.info(f"[GRVTWSPool] Force closed: {pool_key}")
