"""
StandX WebSocket Client

Provides real-time data fetching for:
- price (mark_price, index_price, last_price)
- depth_book (orderbook)
- position (user positions) - requires auth
- balance (user balance) - requires auth
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Dict, Any, Set, List, Callable

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)


STANDX_WS_URL = "wss://perps.standx.com/ws-stream/v1"


class StandXWSClient(BaseWSClient):
    """
    StandX WebSocket 클라이언트.
    BaseWSClient를 상속하여 연결/재연결 로직 공유.
    """

    WS_URL = STANDX_WS_URL
    # StandX ping 설정:
    # - None: ping 없음 (기본값, 원래 StandX는 ping 보내면 서버가 끊음)
    # - 숫자: ping 간격 (테스트용)
    PING_INTERVAL = None  # 테스트: 30.0으로 변경해서 테스트 가능
    RECV_TIMEOUT = None # 60초간 메시지 없으면 재연결
    RECONNECT_MIN = 0.2
    RECONNECT_MAX = 30.0

    def __init__(self, jwt_token: Optional[str] = None):
        super().__init__()
        self.jwt_token = jwt_token

        # Subscriptions
        self._price_subs: Set[str] = set()
        self._orderbook_subs: Set[str] = set()
        self._user_subs: Set[str] = set()  # position, balance, order, trade

        # Cached data
        self._prices: Dict[str, Dict[str, Any]] = {}
        self._orderbooks: Dict[str, Dict[str, Any]] = {}
        self._positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position
        self._collateral: Optional[Dict[str, Any]] = None
        self._orders: Dict[int, Dict[str, Any]] = {}  # order_id -> order

        # Events for waiting (data ready)
        self._price_events: Dict[str, asyncio.Event] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = {}
        self._position_event: asyncio.Event = asyncio.Event()
        self._collateral_event: asyncio.Event = asyncio.Event()
        self._orders_event: asyncio.Event = asyncio.Event()

        # Auth state
        self._authenticated: bool = False

        # Reconnect event (for _send to wait)
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._reconnect_event.set()  # Initially not reconnecting

        # Reconnect callback (called after _resubscribe completes)
        # Set by external code (e.g., standx.py) to reload cache via REST
        self.on_reconnect: Optional[Callable[[], Any]] = None

    # ==================== Abstract Method Implementations ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        channel = data.get("channel")
        symbol = data.get("symbol")
        payload = data.get("data", {})

        if channel == "auth":
            code = payload.get("code")
            if code == 0:  # 0 = success
                self._authenticated = True
            else:
                msg = f"[StandXWS] auth failed: {payload}"
                logger.debug(msg)
                logger.error(msg)
            return

        if channel == "price":
            self._prices[symbol] = payload
            if symbol in self._price_events:
                self._price_events[symbol].set()

        elif channel == "depth_book":
            self._orderbooks[symbol] = self._parse_orderbook(payload)
            if symbol in self._orderbook_events:
                self._orderbook_events[symbol].set()

        elif channel == "position":
            logger.debug(f"[StandXWS] position update: {payload}")
            pos_symbol = payload.get("symbol")
            if pos_symbol:
                self._positions[pos_symbol] = payload
            self._position_event.set()

        elif channel == "balance":
            free = payload.get("free", "0")
            total = payload.get("total", "0")
            locked = payload.get("locked", "0")

            self._collateral = {
                "cross_available": free,
                "balance": total,
                "equity": total,
                "upnl": "0",
                "cross_balance": total,
                "isolated_balance": "0",
                "locked": locked,
                "_raw": payload,
            }
            self._collateral_event.set()

        elif channel == "order":
            # Order updates
            order_id = payload.get("id")
            status = payload.get("status", "").lower()
            if order_id:
                if status in ("filled", "canceled", "rejected"):
                    # Remove completed/canceled orders
                    self._orders.pop(order_id, None)
                else:
                    # Add/update open orders
                    self._orders[order_id] = payload
                self._orders_event.set()
                logger.debug(f"[StandXWS] order update: id={order_id}, status={status}")

    async def _resubscribe(self) -> None:
        """Resubscribe to all channels after reconnect"""
        logger.debug(f"[StandXWSClient] Reconnected, resubscribing...")

        # 캐시된 데이터 초기화 (stale data 방지)
        self._prices.clear()
        self._orderbooks.clear()
        self._positions.clear()
        self._collateral = None
        self._orders.clear()
        self._authenticated = False

        # 이벤트 초기화 (on_reconnect 콜백에서 REST로 채운 후 set됨)
        self._position_event.clear()
        self._collateral_event.clear()
        self._orders_event.clear()
        for ev in self._price_events.values():
            ev.clear()
        for ev in self._orderbook_events.values():
            ev.clear()

        # WS 연결 확인
        if not self._ws:
            logger.debug(f"[StandXWSClient] _resubscribe failed: no websocket")
            return

        # Re-authenticate if we had a token
        if self.jwt_token:
            auth_msg: Dict[str, Any] = {
                "auth": {
                    "token": self.jwt_token,
                    "streams": [
                        {"channel": "balance"},
                    ]
                }
            }
            try:
                await self._ws.send(_json_dumps(auth_msg))
            except Exception as e:
                logger.debug(f"[StandXWSClient] Auth send failed: {e}")
                return

            # Wait for auth
            for _ in range(50):
                if self._authenticated:
                    break
                await asyncio.sleep(0.1)

            if not self._authenticated:
                logger.debug(f"[StandXWSClient] Auth failed after reconnect")
            else:
                logger.debug(f"[StandXWSClient] Re-authenticated")

        # Resubscribe to price channels
        for symbol in self._price_subs:
            try:
                await self._ws.send(_json_dumps({"subscribe": {"channel": "price", "symbol": symbol}}))
            except Exception as e:
                logger.debug(f"[StandXWSClient] Resubscribe price/{symbol} failed: {e}")

        # Resubscribe to orderbook channels
        for symbol in self._orderbook_subs:
            try:
                await self._ws.send(_json_dumps({"subscribe": {"channel": "depth_book", "symbol": symbol}}))
            except Exception as e:
                logger.debug(f"[StandXWSClient] Resubscribe orderbook/{symbol} failed: {e}")

        # Resubscribe to user channels (position, order)
        # Note: balance is already included in auth message
        user_channels_to_resub = [ch for ch in self._user_subs if ch not in ("balance",)]
        for channel in user_channels_to_resub:
            try:
                await self._ws.send(_json_dumps({"subscribe": {"channel": channel}}))
            except Exception as e:
                logger.debug(f"[StandXWSClient] Resubscribe {channel} failed: {e}")

        logger.debug(f"[StandXWSClient] Resubscribed: {len(self._price_subs)} price, {len(self._orderbook_subs)} orderbook, {len(user_channels_to_resub)} user")

        # Reconnect callback: REST API로 캐시 갱신 (stale data 방지)
        if self.on_reconnect:
            try:
                result = self.on_reconnect()
                if asyncio.iscoroutine(result):
                    await result
                logger.debug(f"[StandXWSClient] on_reconnect callback completed")
            except Exception as e:
                logger.debug(f"[StandXWSClient] on_reconnect callback failed: {e}")

    def _build_ping_message(self) -> Optional[str]:
        """
        StandX ping 메시지.
        - PING_INTERVAL = None: ping 안 보냄 (기본값)
        - PING_INTERVAL = 숫자: ping 보냄 (테스트용)

        주의: 원래 StandX는 ping 보내면 서버가 끊는다고 했는데,
        연결이 자꾸 끊기는 문제가 있어서 테스트 중.
        """
        if self.PING_INTERVAL is None:
            return None
        # StandX ping 형식 (추정) - 서버가 지원하는지 확인 필요
        return '{"ping": "pong"}'

    # ==================== Connection Management ====================

    async def connect(self) -> bool:
        """WS 연결 (base class 사용)"""
        result = await super().connect()
        if result:
            logger.debug(f"[StandXWSClient] Connected to {self.WS_URL}")
        return result

    async def close(self) -> None:
        """연결 종료 및 상태 초기화"""
        await super().close()
        self._authenticated = False
        self._price_subs.clear()
        self._orderbook_subs.clear()
        self._user_subs.clear()

    async def _handle_disconnect(self) -> None:
        """연결 끊김 처리 - reconnect event 관리 추가"""
        self._reconnect_event.clear()
        self._authenticated = False
        await super()._handle_disconnect()
        self._reconnect_event.set()

    # ==================== StandX-specific Methods ====================

    async def _send_msg(self, msg: Dict[str, Any]) -> None:
        """Send message to WebSocket (with reconnect wait)"""
        if self._reconnecting:
            try:
                await asyncio.wait_for(self._reconnect_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                raise RuntimeError("[standx_ws] reconnect timeout")

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

    # ----------------------------
    # Authentication
    # ----------------------------
    async def authenticate(self, jwt_token: str, streams: Optional[list] = None) -> bool:
        """
        Authenticate with JWT token

        Args:
            jwt_token: JWT access token
            streams: Optional list of channels to subscribe on auth
                     e.g., [{"channel": "order"}, {"channel": "position"}]
        """
        self.jwt_token = jwt_token
        auth_msg: Dict[str, Any] = {
            "auth": {
                "token": jwt_token,
            }
        }
        if streams:
            auth_msg["auth"]["streams"] = streams
            for stream in streams:
                self._user_subs.add(stream["channel"])

        await self._send_msg(auth_msg)

        # Wait for auth response
        for _ in range(50):  # 5 seconds
            if self._authenticated:
                return True
            await asyncio.sleep(0.1)
        return False

    # ----------------------------
    # Public Subscriptions
    # ----------------------------
    async def subscribe_price(self, symbol: str) -> None:
        """Subscribe to price channel for symbol"""
        if symbol in self._price_subs:
            return
        logger.debug(f"[StandXWS] Subscribe: price/{symbol}")
        await self._send_msg({"subscribe": {"channel": "price", "symbol": symbol}})
        self._price_subs.add(symbol)
        if symbol not in self._price_events:
            self._price_events[symbol] = asyncio.Event()

    async def unsubscribe_price(self, symbol: str) -> None:
        """Unsubscribe from price channel"""
        if symbol not in self._price_subs:
            return
        logger.debug(f"[StandXWS] Unsubscribe: price/{symbol}")
        await self._send_msg({"unsubscribe": {"channel": "price", "symbol": symbol}})
        self._price_subs.discard(symbol)

    async def subscribe_orderbook(self, symbol: str) -> None:
        """Subscribe to orderbook (depth_book) channel for symbol"""
        if symbol in self._orderbook_subs:
            return
        logger.debug(f"[StandXWS] Subscribe: orderbook/{symbol}")
        await self._send_msg({"subscribe": {"channel": "depth_book", "symbol": symbol}})
        self._orderbook_subs.add(symbol)
        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()

    async def unsubscribe_orderbook(self, symbol: str) -> None:
        """Unsubscribe from orderbook (depth_book) channel"""
        if symbol not in self._orderbook_subs:
            return
        logger.debug(f"[StandXWS] Unsubscribe: orderbook/{symbol}")
        await self._send_msg({"unsubscribe": {"channel": "depth_book", "symbol": symbol}})
        self._orderbook_subs.discard(symbol)

    # ----------------------------
    # User Subscriptions (requires auth)
    # ----------------------------
    async def subscribe_position(self) -> None:
        """Subscribe to position channel (requires auth)"""
        if "position" in self._user_subs:
            return
        logger.debug("[StandXWS] Subscribe: position")
        await self._send_msg({"subscribe": {"channel": "position"}})
        self._user_subs.add("position")

    async def subscribe_balance(self) -> None:
        """Subscribe to balance channel (requires auth)"""
        if "balance" in self._user_subs:
            return
        logger.debug("[StandXWS] Subscribe: balance")
        await self._send_msg({"subscribe": {"channel": "balance"}})
        self._user_subs.add("balance")

    async def subscribe_orders(self) -> None:
        """Subscribe to order channel (requires auth)"""
        if "order" in self._user_subs:
            return
        logger.debug("[StandXWS] Subscribe: order")
        await self._send_msg({"subscribe": {"channel": "order"}})
        self._user_subs.add("order")

    async def subscribe_user_channels(self) -> None:
        """Subscribe to all user channels"""
        await self.subscribe_position()
        await self.subscribe_balance()
        await self.subscribe_orders()

    # ----------------------------
    # Data Getters
    # ----------------------------
    def get_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached price data for symbol"""
        return self._prices.get(symbol)

    def get_mark_price(self, symbol: str) -> Optional[str]:
        """Get mark price for symbol"""
        price_data = self._prices.get(symbol)
        if price_data:
            return price_data.get("mark_price")
        return None

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached orderbook for symbol"""
        return self._orderbooks.get(symbol)

    @staticmethod
    def _parse_orderbook(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse StandX orderbook to standard format.

        StandX: {"asks": [["price", "size"], ...], "bids": [...]}
        Output: {"asks": [[price, size], ...], "bids": [...], "time": int}
        """
        def parse_levels(levels: list) -> list:
            result = []
            for item in levels:
                try:
                    px = float(item[0])
                    sz = float(item[1])
                    result.append([px, sz])
                except (IndexError, ValueError, TypeError):
                    continue
            return result

        asks = parse_levels(data.get("asks", []))
        bids = parse_levels(data.get("bids", []))

        # 표준 정렬: asks 오름차순, bids 내림차순
        asks.sort(key=lambda x: x[0])
        bids.sort(key=lambda x: x[0], reverse=True)

        return {
            "asks": asks,
            "bids": bids,
            "time": int(time.time() * 1000),
        }

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached position for symbol"""
        return self._positions.get(symbol)

    def get_collateral(self) -> Optional[Dict[str, Any]]:
        """Get cached collateral/balance"""
        return self._collateral

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cached open orders, optionally filtered by symbol"""
        orders = list(self._orders.values())
        if symbol:
            orders = [o for o in orders if o.get("symbol") == symbol]
        return orders

    # ----------------------------
    # Wait for data
    # ----------------------------
    async def wait_price_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """Wait until price data is available"""
        if symbol in self._prices:
            return True

        if symbol not in self._price_events:
            self._price_events[symbol] = asyncio.Event()

        try:
            await asyncio.wait_for(self._price_events[symbol].wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

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

    async def wait_position_ready(self, timeout: float = 5.0) -> bool:
        """Wait until position data is available"""
        if self._positions:
            return True
        try:
            await asyncio.wait_for(self._position_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_collateral_ready(self, timeout: float = 0.1) -> bool:
        """Wait until collateral/balance data is available"""
        if self._collateral:
            return True
        try:
            await asyncio.wait_for(self._collateral_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orders_ready(self, timeout: float = 3.0) -> bool:
        """Wait until orders data is available (or event is set)"""
        if self._orders_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._orders_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ----------------------------
    # Initial Cache Loading
    # ----------------------------
    def set_initial_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Set initial positions from REST API"""
        self._positions.clear()
        for pos in positions:
            symbol = pos.get("symbol")
            if symbol:
                self._positions[symbol] = pos
        self._position_event.set()
        logger.debug(f"[StandXWS] Initial positions loaded: {len(self._positions)}")

    def set_initial_orders(self, orders: List[Dict[str, Any]]) -> None:
        """Set initial orders from REST API"""
        self._orders.clear()
        for order in orders:
            order_id = order.get("id")
            if order_id:
                self._orders[order_id] = order
        self._orders_event.set()
        logger.debug(f"[StandXWS] Initial orders loaded: {len(self._orders)}")

    def set_initial_collateral(self, collateral: Dict[str, Any]) -> None:
        """Set initial collateral from REST API"""
        self._collateral = collateral
        self._collateral_event.set()
        logger.debug(f"[StandXWS] Initial collateral loaded")


# ----------------------------
# WebSocket Pool (Singleton)
# ----------------------------
class StandXWSPool:
    """
    Singleton pool for StandX WebSocket connections.
    Shares connections across multiple exchange instances.
    """

    def __init__(self):
        self._clients: Dict[str, StandXWSClient] = {}  # key: wallet_address
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        wallet_address: str,
        jwt_token: Optional[str] = None,
    ) -> StandXWSClient:
        """
        Get or create a WebSocket client for the given wallet.
        """
        key = wallet_address.lower()

        async with self._lock:
            if key in self._clients:
                client = self._clients[key]
                # Reconnect if needed
                if not client._running:
                    await client.connect()
                return client

            # Create new client
            client = StandXWSClient(jwt_token=jwt_token)
            await client.connect()

            # Authenticate if token provided
            if jwt_token:
                auth = await client.authenticate(jwt_token, streams=[
                    {"channel": "balance"},
                ])
                if not auth:
                    msg = f"[StandXWSPool] auth failed for wallet {wallet_address}"
                    logger.debug(msg)
                    logger.error(msg)

            self._clients[key] = client
            return client

    async def release(self, wallet_address: str, force_close: bool = False) -> None:
        """
        Release a client.

        Args:
            wallet_address: Wallet address key
            force_close: If True, actually close and remove from pool
        """
        if not force_close:
            return  # Keep connection alive for reuse

        key = wallet_address.lower()
        async with self._lock:
            if key in self._clients:
                client = self._clients.pop(key)
                await client.close()
                logger.debug(f"[StandXWSPool] Force closed: {key[:10]}...")

    async def close_all(self) -> None:
        """Close all connections"""
        async with self._lock:
            for client in self._clients.values():
                await client.close()
            self._clients.clear()


# Global singleton
WS_POOL = StandXWSPool()


# ============================================================
# Order Response Stream Client (ws-api/v1)
# For order creation and cancellation
# ============================================================

STANDX_ORDER_WS_URL = "wss://perps.standx.com/ws-api/v1"


class StandXOrderWSClient(BaseWSClient):
    """
    StandX Order Response Stream WebSocket 클라이언트.

    별도의 엔드포인트 (ws-api/v1)를 사용하여 주문 생성/취소를 처리.
    Market Stream과 별도로 운영됨.
    """

    WS_URL = STANDX_ORDER_WS_URL
    # ping 설정 (테스트: 30.0으로 변경)
    PING_INTERVAL = None  # 테스트: 30.0으로 변경해서 테스트 가능
    RECV_TIMEOUT = None
    RECONNECT_MIN = 0.2
    RECONNECT_MAX = 30.0

    def __init__(self, jwt_token: Optional[str] = None, auth_handler=None):
        super().__init__()
        self.jwt_token = jwt_token
        self._auth_handler = auth_handler  # StandXAuth instance for signing

        # Session ID (consistent throughout session)
        self._session_id = str(uuid.uuid4())

        # Pending requests (request_id -> Future)
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._pending_lock = asyncio.Lock()

        # Auth state
        self._authenticated: bool = False
        self._auth_event: asyncio.Event = asyncio.Event()

        # Reconnect event
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._reconnect_event.set()

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        # Response format: {"code": 0, "message": "success", "request_id": "xxx"}
        request_id = data.get("request_id")
        code = data.get("code")
        message = data.get("message", "")

        # Check if this is an auth response
        if request_id and request_id in self._pending_requests:
            async with self._pending_lock:
                future = self._pending_requests.pop(request_id, None)
                if future and not future.done():
                    if code == 0:
                        future.set_result(data)
                    else:
                        future.set_exception(RuntimeError(f"Order API error: code={code}, msg={message}"))

        # Auth success check (code 200 for auth)
        if code == 200 and "success" in message.lower():
            self._authenticated = True
            self._auth_event.set()
        elif code == 0:
            # General success - might be auth login
            if not self._authenticated and "success" in message.lower():
                self._authenticated = True
                self._auth_event.set()

    async def _resubscribe(self) -> None:
        """Re-authenticate after reconnect"""
        logger.debug(f"[StandXOrderWSClient] Reconnected, re-authenticating...")

        self._authenticated = False
        self._auth_event.clear()

        # Clear pending requests (they will timeout)
        async with self._pending_lock:
            pending_count = len(self._pending_requests)
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(RuntimeError("Connection lost during reconnect"))
            self._pending_requests.clear()
            if pending_count > 0:
                logger.debug(f"[StandXOrderWSClient] Cleared {pending_count} pending requests")

        # Generate new session ID
        self._session_id = str(uuid.uuid4())

        # Re-authenticate if we have a token
        if self.jwt_token:
            auth_ok = await self._do_auth()
            if auth_ok:
                logger.debug(f"[StandXOrderWSClient] Re-authenticated")
            else:
                logger.debug(f"[StandXOrderWSClient] Re-authentication failed")

    def _build_ping_message(self) -> Optional[str]:
        """StandX Order WS ping (테스트용)"""
        if self.PING_INTERVAL is None:
            return None
        return '{"ping": "pong"}'

    async def connect(self) -> bool:
        """Connect and authenticate"""
        result = await super().connect()
        if result:
            logger.debug(f"[StandXOrderWSClient] Connected to {self.WS_URL}")
            if self.jwt_token:
                auth_ok = await self._do_auth()
                if auth_ok:
                    logger.debug(f"[StandXOrderWSClient] Authenticated")
                else:
                    logger.debug(f"[StandXOrderWSClient] Authentication failed")
        return result

    async def _do_auth(self) -> bool:
        """Send auth:login message"""
        if not self._ws or not self._running:
            return False

        request_id = str(uuid.uuid4())
        auth_msg = {
            "session_id": self._session_id,
            "request_id": request_id,
            "method": "auth:login",
            "params": json.dumps({"token": self.jwt_token}),
        }

        try:
            await self._ws.send(_json_dumps(auth_msg))
            # Wait for auth response
            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=10.0)
                return self._authenticated
            except asyncio.TimeoutError:
                logger.debug("[StandXOrderWS] auth timeout")
                return False
        except Exception as e:
            logger.debug(f"[StandXOrderWS] auth error: {e}")
            return False

    async def close(self) -> None:
        """Close connection"""
        await super().close()
        self._authenticated = False
        self._auth_event.clear()
        async with self._pending_lock:
            self._pending_requests.clear()

    async def _handle_disconnect(self) -> None:
        """Handle disconnection"""
        self._reconnect_event.clear()
        self._authenticated = False
        await super()._handle_disconnect()
        self._reconnect_event.set()

    async def _send_request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """
        Send a request and wait for response.

        Args:
            method: "order:new" or "order:cancel"
            params: Request parameters (will be JSON stringified)
            timeout: Request timeout in seconds
        """
        if self._reconnecting:
            try:
                await asyncio.wait_for(self._reconnect_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                raise RuntimeError("[StandXOrderWS] reconnect timeout")

        if not self._ws or not self._running:
            await self.connect()

        if not self._authenticated:
            raise RuntimeError("[StandXOrderWS] not authenticated")

        request_id = str(uuid.uuid4())
        params_str = json.dumps(params, separators=(",", ":"))

        # Build headers (need body signature)
        if self._auth_handler is None:
            raise RuntimeError("[StandXOrderWS] auth_handler required for signing")

        sign_headers = self._auth_handler.sign_request(params_str)

        msg = {
            "session_id": self._session_id,
            "request_id": request_id,
            "method": method,
            "header": {
                "x-request-id": sign_headers["x-request-id"],
                "x-request-timestamp": sign_headers["x-request-timestamp"],
                "x-request-signature": sign_headers["x-request-signature"],
            },
            "params": params_str,
        }

        # Create future for response
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._pending_lock:
            self._pending_requests[request_id] = future

        try:
            await self._ws.send(_json_dumps(msg))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            async with self._pending_lock:
                self._pending_requests.pop(request_id, None)
            raise RuntimeError(f"[StandXOrderWS] request timeout: {method}")
        except Exception as e:
            async with self._pending_lock:
                self._pending_requests.pop(request_id, None)
            raise

    async def create_order(self, params: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """
        Create order via WebSocket.

        Args:
            params: Order parameters (same as REST API)
                - symbol: Trading pair (e.g., "BTC-USD")
                - side: "buy" or "sell"
                - order_type: "market" or "limit"
                - qty: Order quantity (string)
                - price: Order price (string, for limit orders)
                - reduce_only: bool
                - time_in_force: "gtc", "ioc", or "alo"
        """
        return await self._send_request("order:new", params, timeout=timeout)

    async def cancel_order(self, order_id: Optional[int] = None, cl_ord_id: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
        """
        Cancel order via WebSocket.

        Args:
            order_id: Server order ID
            cl_ord_id: Client order ID
        """
        params: Dict[str, Any] = {}
        if order_id:
            params["order_id"] = order_id
        if cl_ord_id:
            params["cl_ord_id"] = cl_ord_id

        if not params:
            raise ValueError("order_id or cl_ord_id required")

        return await self._send_request("order:cancel", params, timeout=timeout)


class StandXOrderWSPool:
    """
    Singleton pool for StandX Order WebSocket connections.
    """

    def __init__(self):
        self._clients: Dict[str, StandXOrderWSClient] = {}  # key: wallet_address
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        wallet_address: str,
        jwt_token: Optional[str] = None,
        auth_handler=None,
    ) -> StandXOrderWSClient:
        """Get or create an Order WebSocket client"""
        key = wallet_address.lower()

        async with self._lock:
            if key in self._clients:
                client = self._clients[key]
                # Update token if changed
                if jwt_token and client.jwt_token != jwt_token:
                    client.jwt_token = jwt_token
                    client._auth_handler = auth_handler
                    if client._running:
                        client._authenticated = False
                        client._auth_event.clear()
                        await client._do_auth()
                # Reconnect if needed
                if not client._running:
                    await client.connect()
                return client

            # Create new client
            client = StandXOrderWSClient(jwt_token=jwt_token, auth_handler=auth_handler)
            await client.connect()
            self._clients[key] = client
            return client

    async def release(self, wallet_address: str, force_close: bool = False) -> None:
        """
        Release a client.

        Args:
            wallet_address: Wallet address key
            force_close: If True, actually close and remove from pool
        """
        if not force_close:
            return  # Keep connection alive for reuse

        key = wallet_address.lower()
        async with self._lock:
            if key in self._clients:
                client = self._clients.pop(key)
                await client.close()
                logger.debug(f"[StandXOrderWSPool] Force closed: {key[:10]}...")

    async def close_all(self) -> None:
        """Close all connections"""
        async with self._lock:
            for client in self._clients.values():
                await client.close()
            self._clients.clear()


# Global singleton for Order WS
ORDER_WS_POOL = StandXOrderWSPool()
