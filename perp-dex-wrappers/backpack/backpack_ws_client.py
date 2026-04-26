"""
Backpack WebSocket Client

Provides real-time data:
- depth (orderbook) via incremental updates
- markPrice (mark price, index price, funding rate)
- account.positionUpdate (private, requires auth)
- account.orderUpdate (private, requires auth)
"""
import asyncio
import base64
import logging
import time
from typing import Optional, Dict, Any, Set, List

import aiohttp
import nacl.signing

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)


BACKPACK_WS_URL = "wss://ws.backpack.exchange"
BACKPACK_REST_URL = "https://api.backpack.exchange/api/v1"
ORDERBOOK_MAX_LEVELS = 50  # Limit orderbook depth (Backpack sends ~5000 levels)


class BackpackWSClient(BaseWSClient):
    """
    Backpack WebSocket 클라이언트.
    BaseWSClient를 상속하여 연결/재연결 로직 공유.
    """

    WS_URL = BACKPACK_WS_URL
    PING_INTERVAL = None  # Server sends Ping every 60s, client must respond with Pong (handled by websockets lib)
    RECV_TIMEOUT = 90.0  # 90초간 메시지 없으면 재연결 (server ping 60s + margin)
    RECONNECT_MIN = 0.5
    RECONNECT_MAX = 30.0

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None):
        super().__init__()

        # Auth credentials (for private streams)
        self._api_key = api_key
        self._secret_key = secret_key

        # Subscriptions
        self._orderbook_subs: Set[str] = set()
        self._price_subs: Set[str] = set()
        self._position_subscribed: bool = False
        self._order_subscribed: bool = False

        # Cached data
        self._orderbooks: Dict[str, Dict[str, Any]] = {}
        self._prices: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position
        self._open_orders: Dict[str, Dict[str, Any]] = {}  # order_id -> order

        # Track update IDs for delta validation
        self._orderbook_last_u: Dict[str, int] = {}

        # Events for waiting
        self._orderbook_events: Dict[str, asyncio.Event] = {}
        self._price_events: Dict[str, asyncio.Event] = {}
        self._position_event: asyncio.Event = asyncio.Event()
        self._order_event: asyncio.Event = asyncio.Event()

        # Reconnect event (for _send to wait)
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._reconnect_event.set()

        # HTTP session for REST calls
        self._http_session: Optional[aiohttp.ClientSession] = None

    # ==================== Abstract Method Implementations ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        stream = data.get("stream", "")
        payload = data.get("data", {})

        # depth.{symbol} stream
        if stream.startswith("depth."):
            symbol = payload.get("s")
            if symbol:
                await self._handle_depth_update(symbol, payload)

        # markPrice.{symbol} stream
        elif stream.startswith("markPrice."):
            symbol = payload.get("s")
            if symbol:
                self._handle_mark_price_update(symbol, payload)

        # account.positionUpdate stream
        elif stream.startswith("account.positionUpdate"):
            self._handle_position_update(payload)

        # account.orderUpdate stream
        elif stream.startswith("account.orderUpdate"):
            self._handle_order_update(payload)

    async def _handle_depth_update(self, symbol: str, data: Dict[str, Any]) -> None:
        """
        Handle depth update.
        Backpack sends incremental updates.
        Format: {"e": "depth", "s": "SOL_USDC", "a": [...], "b": [...], "U": firstId, "u": lastId}
        """
        first_update_id = data.get("U", 0)
        last_update_id = data.get("u", 0)

        if symbol not in self._orderbooks:
            # First update - need to fetch snapshot
            await self._fetch_orderbook_snapshot(symbol)
            if symbol not in self._orderbooks:
                return

        # Check if update is sequential
        last_u = self._orderbook_last_u.get(symbol, 0)
        if last_u > 0 and first_update_id != last_u + 1:
            # Gap detected - refetch snapshot
            logger.warning(f"[BackpackWS] orderbook gap detected for {symbol}: expected {last_u + 1}, got {first_update_id}")
            await self._fetch_orderbook_snapshot(symbol)
            if symbol not in self._orderbooks:
                return

        # Apply delta updates
        self._apply_depth_delta(symbol, data)
        self._orderbook_last_u[symbol] = last_update_id

        # Signal data ready
        if symbol in self._orderbook_events:
            self._orderbook_events[symbol].set()

    def _handle_mark_price_update(self, symbol: str, data: Dict[str, Any]) -> None:
        """
        Handle mark price update.
        Format: {"e": "markPrice", "s": "SOL_USDC", "p": "18.70", "f": "1.70", "i": "19.70", "n": 1694687965941, "T": ...}
        """
        self._prices[symbol] = {
            "mark_price": data.get("p"),
            "index_price": data.get("i"),
            "funding_rate": data.get("f"),
            "next_funding_time": data.get("n"),
            "time": int(time.time() * 1000),
        }

        # Signal data ready
        if symbol in self._price_events:
            self._price_events[symbol].set()

    def _handle_position_update(self, data: Dict[str, Any]) -> None:
        """
        Handle position update.
        Format: {"e": "positionOpened", "s": "SOL_USDC_PERP", "q": 5, "B": 122, "P": "0", ...}
        On subscription, initial positions are sent without "e" field.
        """
        symbol = data.get("s")
        if not symbol:
            return

        event_type = data.get("e")  # positionOpened, positionAdjusted, positionClosed

        if event_type == "positionClosed":
            # Remove closed position
            self._positions.pop(symbol, None)
        else:
            # Parse position
            net_qty = data.get("q", 0)
            try:
                net_qty = float(net_qty)
            except (ValueError, TypeError):
                net_qty = 0

            self._positions[symbol] = {
                "symbol": symbol,
                "side": "long" if net_qty > 0 else "short" if net_qty < 0 else None,
                "size": str(abs(net_qty)),
                "entry_price": data.get("B"),  # Entry price
                "unrealized_pnl": data.get("P"),  # PnL unrealized
                "raw_data": data,
            }

        self._position_event.set()

    def _handle_order_update(self, data: Dict[str, Any]) -> None:
        """
        Handle order update.
        Format: {"e": "orderAccepted", "s": "SOL_USD", "i": "order_id", "S": "Bid", "q": "100", "p": "20", ...}
        """
        event_type = data.get("e")
        order_id = data.get("i")
        if not order_id:
            return

        # Remove order on cancel/expire/fill
        if event_type in ("orderCancelled", "orderExpired"):
            self._open_orders.pop(order_id, None)
        elif event_type == "orderFill":
            # Check if fully filled
            executed_qty = data.get("z", "0")
            quantity = data.get("q", "0")
            try:
                if float(executed_qty) >= float(quantity):
                    self._open_orders.pop(order_id, None)
                else:
                    # Partial fill - update order
                    self._update_order(order_id, data)
            except (ValueError, TypeError):
                pass
        else:
            # orderAccepted, orderModified - add/update order
            self._update_order(order_id, data)

        self._order_event.set()

    def _update_order(self, order_id: str, data: Dict[str, Any]) -> None:
        """Update or create order in cache"""
        side_raw = data.get("S", "")
        side = "buy" if side_raw == "Bid" else "sell" if side_raw == "Ask" else side_raw

        self._open_orders[order_id] = {
            "id": order_id,
            "symbol": data.get("s"),
            "side": side,
            "size": data.get("q"),
            "price": data.get("p"),
            "order_type": data.get("o"),
            "status": data.get("X"),
            "executed_qty": data.get("z"),
            "time": int(time.time() * 1000),
        }

    def _generate_signature(self, instruction: str) -> str:
        """Generate ED25519 signature for private stream subscription"""
        if not self._secret_key:
            raise ValueError("Secret key required for private streams")

        private_key_bytes = base64.b64decode(self._secret_key)
        signing_key = nacl.signing.SigningKey(private_key_bytes)
        signature = signing_key.sign(instruction.encode())
        return base64.b64encode(signature.signature).decode()

    def _get_verifying_key(self) -> str:
        """Get base64 encoded verifying (public) key from secret key"""
        if not self._secret_key:
            raise ValueError("Secret key required for private streams")

        private_key_bytes = base64.b64decode(self._secret_key)
        signing_key = nacl.signing.SigningKey(private_key_bytes)
        verify_key = signing_key.verify_key
        return base64.b64encode(bytes(verify_key)).decode()

    def _apply_depth_delta(self, symbol: str, data: Dict[str, Any]) -> None:
        """Apply incremental depth update to orderbook"""
        if symbol not in self._orderbooks:
            return

        orderbook = self._orderbooks[symbol]

        # Process asks
        for item in data.get("a", []):
            try:
                price = float(item[0])
                size = float(item[1])
                if size == 0:
                    # Remove level
                    orderbook["asks"] = [lvl for lvl in orderbook["asks"] if lvl[0] != price]
                else:
                    # Update or insert
                    updated = False
                    for i, lvl in enumerate(orderbook["asks"]):
                        if lvl[0] == price:
                            orderbook["asks"][i] = [price, size]
                            updated = True
                            break
                    if not updated:
                        orderbook["asks"].append([price, size])
            except (IndexError, ValueError, TypeError):
                continue

        # Process bids
        for item in data.get("b", []):
            try:
                price = float(item[0])
                size = float(item[1])
                if size == 0:
                    # Remove level
                    orderbook["bids"] = [lvl for lvl in orderbook["bids"] if lvl[0] != price]
                else:
                    # Update or insert
                    updated = False
                    for i, lvl in enumerate(orderbook["bids"]):
                        if lvl[0] == price:
                            orderbook["bids"][i] = [price, size]
                            updated = True
                            break
                    if not updated:
                        orderbook["bids"].append([price, size])
            except (IndexError, ValueError, TypeError):
                continue

        # Re-sort: asks ascending, bids descending
        orderbook["asks"].sort(key=lambda x: x[0])
        orderbook["bids"].sort(key=lambda x: x[0], reverse=True)

        # Limit to max levels
        orderbook["asks"] = orderbook["asks"][:ORDERBOOK_MAX_LEVELS]
        orderbook["bids"] = orderbook["bids"][:ORDERBOOK_MAX_LEVELS]
        orderbook["time"] = int(time.time() * 1000)

    async def _fetch_orderbook_snapshot(self, symbol: str) -> None:
        """Fetch orderbook snapshot from REST API"""
        try:
            if not self._http_session or self._http_session.closed:
                self._http_session = aiohttp.ClientSession()

            url = f"{BACKPACK_REST_URL}/depth"
            params = {"symbol": symbol}

            async with self._http_session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"[BackpackWS] failed to fetch orderbook snapshot: {resp.status}")
                    return

                data = await resp.json()

            # Parse snapshot
            # Format: {"lastUpdateId": "...", "asks": [["price", "size"], ...], "bids": [...]}
            asks = []
            for item in data.get("asks", []):
                try:
                    asks.append([float(item[0]), float(item[1])])
                except (IndexError, ValueError, TypeError):
                    continue

            bids = []
            for item in data.get("bids", []):
                try:
                    bids.append([float(item[0]), float(item[1])])
                except (IndexError, ValueError, TypeError):
                    continue

            # Sort: asks ascending, bids descending
            asks.sort(key=lambda x: x[0])
            bids.sort(key=lambda x: x[0], reverse=True)

            # Limit to max levels
            asks = asks[:ORDERBOOK_MAX_LEVELS]
            bids = bids[:ORDERBOOK_MAX_LEVELS]

            self._orderbooks[symbol] = {
                "asks": asks,
                "bids": bids,
                "time": int(time.time() * 1000),
            }

            # Update last update ID
            last_update_id = data.get("lastUpdateId")
            if last_update_id:
                try:
                    self._orderbook_last_u[symbol] = int(last_update_id)
                except (ValueError, TypeError):
                    self._orderbook_last_u[symbol] = 0

        except Exception as e:
            logger.error(f"[BackpackWS] failed to fetch orderbook snapshot: {e}")

    async def _resubscribe(self) -> None:
        """Resubscribe to all channels after reconnect"""
        # Clear cached data (stale data 방지)
        self._orderbooks.clear()
        self._orderbook_last_u.clear()
        self._prices.clear()
        self._positions.clear()
        self._open_orders.clear()

        # Clear events
        for ev in self._orderbook_events.values():
            ev.clear()
        for ev in self._price_events.values():
            ev.clear()
        self._position_event.clear()
        self._order_event.clear()

        # Resubscribe to orderbook channels
        for symbol in self._orderbook_subs:
            stream = f"depth.{symbol}"
            await self._ws.send(_json_dumps({"method": "SUBSCRIBE", "params": [stream]}))
            # Fetch snapshot after resubscribe
            await self._fetch_orderbook_snapshot(symbol)

        # Resubscribe to mark price channels
        for symbol in self._price_subs:
            stream = f"markPrice.{symbol}"
            await self._ws.send(_json_dumps({"method": "SUBSCRIBE", "params": [stream]}))

        # Resubscribe to private streams (if authenticated)
        if self._position_subscribed and self._secret_key:
            await self._subscribe_private_stream("account.positionUpdate")

        if self._order_subscribed and self._secret_key:
            await self._subscribe_private_stream("account.orderUpdate")

    def _build_ping_message(self) -> Optional[str]:
        """Backpack server sends ping, client responds with pong (handled by websockets lib)"""
        return None

    # ==================== Connection Management ====================

    async def connect(self) -> bool:
        """WS 연결 (base class 사용)"""
        return await super().connect()

    async def close(self) -> None:
        """연결 종료 및 상태 초기화"""
        await super().close()
        self._orderbook_subs.clear()
        self._price_subs.clear()
        self._position_subscribed = False
        self._order_subscribed = False

        # Close HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def _handle_disconnect(self) -> None:
        """연결 끊김 처리 - reconnect event 관리 추가"""
        self._reconnect_event.clear()
        await super()._handle_disconnect()
        self._reconnect_event.set()

    # ==================== Subscription Methods ====================

    async def _send_msg(self, msg: Dict[str, Any]) -> None:
        """Send message to WebSocket (with reconnect wait)"""
        if self._reconnecting:
            try:
                await asyncio.wait_for(self._reconnect_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                raise RuntimeError("[backpack_ws] reconnect timeout")

        if not self._ws or not self._running:
            await self.connect()
        if self._ws:
            try:
                await self._ws.send(_json_dumps(msg))
            except Exception:
                if self._reconnecting:
                    await asyncio.wait_for(self._reconnect_event.wait(), timeout=60.0)
                    if self._ws:
                        await self._ws.send(_json_dumps(msg))

    async def subscribe_orderbook(self, symbol: str) -> None:
        """Subscribe to orderbook (depth) channel for symbol"""
        if symbol in self._orderbook_subs:
            return

        logger.debug(f"[BackpackWS] Subscribe: orderbook/{symbol}")
        stream = f"depth.{symbol}"
        await self._send_msg({"method": "SUBSCRIBE", "params": [stream]})
        self._orderbook_subs.add(symbol)

        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()

        # Fetch initial snapshot
        await self._fetch_orderbook_snapshot(symbol)

    async def unsubscribe_orderbook(self, symbol: str) -> None:
        """Unsubscribe from orderbook (depth) channel"""
        if symbol not in self._orderbook_subs:
            return

        logger.debug(f"[BackpackWS] Unsubscribe: orderbook/{symbol}")
        stream = f"depth.{symbol}"
        await self._send_msg({"method": "UNSUBSCRIBE", "params": [stream]})
        self._orderbook_subs.discard(symbol)

        # Clean up cached data
        self._orderbooks.pop(symbol, None)
        self._orderbook_last_u.pop(symbol, None)

    async def subscribe_mark_price(self, symbol: str) -> None:
        """Subscribe to mark price channel for symbol"""
        if symbol in self._price_subs:
            return

        logger.debug(f"[BackpackWS] Subscribe: markPrice/{symbol}")
        stream = f"markPrice.{symbol}"
        await self._send_msg({"method": "SUBSCRIBE", "params": [stream]})
        self._price_subs.add(symbol)

        if symbol not in self._price_events:
            self._price_events[symbol] = asyncio.Event()

    async def unsubscribe_mark_price(self, symbol: str) -> None:
        """Unsubscribe from mark price channel"""
        if symbol not in self._price_subs:
            return

        logger.debug(f"[BackpackWS] Unsubscribe: markPrice/{symbol}")
        stream = f"markPrice.{symbol}"
        await self._send_msg({"method": "UNSUBSCRIBE", "params": [stream]})
        self._price_subs.discard(symbol)

        # Clean up cached data
        self._prices.pop(symbol, None)

    # ==================== Private Stream Subscriptions ====================

    async def _subscribe_private_stream(self, stream: str) -> None:
        """Subscribe to a private stream with authentication"""
        if not self._secret_key:
            raise ValueError("Secret key required for private streams")

        logger.debug(f"[BackpackWS] Subscribe (private): {stream}")
        timestamp = str(int(time.time() * 1000))
        window = "5000"

        # Generate signature
        instruction = f"instruction=subscribe&timestamp={timestamp}&window={window}"
        signature = self._generate_signature(instruction)
        verifying_key = self._get_verifying_key()

        msg = {
            "method": "SUBSCRIBE",
            "params": [stream],
            "signature": [verifying_key, signature, timestamp, window]
        }
        await self._send_msg(msg)

    async def subscribe_position(self) -> None:
        """Subscribe to position updates (requires auth)"""
        if self._position_subscribed:
            return

        if not self._secret_key:
            raise ValueError("Secret key required for position subscription")

        await self._subscribe_private_stream("account.positionUpdate")
        self._position_subscribed = True

    async def unsubscribe_position(self) -> None:
        """Unsubscribe from position updates"""
        if not self._position_subscribed:
            return

        logger.debug("[BackpackWS] Unsubscribe: position")
        await self._send_msg({"method": "UNSUBSCRIBE", "params": ["account.positionUpdate"]})
        self._position_subscribed = False
        self._positions.clear()

    async def subscribe_orders(self) -> None:
        """Subscribe to order updates (requires auth)"""
        if self._order_subscribed:
            return

        if not self._secret_key:
            raise ValueError("Secret key required for order subscription")

        await self._subscribe_private_stream("account.orderUpdate")
        self._order_subscribed = True

    async def unsubscribe_orders(self) -> None:
        """Unsubscribe from order updates"""
        if not self._order_subscribed:
            return

        logger.debug("[BackpackWS] Unsubscribe: orders")
        await self._send_msg({"method": "UNSUBSCRIBE", "params": ["account.orderUpdate"]})
        self._order_subscribed = False
        self._open_orders.clear()

    # ==================== Data Getters ====================

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached orderbook for symbol"""
        return self._orderbooks.get(symbol)

    def get_mark_price(self, symbol: str) -> Optional[str]:
        """Get cached mark price for symbol"""
        price_data = self._prices.get(symbol)
        if price_data:
            return price_data.get("mark_price")
        return None

    def get_price_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get full cached price data for symbol (mark_price, index_price, funding_rate, etc)"""
        return self._prices.get(symbol)

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached position for symbol"""
        return self._positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached positions"""
        return self._positions.copy()

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cached open orders, optionally filtered by symbol"""
        if symbol is None:
            return list(self._open_orders.values())
        return [o for o in self._open_orders.values() if o.get("symbol") == symbol]

    def get_all_open_orders(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached open orders by order_id"""
        return self._open_orders.copy()

    # ==================== Wait for data ====================

    async def wait_orderbook_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """Wait until orderbook data is available"""
        if symbol in self._orderbooks:
            return True

        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()

        try:
            await asyncio.wait_for(self._orderbook_events[symbol].wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_price_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """Wait until mark price data is available"""
        if symbol in self._prices:
            return True

        if symbol not in self._price_events:
            self._price_events[symbol] = asyncio.Event()

        try:
            await asyncio.wait_for(self._price_events[symbol].wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_position_ready(self, timeout: float = 5.0) -> bool:
        """Wait until position data is available"""
        if self._positions:
            return True

        try:
            await asyncio.wait_for(self._position_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orders_ready(self, timeout: float = 5.0) -> bool:
        """Wait until order data is available"""
        if self._open_orders:
            return True

        try:
            await asyncio.wait_for(self._order_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ----------------------------
# WebSocket Pool (Singleton)
# ----------------------------
class BackpackWSPool:
    """
    Singleton pool for Backpack WebSocket connections.
    Shares connections across multiple exchange instances.
    Supports both public (unauthenticated) and private (authenticated) clients.
    """

    def __init__(self):
        self._public_client: Optional[BackpackWSClient] = None
        self._private_clients: Dict[str, BackpackWSClient] = {}  # api_key -> client
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> BackpackWSClient:
        """
        Get or create a WebSocket client.
        - Without credentials: returns shared public client
        - With credentials: returns client for that API key
        """
        async with self._lock:
            if api_key and secret_key:
                # Authenticated client
                if api_key in self._private_clients:
                    client = self._private_clients[api_key]
                    if not client._running:
                        await client.connect()
                    return client

                # Create new authenticated client
                client = BackpackWSClient(api_key=api_key, secret_key=secret_key)
                await client.connect()
                self._private_clients[api_key] = client
                return client
            else:
                # Public client (shared)
                if self._public_client is not None:
                    if not self._public_client._running:
                        await self._public_client.connect()
                    return self._public_client

                # Create new public client
                self._public_client = BackpackWSClient()
                await self._public_client.connect()
                return self._public_client

    async def release(self, api_key: str = None, force_close: bool = False) -> None:
        """
        Release client.

        Args:
            api_key: API key (None = public client)
            force_close: True면 연결 종료 및 풀에서 제거
        """
        if not force_close:
            return  # Keep connection alive for reuse

        async with self._lock:
            if api_key:
                if api_key in self._private_clients:
                    client = self._private_clients.pop(api_key)
                    await client.close()
                    logger.info(f"[BackpackWSPool] Force closed private: {api_key[:8]}...")
            else:
                if self._public_client:
                    await self._public_client.close()
                    self._public_client = None
                    logger.info("[BackpackWSPool] Force closed public client")

    async def close_all(self) -> None:
        """Close all connections"""
        async with self._lock:
            if self._public_client:
                await self._public_client.close()
                self._public_client = None

            for client in self._private_clients.values():
                await client.close()
            self._private_clients.clear()


# Global singleton
WS_POOL = BackpackWSPool()
