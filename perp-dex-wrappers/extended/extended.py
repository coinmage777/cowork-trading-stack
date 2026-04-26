"""
Extended Exchange Wrapper

Uses official SDK (x10-python-trading-starknet) for REST API
WebSocket is implemented separately for real-time data
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List
from decimal import Decimal

logger = logging.getLogger(__name__)

from mpdex.base import MultiPerpDex, MultiPerpDexMixin


class ExtendedExchange(MultiPerpDexMixin, MultiPerpDex):
    """
    Extended (x10) Exchange Wrapper

    REST API: 공식 SDK 사용 (x10-python-trading-starknet)
    WebSocket: 직접 구현 (SDK 미지원)
    """

    def __init__(
        self,
        api_key: str,
        public_key: str,
        private_key: str,
        vault: int,
        network: str = "mainnet",
        prefer_ws: bool = True,
    ):
        super().__init__()
        # WS 지원 플래그
        self.ws_supported.update({
            "get_mark_price": True,  # Separate public stream
            "get_orderbook": True,   # Separate public stream per symbol
            "get_position": True,
            "get_collateral": True,
            "get_open_orders": True,
            "create_order": False,
            "cancel_orders": False,
            "update_leverage": False,
        })

        self._api_key = api_key
        self._public_key = public_key
        self._private_key = private_key
        self._vault = vault
        self._network = network
        self._prefer_ws = prefer_ws

        # SDK imports (lazy)
        self._sdk_imported = False
        self._config = None
        self._account = None
        self._client = None

        # WS clients
        self.ws_client = None  # Account stream (private)
        self.mark_price_ws = None  # Mark price stream (public)
        self._orderbook_ws: Dict[str, Any] = {}  # symbol -> orderbook WS client
        self._ws_url = (
            "wss://api.starknet.extended.exchange/stream.extended.exchange/v1"
            if network == "mainnet" else
            "wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1"
        )

        # Available symbols cache
        self.available_symbols = {}
        # Market info cache (symbol -> MarketModel with trading_config for precision)
        self._markets: Dict[str, Any] = {}
        # Symbol meta cache (extracted key trading parameters)
        self._symbol_meta: Dict[str, Dict[str, Any]] = {}

    def _import_sdk(self):
        """Lazy import SDK to avoid import errors when SDK not installed"""
        if self._sdk_imported:
            return

        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG

        self._config = MAINNET_CONFIG if self._network == "mainnet" else TESTNET_CONFIG
        self._account = StarkPerpetualAccount(
            vault=self._vault,
            public_key=self._public_key,
            private_key=self._private_key,
            api_key=self._api_key,
        )
        self._sdk_imported = True

    async def init(self):
        """Initialize SDK client and optionally WS"""
        self._import_sdk()

        from x10.perpetual.trading_client import PerpetualTradingClient

        self._client = PerpetualTradingClient(
            self._config,
            self._account,
        )

        # Load markets
        await self._load_markets()

        # Initialize WebSocket if preferred
        if self._prefer_ws:
            await self._init_ws()

        return self

    def get_perp_quote(self, symbol):
        return 'USD'

    def _get_symbol_meta(self, symbol: str) -> Dict[str, Any]:
        """Get cached symbol meta, return defaults if not found"""
        meta = self._symbol_meta.get(symbol)
        if not meta:
            return {
                "tick_size": "0.01",
                "lot_size": "0.01",
                "min_order_size": "0.1",
                "max_leverage": 1,
            }
        return meta

    async def _load_markets(self):
        """Load available markets from API (with trading_config for precision)"""
        try:
            response = await self._client.markets_info.get_markets()
            markets = response.data or []
            perp_symbols = []
            for market in markets:
                name = market.name
                perp_symbols.append(name)
                # Cache full market info
                self._markets[name] = market
                # Extract key trading parameters to _symbol_meta
                tc = market.trading_config
                self._symbol_meta[name] = {
                    "asset_precision": market.asset_precision,
                    "collateral_precision": market.collateral_asset_precision,
                    "active": market.active,
                    "min_order_size": str(tc.min_order_size),
                    "lot_size": str(tc.min_order_size_change),
                    "tick_size": str(tc.min_price_change),
                    "max_leverage": int(tc.max_leverage),
                    "max_market_order_value": str(tc.max_market_order_value),
                    "max_limit_order_value": str(tc.max_limit_order_value),
                    "max_position_value": str(tc.max_position_value),
                    "max_num_orders": tc.max_num_orders,
                }
            self.available_symbols = {"perp": perp_symbols}
        except Exception as e:
            logger.warning(f"Failed to load markets: {e}")
            self.available_symbols = {"perp": []}

    async def _init_ws(self):
        """Initialize WebSocket clients"""
        # Account stream (private)
        try:
            from .extended_ws_client import ExtendedWSClient

            self.ws_client = ExtendedWSClient(
                api_key=self._api_key,
                ws_url=self._ws_url,
            )
            await self.ws_client.connect()

            # Wait for initial snapshot
            await self.ws_client.wait_ready(timeout=5.0)
            logger.info("Account WebSocket initialized")
        except Exception as e:
            logger.warning(f"Account WebSocket init failed: {e}")
            self.ws_client = None

        # Mark price stream (public, all markets)
        try:
            from .extended_ws_client import ExtendedMarkPriceWSClient

            self.mark_price_ws = ExtendedMarkPriceWSClient(
                ws_url=self._ws_url,
                market=None,  # Subscribe to all markets
            )
            await self.mark_price_ws.connect()
            logger.info("Mark price WebSocket initialized")
        except Exception as e:
            logger.warning(f"Mark price WebSocket init failed: {e}")
            self.mark_price_ws = None

    # ==================== Core Interface Methods ====================

    async def get_collateral(self) -> Dict[str, Any]:
        """Get account balance/collateral"""
        # Try WS first
        if self._prefer_ws and self.ws_client:
            balance = self.ws_client.get_balance()
            if balance:
                return balance

        # SDK fallback
        logger.info("get_collateral: REST fallback")
        response = await self._client.account.get_balance()
        result = response.data

        return {
            "available_collateral": float(result.available_for_trade) if result else 0,
            "total_collateral": float(result.equity) if result else 0,
            "unrealized_pnl": float(result.unrealised_pnl) if result else 0,
        }

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get position for symbol"""
        # Try WS first - if WS is ready, trust the cache (None = no position)
        if self._prefer_ws and self.ws_client and self.ws_client._position_event.is_set():
            return self.ws_client.get_position(symbol)  # None means no position

        # SDK fallback (WS not ready or not preferred)
        logger.info(f"get_position({symbol}): REST fallback")
        response = await self._client.account.get_positions(market_names=[symbol])
        positions = response.data

        if not positions:
            return None

        # Parse first matching position
        for pos in positions:
            if pos.market == symbol:
                size = float(pos.size)

                if size == 0:
                    return None

                return {
                    "symbol": symbol,
                    "side": "long" if str(pos.side).upper() == "LONG" else "short",
                    "size": size,
                    "entry_price": float(pos.open_price),
                    "unrealized_pnl": float(pos.unrealised_pnl),
                    "liquidation_price": float(pos.liquidation_price) if pos.liquidation_price else None,
                    "raw_data": pos,
                }

        return None

    async def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """Get open orders for symbol"""
        # Try WS first - if WS is ready, trust the cache (empty list = no orders)
        if self._prefer_ws and self.ws_client and self.ws_client._orders_event.is_set():
            return self.ws_client.get_orders(symbol)  # Empty list means no orders

        # SDK fallback (WS not ready or not preferred)
        logger.info(f"get_open_orders({symbol}): REST fallback")
        response = await self._client.account.get_open_orders(market_names=[symbol])
        result = response.data or []

        orders = []
        for order in result:
            orders.append({
                "id": order.id,
                "symbol": order.market,
                "side": str(order.side).lower(),
                "size": float(order.qty),
                "price": float(order.price),
                "type": str(order.type).lower(),
                "status": str(order.status).lower(),
            })

        return orders

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        order_type: str = "market",
        **kwargs,
    ) -> Dict[str, Any]:
        """Create order via SDK"""
        from decimal import ROUND_DOWN, ROUND_UP
        from x10.perpetual.orders import OrderSide

        # SDK side format
        sdk_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        # Get market info for precision
        market = self._markets.get(symbol)
        if not market:
            # Try to load markets if not cached
            await self._load_markets()
            market = self._markets.get(symbol)

        # For market orders, we need a price - get mark price
        if price is None:
            response = await self._client.markets_info.get_market_statistics(market_name=symbol)
            stats = response.data
            if stats:
                # Use mark price with slippage for market order
                mark = float(stats.mark_price)
                price = mark * 1.01 if side.upper() == "BUY" else mark * 0.99

        if price is None:
            raise ValueError("Could not determine price for order")

        # Apply precision from trading_config
        price_dec = Decimal(str(price))
        amount_dec = Decimal(str(amount))

        if market and market.trading_config:
            tc = market.trading_config
            # Round price: BUY rounds up, SELL rounds down
            rounding = ROUND_UP if side.upper() == "BUY" else ROUND_DOWN
            price_dec = tc.round_price(price_dec, rounding_direction=rounding)
            # Round amount down to avoid exceeding balance
            amount_dec = tc.round_order_size(amount_dec, rounding_direction=ROUND_DOWN)

        # Handle is_reduce_only -> reduce_only (SDK uses reduce_only)
        reduce_only = kwargs.pop("is_reduce_only", False)

        # Place order via SDK
        response = await self._client.place_order(
            market_name=symbol,
            amount_of_synthetic=amount_dec,
            price=price_dec,
            side=sdk_side,
            reduce_only=reduce_only,
            **kwargs,
        )
        result = response.data

        return {
            "id": result.id if result else None,
            "external_id": result.external_id if result else None,
            "symbol": symbol,
            "side": side.lower(),
            "size": float(amount_dec),
            "price": float(price_dec),
            "type": order_type,
            "status": "pending",
        }

    async def cancel_orders(
        self,
        symbol: str,
        open_orders: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Cancel orders"""
        cancelled = []

        if open_orders:
            # Cancel specific orders
            for order in open_orders:
                order_id = order.get("id")
                if order_id:
                    try:
                        await self._client.orders.cancel_order(order_id=order_id)
                        cancelled.append(order)
                    except Exception as e:
                        logger.warning(f"Failed to cancel order {order_id}: {e}")
        else:
            # Mass cancel for symbol
            try:
                await self._client.orders.mass_cancel(markets=[symbol])
                cancelled = []  # All orders cancelled
            except Exception as e:
                logger.warning(f"Mass cancel failed: {e}")

        return cancelled

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get mark price for symbol"""
        # Try mark price WS first
        if self._prefer_ws and self.mark_price_ws:
            price = self.mark_price_ws.get_mark_price(symbol)
            if price:
                return price

        # REST fallback
        logger.info(f"get_mark_price({symbol}): REST fallback")
        try:
            response = await self._client.markets_info.get_market_statistics(market_name=symbol)
            stats = response.data
            if stats:
                return float(stats.mark_price)
        except Exception:
            pass

        return None

    async def get_orderbook(self, symbol: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Get orderbook via WS (lazy connection per symbol)"""
        from .extended_ws_client import ExtendedOrderbookWSClient

        # Create WS client for this symbol if not exists
        if symbol not in self._orderbook_ws:
            client = ExtendedOrderbookWSClient(
                ws_url=self._ws_url,
                market=symbol,
            )
            await client.connect()
            self._orderbook_ws[symbol] = client

        client = self._orderbook_ws[symbol]

        # Wait for snapshot if not ready
        if not client._ready_event.is_set():
            ready = await client.wait_ready(timeout=timeout)
            if not ready:
                raise TimeoutError(f"Orderbook WS not ready for {symbol}")

        orderbook = client.get_orderbook()
        if orderbook is None:
            raise ValueError(f"No orderbook data for {symbol}")

        return orderbook

    async def update_leverage(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Update leverage for symbol (only cross margin supported).

        Args:
            leverage: If None, leverage is not updated.
            margin_mode: Ignored - Extended only supports cross margin.

        Note: At least one of leverage or margin_mode should be provided.
        """
        if leverage is None and margin_mode is None:
            return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

        meta = self._get_symbol_meta(symbol)
        max_lev = meta.get("max_leverage", 1)

        message = None
        if margin_mode and margin_mode.lower() == "isolated":
            message = "Extended only supports cross margin."
            logger.info(message)

        # If leverage is None, nothing to update (margin_mode is always cross)
        if leverage is None:
            result = {
                "symbol": symbol,
                "leverage": None,
                "margin_mode": "cross",
                "status": "ok",
                "max_leverage": max_lev,
            }
            if message:
                result["message"] = message
            return result

        res = await self._client.account.update_leverage(market_name=symbol, leverage=Decimal(leverage))
        result = {
            "symbol": symbol,
            "leverage": leverage,
            "margin_mode": "cross",
            "status": "ok",
            "max_leverage": max_lev,
            "result": res,
        }
        if message:
            result["message"] = message
        return result

    async def get_leverage_info(self, symbol: str) -> Dict[str, Any]:
        """Get leverage info for symbol (only cross margin supported)"""
        meta = self._get_symbol_meta(symbol)
        max_lev = meta.get("max_leverage", 1)
        try:
            res = await self._client.account.get_leverage(market_names=[symbol])
            # res: status='OK' data=[AccountLeverage(market='BTC-USD', leverage=Decimal('50'))]
            leverage = None
            if res and res.data:
                for item in res.data:
                    if item.market == symbol:
                        leverage = int(item.leverage)
                        break
            return {
                "symbol": symbol,
                "leverage": leverage,
                "margin_mode": "cross",
                "status": "ok",
                "max_leverage": max_lev,
                "available_margin_modes": ["cross"],
            }
        except Exception as e:
            return {
                "symbol": symbol,
                "leverage": None,
                "margin_mode": None,
                "status": "error",
                "max_leverage": max_lev,
                "available_margin_modes": ["cross"],
                "message": str(e),
            }

    async def get_available_symbols(self) -> Dict[str, List[str]]:
        """Get available trading symbols"""
        if not self.available_symbols:
            await self._load_markets()
        return self.available_symbols

    async def close(self):
        """Close all connections"""
        if self.ws_client:
            await self.ws_client.close()
            self.ws_client = None

        if self.mark_price_ws:
            await self.mark_price_ws.close()
            self.mark_price_ws = None

        # Close orderbook WS clients
        for symbol, client in list(self._orderbook_ws.items()):
            try:
                await client.close()
            except Exception:
                pass
        self._orderbook_ws.clear()

        if self._client:
            # SDK cleanup if needed
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

        logger.info("Closed")
