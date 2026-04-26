from abc import ABC, abstractmethod

class MultiPerpDex(ABC):
    def __init__(self):
        self.has_spot = False
        self.has_margin_mode = False # isolated / cross 지원 여부
        self.available_symbols = {}
        # WebSocket support flags for each function
        # Override in subclass if WS is implemented for that function
        self.ws_supported = {
            "get_mark_price": False,
            "get_position": False,
            "get_open_orders": False,
            "get_collateral": False,
            "get_orderbook": False,
            "create_order": False,
            "cancel_orders": False,
            "update_leverage": False,
        }

    @abstractmethod
    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        """
        If price is None, it is a market order.
        """
        pass

    @abstractmethod
    async def get_position(self, symbol):
        """
        side: long / short
        size:
        entry_price:
        unrealized_pnl:
        # 추가
        leverage_type: isolated / cross
        leverage_value: int
        liquidation_price: float or None
        """
        pass
    
    @abstractmethod
    async def close_position(self, symbol, position, *, is_reduce_only=True):
        pass
    
    @abstractmethod
    async def get_collateral(self):
        pass
    
    @abstractmethod
    async def get_open_orders(self, symbol):
        """
        Output: List of open orders for the given symbol.
        Each order is a dict with at least the following keys:
            - id: Order ID
            - symbol: Trading pair symbol
            - side: 'buy' or 'sell'
            - size: Order size/amount
            - price: Order price (None for market orders)
        """
        pass
    
    @abstractmethod
    async def cancel_orders(self, symbol, open_orders = None):
        """
        Docstring for cancel_orders
        If open_orders is None, cancel all open orders for the given symbol.
        open_orders: List of orders to cancel. If provided, only these orders will be canceled.
        Each open order is a dict with at least the following keys:
            - id: Order ID
            - symbol: Trading pair symbol
            - side: 'buy' or 'sell'
            - size: Order size/amount
            - price: Order price (None for market orders)
        """
        pass

    @abstractmethod
    async def get_mark_price(self,symbol):
        pass

    @abstractmethod
    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        """
        Update leverage and/or margin mode for the given symbol.

        Args:
            symbol: Trading pair symbol
            leverage: int or None. If None, only margin_mode is updated (if provided).
            margin_mode: 'isolated' or 'cross' or None. If None, only leverage is updated (if provided).

        Note: At least one of leverage or margin_mode should be provided.
        """
        pass

    @abstractmethod
    async def get_leverage_info(self, symbol):
        """
        Get current leverage settings for the given symbol.
        Returns:
            {
                "symbol": str,
                "leverage": int or None,
                "margin_mode": 'isolated' or 'cross' or None,
                "status": "ok" or "error",
                "max_leverage": int or None,
                "available_margin_modes": list (e.g., ["cross", "isolated"] or ["cross"]),
            }
        """
        pass

    @abstractmethod
    async def get_available_symbols(self):
        pass

    @abstractmethod
    async def close(self):
        """
        Close exchange connections (HTTP sessions, WebSocket clients, etc.)
        Must be called when done using the exchange to properly release resources.
        """
        pass

class MultiPerpDexMixin:
    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        """
        Default implementation: returns not_implemented status.

        Args:
            leverage: If None, only margin_mode is updated (if provided).
            margin_mode: If None, only leverage is updated (if provided).
        """
        return {
            "symbol": symbol,
            "leverage": leverage,
            "margin_mode": margin_mode,
            "status": "not_implemented",
        }

    async def get_leverage_info(self, symbol):
        """Default implementation: returns not_implemented status."""
        return {
            "symbol": symbol,
            "leverage": None,
            "margin_mode": None,
            "status": "not_implemented",
            "max_leverage": None,
            "available_margin_modes": []
        }

    async def get_available_symbols(self):
        """
        Returns a dictionary of available trading symbols categorized by market type.
        Example output:
        {
            "perp": ["BTC-USDT", "ETH-USDT", ...],
            "spot": ["BTC/USDT", "ETH/USDT", ...]
        }
        For hyperliquid, it returns:
        {
            "perp": {dex: [symbols...] for dex in self.supported_dexes},
            "spot": [symbols...]
        }
        
        """
        if self.available_symbols == {}:
            raise NotImplementedError("get_available_symbols method not implemented.")
        
        return self.available_symbols

    async def get_open_orders(self, symbol):
        return await self.exchange.fetch_open_orders(symbol)
    
    async def close_position(self, symbol, position=None, *, is_reduce_only=True):
        if position is None:
            position = await self.get_position(symbol)
        if not position:
            return None
        size = position.get('size')
        side = 'sell' if position.get('side').lower() in ['long','buy'] else 'buy'
        return await self.create_order(symbol, side, size, price=None, order_type='market', is_reduce_only=is_reduce_only)