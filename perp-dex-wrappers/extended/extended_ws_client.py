"""
Extended WebSocket Client

Handles real-time data streaming from Extended exchange:
- Account stream (private): positions, orders, balance
- Mark price stream (public): mark prices

Authentication: X-Api-Key header
"""

import asyncio
import json
import logging
from typing import Optional, Dict, Any, List

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)


class ExtendedWSClient(BaseWSClient):
    """
    Extended WebSocket Client

    Account stream URL: /stream.extended.exchange/v1/account
    - Requires X-Api-Key header
    - Initial snapshot includes: balance, positions, orders
    - Updates: POSITION, ORDER, BALANCE, TRADE messages
    """

    # Server sends pings every 15 seconds, expects pong within 10 seconds
    PING_INTERVAL = None  # We don't send pings, server does
    RECV_TIMEOUT = 30.0   # Reconnect if no message for 30s
    RECONNECT_MIN = 1.0
    RECONNECT_MAX = 8.0

    def __init__(
        self,
        api_key: str,
        ws_url: str = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
    ):
        super().__init__()

        self._api_key = api_key
        self.WS_URL = f"{ws_url}/account"

        # Set auth header
        self._extra_headers = {
            "X-Api-Key": self._api_key,
        }

        # Cache
        self._positions: Dict[str, Dict[str, Any]] = {}  # market -> position
        self._orders: Dict[int, Dict[str, Any]] = {}      # order_id -> order
        self._balance: Optional[Dict[str, Any]] = None
        self._mark_prices: Dict[str, float] = {}

        # Events for waiting on data
        self._position_event = asyncio.Event()
        self._orders_event = asyncio.Event()
        self._balance_event = asyncio.Event()
        self._ready_event = asyncio.Event()  # All initial data received

        # Sequence tracking
        self._last_seq = 0

    # ==================== Abstract Method Implementations ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message"""
        msg_type = data.get("type")
        seq = data.get("seq", 0)

        # Check sequence (reconnect if out of order)
        if seq > 0 and self._last_seq > 0 and seq != self._last_seq + 1:
            logger.warning(f"[ExtendedWS] Sequence gap: expected {self._last_seq + 1}, got {seq}")
        if seq > 0:
            self._last_seq = seq

        if msg_type == "POSITION":
            self._handle_positions(data.get("data", {}).get("positions", []))
        elif msg_type == "ORDER":
            self._handle_orders(data.get("data", {}).get("orders", []))
        elif msg_type == "BALANCE":
            self._handle_balance(data.get("data", {}).get("balance", {}))
        elif msg_type == "TRADE":
            self._handle_trade(data.get("data", {}).get("trades", []))
        elif msg_type == "MP":  # Mark Price (from public stream)
            self._handle_mark_price(data.get("data", {}))
        else:
            logger.debug(f"[ExtendedWS] Unknown message type: {msg_type}")

        # Check if initial data is ready
        if self._position_event.is_set() and self._balance_event.is_set():
            if not self._ready_event.is_set():
                self._ready_event.set()
                print("[ExtendedWS] Initial snapshot received")

    async def _resubscribe(self) -> None:
        """
        Resubscribe after reconnect.
        For Extended, account stream auto-sends initial snapshot on connect.
        We just need to reset events and wait for new snapshot.
        """
        print("[ExtendedWS] Reconnected, waiting for new snapshot...")

        # Clear cache and events
        self._positions.clear()
        self._orders.clear()
        self._balance = None
        self._last_seq = 0

        self._position_event.clear()
        self._orders_event.clear()
        self._balance_event.clear()
        self._ready_event.clear()

    def _build_ping_message(self) -> Optional[str]:
        """
        Extended server sends pings, we respond with pong automatically.
        We don't need to send pings from client side.
        """
        return None

    # ==================== Message Handlers ====================

    def _handle_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Update position cache"""
        for pos in positions:
            market = pos.get("market")
            if not market:
                continue

            status = pos.get("status", "").upper()
            size = float(pos.get("size", 0))

            # Position closed if status is CLOSED or size is 0
            if status == "CLOSED" or size == 0:
                removed = self._positions.pop(market, None)
                if removed:
                    logger.debug(f"[ExtendedWS] Position closed: {market}")
            else:
                side_raw = pos.get("side", "LONG")
                self._positions[market] = {
                    "symbol": market,
                    "side": "long" if side_raw.upper() == "LONG" else "short",
                    "size": size,
                    "entry_price": float(pos.get("openPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealisedPnl", 0)),
                    "liquidation_price": float(pos.get("liquidationPrice") or 0) or None,
                    "raw_data": pos,
                }

        if not self._position_event.is_set():
            self._position_event.set()

    def _handle_orders(self, orders: List[Dict[str, Any]]) -> None:
        """Update orders cache"""
        for order in orders:
            order_id = order.get("id")
            if not order_id:
                continue

            status = order.get("status", "").upper()

            if status in ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                # Remove completed orders
                self._orders.pop(order_id, None)
            else:
                # Add/update open orders
                self._orders[order_id] = {
                    "id": order_id,
                    "symbol": order.get("market"),
                    "side": order.get("side", "BUY").lower(),
                    "size": float(order.get("qty", 0)),
                    "filled_size": float(order.get("filledQty", 0)),
                    "price": float(order.get("price", 0)) if order.get("price") else None,
                    "type": order.get("type", "LIMIT").lower(),
                    "status": status.lower(),
                    "created_time": order.get("createdTime"),
                }

        if not self._orders_event.is_set():
            self._orders_event.set()
        logger.debug(f"[ExtendedWS] Orders updated: {len(self._orders)}")

    def _handle_balance(self, balance: Dict[str, Any]) -> None:
        """Update balance cache"""
        self._balance = {
            "available_collateral": float(balance.get("availableForTrade", 0)),
            "total_collateral": float(balance.get("equity", 0)),
            "unrealized_pnl": float(balance.get("unrealisedPnl", 0)),
            "initial_margin": float(balance.get("initialMargin", 0)),
            "margin_ratio": float(balance.get("marginRatio", 0)),
            "balance": float(balance.get("balance", 0)),
        }

        if not self._balance_event.is_set():
            self._balance_event.set()
        logger.debug(f"[ExtendedWS] Balance updated")

    def _handle_trade(self, trades: List[Dict[str, Any]]) -> None:
        """Handle trade updates (for logging/events)"""
        for trade in trades:
            logger.debug(f"[ExtendedWS] Trade: {trade.get('market')} {trade.get('side')} {trade.get('qty')} @ {trade.get('price')}")

    def _handle_mark_price(self, data: Dict[str, Any]) -> None:
        """Handle mark price update"""
        market = data.get("m")  # market
        price = data.get("p")   # price
        if market and price:
            self._mark_prices[market] = float(price)

    # ==================== Public API ====================

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached position for symbol"""
        return self._positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached positions"""
        return self._positions.copy()

    def get_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cached open orders, optionally filtered by symbol"""
        if symbol:
            return [o for o in self._orders.values() if o.get("symbol") == symbol]
        return list(self._orders.values())

    def get_balance(self) -> Optional[Dict[str, Any]]:
        """Get cached balance"""
        return self._balance

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get cached mark price for symbol"""
        return self._mark_prices.get(symbol)

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """Wait for initial snapshot to be received"""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_position_ready(self, timeout: float = 5.0) -> bool:
        """Wait for position data"""
        if self._position_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._position_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_balance_ready(self, timeout: float = 5.0) -> bool:
        """Wait for balance data"""
        if self._balance_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._balance_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ==================== Mark Price Stream (Public) ====================

class ExtendedMarkPriceWSClient(BaseWSClient):
    """
    Separate client for mark price stream (public, no auth needed)
    URL: /stream.extended.exchange/v1/prices/mark/{market}
    """

    PING_INTERVAL = None
    RECV_TIMEOUT = 30.0

    def __init__(
        self,
        ws_url: str = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
        market: Optional[str] = None,
    ):
        super().__init__()

        # If market is specified, subscribe to that market only
        # Otherwise, subscribe to all markets
        if market:
            self.WS_URL = f"{ws_url}/prices/mark/{market}"
        else:
            self.WS_URL = f"{ws_url}/prices/mark"

        self._mark_prices: Dict[str, float] = {}
        self._events: Dict[str, asyncio.Event] = {}

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle mark price message"""
        msg_type = data.get("type")
        if msg_type == "MP":
            payload = data.get("data", {})
            market = payload.get("m")
            price = payload.get("p")
            if market and price:
                self._mark_prices[market] = float(price)
                if market in self._events:
                    self._events[market].set()

    async def _resubscribe(self) -> None:
        """No explicit subscription needed for mark price stream"""
        self._mark_prices.clear()
        for ev in self._events.values():
            ev.clear()

    def _build_ping_message(self) -> Optional[str]:
        return None

    def get_mark_price(self, symbol: str) -> Optional[float]:
        return self._mark_prices.get(symbol)

    async def wait_price(self, symbol: str, timeout: float = 5.0) -> Optional[float]:
        """Wait for mark price of symbol"""
        if symbol not in self._events:
            self._events[symbol] = asyncio.Event()

        if symbol in self._mark_prices:
            return self._mark_prices[symbol]

        try:
            await asyncio.wait_for(self._events[symbol].wait(), timeout=timeout)
            return self._mark_prices.get(symbol)
        except asyncio.TimeoutError:
            return None


# ==================== Orderbook Stream (Public) ====================

class ExtendedOrderbookWSClient(BaseWSClient):
    """
    Orderbook stream client (public, no auth needed)
    URL: /stream.extended.exchange/v1/orderbooks/{market}

    - SNAPSHOT: Full orderbook (initial and every minute)
    - DELTA: Changes since last update
    """

    PING_INTERVAL = None
    RECV_TIMEOUT = 30.0

    def __init__(
        self,
        ws_url: str = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
        market: str = None,
    ):
        super().__init__()

        if not market:
            raise ValueError("market is required for orderbook stream")

        self._market = market
        self.WS_URL = f"{ws_url}/orderbooks/{market}"

        # Orderbook state: {price: qty}
        self._bids: Dict[float, float] = {}  # price -> qty (descending)
        self._asks: Dict[float, float] = {}  # price -> qty (ascending)

        self._last_seq = 0
        self._ready_event = asyncio.Event()

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle orderbook message (SNAPSHOT or DELTA)"""
        msg_type = data.get("type")
        seq = data.get("seq", 0)
        payload = data.get("data", {})

        # Check sequence
        if seq > 0 and self._last_seq > 0 and seq != self._last_seq + 1:
            logger.warning(f"[ExtendedOB] Sequence gap: expected {self._last_seq + 1}, got {seq}")
            # Should reconnect on gap, but for now just continue
        if seq > 0:
            self._last_seq = seq

        if msg_type == "SNAPSHOT":
            self._handle_snapshot(payload)
        elif msg_type == "DELTA":
            self._handle_delta(payload)

    def _handle_snapshot(self, data: Dict[str, Any]) -> None:
        """Handle full orderbook snapshot"""
        self._bids.clear()
        self._asks.clear()

        for bid in data.get("b", []):
            price = float(bid.get("p", 0))
            qty = float(bid.get("q", 0))
            if qty > 0:
                self._bids[price] = qty

        for ask in data.get("a", []):
            price = float(ask.get("p", 0))
            qty = float(ask.get("q", 0))
            if qty > 0:
                self._asks[price] = qty

        if not self._ready_event.is_set():
            self._ready_event.set()
        logger.debug(f"[ExtendedOB] Snapshot: {len(self._bids)} bids, {len(self._asks)} asks")

    def _handle_delta(self, data: Dict[str, Any]) -> None:
        """Handle orderbook delta update - qty is the CHANGE, not absolute"""
        for bid in data.get("b", []):
            price = float(bid.get("p", 0))
            delta_qty = float(bid.get("q", 0))
            current_qty = self._bids.get(price, 0)
            new_qty = current_qty + delta_qty
            if new_qty <= 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = new_qty

        for ask in data.get("a", []):
            price = float(ask.get("p", 0))
            delta_qty = float(ask.get("q", 0))
            current_qty = self._asks.get(price, 0)
            new_qty = current_qty + delta_qty
            if new_qty <= 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = new_qty

    async def _resubscribe(self) -> None:
        """Clear state on reconnect"""
        self._bids.clear()
        self._asks.clear()
        self._last_seq = 0
        self._ready_event.clear()

    def _build_ping_message(self) -> Optional[str]:
        return None

    def get_orderbook(self, depth: int = 20) -> Optional[Dict[str, Any]]:
        """Get current orderbook state"""
        if not self._ready_event.is_set():
            return None

        # Sort bids descending, asks ascending
        sorted_bids = sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[:depth]
        sorted_asks = sorted(self._asks.items(), key=lambda x: x[0])[:depth]

        return {
            "bids": [[price, qty] for price, qty in sorted_bids],
            "asks": [[price, qty] for price, qty in sorted_asks],
            "symbol": self._market,
        }

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait for initial snapshot"""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
