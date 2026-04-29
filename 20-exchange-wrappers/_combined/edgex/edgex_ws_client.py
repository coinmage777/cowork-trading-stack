"""
EdgeX WebSocket Client

Provides real-time data fetching for:
- Public WS: ticker (mark_price), depth (orderbook)
- Private WS: position, collateral, open_orders (auto-push after auth)

References:
- Public WS: /api/v1/public/ws
- Private WS: /api/v1/private/ws
"""
import asyncio
import logging
import time
from typing import Optional, Dict, Any, List, Set

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)


# EdgeX WS URLs
EDGEX_PUBLIC_WS_URL = "wss://quote.edgex.exchange/api/v1/public/ws"
EDGEX_PRIVATE_WS_URL = "wss://quote.edgex.exchange/api/v1/private/ws"


class EdgeXPublicWSClient(BaseWSClient):
    """
    EdgeX Public WebSocket 클라이언트.
    - ticker (mark price)
    - depth (orderbook)
    """

    WS_URL = EDGEX_PUBLIC_WS_URL
    PING_INTERVAL = None  # 서버가 ping 보내면 pong 응답
    RECV_TIMEOUT = 60.0
    RECONNECT_MIN = 1.0
    RECONNECT_MAX = 8.0

    def __init__(self):
        super().__init__()

        # Subscriptions: contractId -> set of channels
        self._ticker_subs: Set[str] = set()  # contractId
        self._depth_subs: Set[str] = set()  # contractId

        # Cached data
        self._tickers: Dict[str, Dict[str, Any]] = {}  # contractId -> ticker data
        self._orderbooks: Dict[str, Dict[str, Any]] = {}  # contractId -> orderbook

        # Events for waiting
        self._ticker_events: Dict[str, asyncio.Event] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = {}

    def _build_ping_message(self) -> Optional[str]:
        """EdgeX doesn't require client ping, server sends ping"""
        return None

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        msg_type = data.get("type")

        # Server ping -> respond with pong
        if msg_type == "ping":
            pong_msg = {"type": "pong", "time": data.get("time", str(int(time.time() * 1000)))}
            if self._ws:
                await self._ws.send(_json_dumps(pong_msg))
            return

        # Subscription confirmation
        if msg_type == "subscribed":
            return

        # Error
        if msg_type == "error":
            content = data.get("content", {})
            logger.warning(f"[EdgeXPublicWS] Error: {content}")
            return

        # Payload (data push)
        if msg_type == "payload" or msg_type == "quote-event":
            channel = data.get("channel", "")
            content = data.get("content", {})
            self._process_payload(channel, content)

    def _process_payload(self, channel: str, content: Dict[str, Any]) -> None:
        """Process payload based on channel type"""
        data_list = content.get("data", [])
        if not data_list:
            return

        # ticker.{contractId} or ticker.all
        if channel.startswith("ticker."):
            for item in data_list:
                contract_id = item.get("contractId")
                if contract_id:
                    self._tickers[contract_id] = item
                    if contract_id in self._ticker_events:
                        self._ticker_events[contract_id].set()

        # depth.{contractId}.{level}
        elif channel.startswith("depth."):
            for item in data_list:
                contract_id = item.get("contractId")
                if contract_id:
                    depth_type = item.get("depthType", "").upper()
                    if depth_type == "SNAPSHOT":
                        # Full orderbook
                        self._orderbooks[contract_id] = {
                            "bids": self._parse_levels(item.get("bids", [])),
                            "asks": self._parse_levels(item.get("asks", [])),
                            "time": int(time.time() * 1000),
                        }
                    elif depth_type == "CHANGED":
                        # Incremental update - create orderbook if not exists
                        if contract_id not in self._orderbooks:
                            self._orderbooks[contract_id] = {
                                "bids": [],
                                "asks": [],
                                "time": int(time.time() * 1000),
                            }
                        self._apply_depth_update(contract_id, item)

                    if contract_id in self._orderbook_events:
                        self._orderbook_events[contract_id].set()

    def _parse_levels(self, levels: List[Dict[str, str]]) -> List[List[float]]:
        """Parse [{price, size}, ...] to [[price, size], ...]"""
        result = []
        for level in levels:
            try:
                px = float(level.get("price", 0))
                sz = float(level.get("size", 0))
                result.append([px, sz])
            except (ValueError, TypeError):
                continue
        return result

    def _apply_depth_update(self, contract_id: str, item: Dict[str, Any]) -> None:
        """Apply incremental depth update"""
        ob = self._orderbooks.get(contract_id)
        if not ob:
            return

        for bid in item.get("bids", []):
            try:
                px = float(bid.get("price", 0))
                sz = float(bid.get("size", 0))
                self._update_level(ob["bids"], px, sz, is_bid=True)
            except (ValueError, TypeError):
                continue

        for ask in item.get("asks", []):
            try:
                px = float(ask.get("price", 0))
                sz = float(ask.get("size", 0))
                self._update_level(ob["asks"], px, sz, is_bid=False)
            except (ValueError, TypeError):
                continue

        ob["time"] = int(time.time() * 1000)

    def _update_level(self, levels: List[List[float]], px: float, sz: float, is_bid: bool) -> None:
        """Update or remove a price level"""
        # Remove if size is 0
        if sz == 0:
            levels[:] = [l for l in levels if l[0] != px]
            return

        # Update or insert
        for i, level in enumerate(levels):
            if level[0] == px:
                level[1] = sz
                return

        # Insert at correct position
        levels.append([px, sz])
        levels.sort(key=lambda x: x[0], reverse=is_bid)

    async def _resubscribe(self) -> None:
        """Resubscribe to all channels after reconnect"""
        # Clear stale data
        self._tickers.clear()
        self._orderbooks.clear()

        for ev in self._ticker_events.values():
            ev.clear()
        for ev in self._orderbook_events.values():
            ev.clear()

        # Resubscribe to tickers
        for contract_id in self._ticker_subs:
            sub_msg = {"type": "subscribe", "channel": f"ticker.{contract_id}"}
            await self._ws.send(_json_dumps(sub_msg))

        # Resubscribe to depth
        for contract_id in self._depth_subs:
            sub_msg = {"type": "subscribe", "channel": f"depth.{contract_id}.200"}
            await self._ws.send(_json_dumps(sub_msg))

    # ==================== Public API ====================

    async def subscribe_ticker(self, contract_id: str) -> None:
        """Subscribe to ticker for a contract"""
        if contract_id in self._ticker_subs:
            return

        self._ticker_subs.add(contract_id)
        self._ticker_events.setdefault(contract_id, asyncio.Event())

        if self._ws and self._running:
            sub_msg = {"type": "subscribe", "channel": f"ticker.{contract_id}"}
            await self._ws.send(_json_dumps(sub_msg))

    async def subscribe_orderbook(self, contract_id: str, depth: int = 200) -> None:
        """Subscribe to orderbook for a contract"""
        if contract_id in self._depth_subs:
            return

        self._depth_subs.add(contract_id)
        self._orderbook_events.setdefault(contract_id, asyncio.Event())

        if self._ws and self._running:
            sub_msg = {"type": "subscribe", "channel": f"depth.{contract_id}.{depth}"}
            await self._ws.send(_json_dumps(sub_msg))

    async def unsubscribe_orderbook(self, contract_id: str) -> None:
        """Unsubscribe from orderbook for a contract"""
        if contract_id not in self._depth_subs:
            return

        if self._ws and self._running:
            unsub_msg = {"type": "unsubscribe", "channel": f"depth.{contract_id}.200"}
            await self._ws.send(_json_dumps(unsub_msg))

        self._depth_subs.discard(contract_id)
        self._orderbooks.pop(contract_id, None)
        self._orderbook_events.pop(contract_id, None)

    def get_ticker(self, contract_id: str) -> Optional[Dict[str, Any]]:
        """Get cached ticker data"""
        return self._tickers.get(contract_id)

    def get_mark_price(self, contract_id: str) -> Optional[float]:
        """Get mark price from ticker"""
        ticker = self._tickers.get(contract_id)
        if ticker:
            try:
                return float(ticker.get("lastPrice", 0))
            except (ValueError, TypeError):
                return None
        return None

    def get_orderbook(self, contract_id: str, depth: int = 50) -> Optional[Dict[str, Any]]:
        """Get cached orderbook with limited depth"""
        ob = self._orderbooks.get(contract_id)
        if ob is None:
            return None
        return {
            "bids": ob["bids"][:depth],
            "asks": ob["asks"][:depth],
            "time": ob["time"],
        }

    async def wait_ticker_ready(self, contract_id: str, timeout: float = 5.0) -> bool:
        """Wait for ticker data to be received"""
        ev = self._ticker_events.get(contract_id)
        if not ev:
            return False
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orderbook_ready(self, contract_id: str, timeout: float = 5.0) -> bool:
        """Wait for orderbook snapshot to be received"""
        ev = self._orderbook_events.get(contract_id)
        if not ev:
            return False
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class EdgeXPrivateWSClient(BaseWSClient):
    """
    EdgeX Private WebSocket 클라이언트.
    - 인증 후 자동 푸시: position, collateral, open_orders
    """

    PING_INTERVAL = None
    RECV_TIMEOUT = 60.0
    RECONNECT_MIN = 1.0
    RECONNECT_MAX = 8.0

    def __init__(self, account_id: str, signature: str, timestamp: str):
        super().__init__()
        self.account_id = account_id
        self.signature = signature
        self.timestamp = timestamp

        # Set URL with accountId query param
        self.WS_URL = f"{EDGEX_PRIVATE_WS_URL}?accountId={account_id}"

        # Set auth headers
        self._extra_headers = {
            "X-edgeX-Api-Timestamp": timestamp,
            "X-edgeX-Api-Signature": signature,
        }

        # Cached data
        self._positions: Dict[str, Dict[str, Any]] = {}  # contractId -> position
        self._collateral: Optional[Dict[str, Any]] = None
        self._open_orders: Dict[str, Dict[str, Any]] = {}  # orderId -> order

        # Events
        self._position_event: asyncio.Event = asyncio.Event()
        self._collateral_event: asyncio.Event = asyncio.Event()
        self._orders_event: asyncio.Event = asyncio.Event()

        # Auth state
        self._snapshot_received: bool = False

    def _build_ping_message(self) -> Optional[str]:
        return None

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        msg_type = data.get("type")

        # Server ping -> respond with pong
        if msg_type == "ping":
            pong_msg = {"type": "pong", "time": data.get("time", str(int(time.time() * 1000)))}
            if self._ws:
                await self._ws.send(_json_dumps(pong_msg))
            return

        # Error
        if msg_type == "error":
            content = data.get("content", {})
            logger.warning(f"[EdgeXPrivateWS] Error: {content}")
            return

        # Trade event (auto-push)
        if msg_type == "trade-event":
            content = data.get("content", {})
            self._process_trade_event(content)

    def _process_trade_event(self, content: Dict[str, Any]) -> None:
        """Process trade-event payload"""
        event = content.get("event", "")
        data = content.get("data", {})

        # Snapshot: initial data after connect
        if event == "Snapshot":
            self._handle_snapshot(data)
            self._snapshot_received = True
            return

        # Account update (collateral)
        if event == "ACCOUNT_UPDATE":
            self._handle_account_update(data)

        # Order update
        if event == "ORDER_UPDATE":
            self._handle_order_update(data)

    def _handle_snapshot(self, data: Dict[str, Any]) -> None:
        """Handle initial snapshot"""
        # Positions
        positions = data.get("position", [])
        self._positions.clear()
        for pos in positions:
            contract_id = pos.get("contractId")
            if contract_id:
                self._positions[contract_id] = pos
        self._position_event.set()

        # Collateral
        collaterals = data.get("collateral", [])
        for col in collaterals:
            coin_id = col.get("coinId")
            if coin_id == "1000":  # USDT
                # legacyAmount = available balance, amount = margin balance (can be negative)
                available = float(col.get("legacyAmount") or col.get("amount") or 0)
                # For total, use legacyAmount as it represents actual equity
                total = float(col.get("legacyAmount") or col.get("amount") or 0)
                self._collateral = {
                    "available_collateral": round(available, 2),
                    "total_collateral": round(total, 2),
                    "_raw": col,
                }
                self._collateral_event.set()
                break

        # Orders (field is "id" not "orderId")
        orders = data.get("order", [])
        self._open_orders.clear()
        for order in orders:
            if order.get("status") == "OPEN":
                order_id = order.get("id")
                if order_id:
                    self._open_orders[order_id] = order
        self._orders_event.set()

    def _handle_account_update(self, data: Dict[str, Any]) -> None:
        """Handle account (collateral) update"""
        # Positions
        positions = data.get("position", [])
        for pos in positions:
            contract_id = pos.get("contractId")
            if contract_id:
                open_size = pos.get("openSize", "0")
                if open_size == "0":
                    self._positions.pop(contract_id, None)
                else:
                    self._positions[contract_id] = pos
        if positions:
            self._position_event.set()

        # Collateral
        collaterals = data.get("collateral", [])
        for col in collaterals:
            coin_id = col.get("coinId")
            if coin_id == "1000":
                # legacyAmount = available balance (same as Snapshot)
                available = float(col.get("legacyAmount") or col.get("amount") or 0)
                total = float(col.get("legacyAmount") or col.get("amount") or 0)
                self._collateral = {
                    "available_collateral": round(available, 2),
                    "total_collateral": round(total, 2),
                    "_raw": col,
                }
                self._collateral_event.set()
                break

    def _handle_order_update(self, data: Dict[str, Any]) -> None:
        """Handle order update (includes position updates)"""
        # Orders
        orders = data.get("order", [])
        for order in orders:
            order_id = order.get("id")  # field is "id" not "orderId"
            status = order.get("status")
            if order_id:
                if status == "OPEN":
                    self._open_orders[order_id] = order
                else:
                    # FILLED, CANCELLED, PENDING, etc - remove from open orders
                    self._open_orders.pop(order_id, None)
        if orders:
            self._orders_event.set()

        # Positions (can come with ORDER_UPDATE after fills)
        positions = data.get("position", [])
        for pos in positions:
            contract_id = pos.get("contractId")
            if contract_id:
                open_size = pos.get("openSize", "0")
                if open_size == "0" or open_size == "0.000":
                    self._positions.pop(contract_id, None)
                else:
                    self._positions[contract_id] = pos
        if positions:
            self._position_event.set()

    async def _resubscribe(self) -> None:
        """Reconnect requires re-auth (handled in connect)"""
        # Clear stale data
        self._positions.clear()
        self._collateral = None
        self._open_orders.clear()
        self._snapshot_received = False

        self._position_event.clear()
        self._collateral_event.clear()
        self._orders_event.clear()

    # ==================== Public API ====================

    def get_position(self, contract_id: str) -> Optional[Dict[str, Any]]:
        """Get cached position for contract"""
        return self._positions.get(contract_id)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached positions"""
        return dict(self._positions)

    def get_collateral(self) -> Optional[Dict[str, Any]]:
        """Get cached collateral"""
        return self._collateral

    def get_open_orders(self, contract_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cached open orders, optionally filtered by contract"""
        orders = list(self._open_orders.values())
        if contract_id:
            orders = [o for o in orders if o.get("contractId") == contract_id]
        return orders

    async def wait_snapshot_ready(self, timeout: float = 10.0) -> bool:
        """Wait for initial snapshot"""
        if self._snapshot_received:
            return True
        try:
            # Wait for all events
            await asyncio.wait_for(
                asyncio.gather(
                    self._position_event.wait(),
                    self._collateral_event.wait(),
                    self._orders_event.wait(),
                ),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False


# Pool for sharing connections (optional, simpler than Hyperliquid)
class EdgeXWSPool:
    """
    Simple pool for EdgeX WS clients.
    - One public client (shared)
    - One private client per account
    """

    def __init__(self):
        self._public: Optional[EdgeXPublicWSClient] = None
        self._private: Dict[str, EdgeXPrivateWSClient] = {}  # account_id -> client
        self._lock = asyncio.Lock()

    async def get_public(self) -> EdgeXPublicWSClient:
        """Get or create public WS client"""
        async with self._lock:
            if self._public is None:
                self._public = EdgeXPublicWSClient()
                await self._public.connect()
            return self._public

    async def get_private(self, account_id: str, signature: str, timestamp: str) -> EdgeXPrivateWSClient:
        """Get or create private WS client for account"""
        async with self._lock:
            if account_id not in self._private:
                client = EdgeXPrivateWSClient(account_id, signature, timestamp)
                await client.connect()
                self._private[account_id] = client
            return self._private[account_id]

    async def close_all(self) -> None:
        """Close all clients"""
        async with self._lock:
            if self._public:
                await self._public.close()
                self._public = None
            for client in self._private.values():
                await client.close()
            self._private.clear()


# Global pool instance
EDGEX_WS_POOL = EdgeXWSPool()
