from mpdex.base import MultiPerpDex, MultiPerpDexMixin
from pysdk.grvt_ccxt_pro import GrvtCcxtPro
from pysdk.grvt_ccxt_env import GrvtEnv
import logging
from pysdk.grvt_ccxt_utils import rand_uint32
import os
import asyncio
from typing import Optional, Dict, Any

def create_logger(name: str, filename: str, level=logging.ERROR) -> logging.Logger:
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

grvt_logger = create_logger("grvt_logger", "grvt_error.log")

class GrvtExchange(MultiPerpDexMixin, MultiPerpDex):
    # WebSocket supported operations
    ws_supported = {
        "mark_price": True,
        "orderbook": True,
        "position": True,
        "collateral": False,  # Not available via WS
        "open_orders": True,
        "create_order": True,  # WS RPC
        "cancel_orders": True,  # WS RPC
    }

    def __init__(self, api_key, account_id, secret_key, use_ws: bool = True):
        super().__init__()
        logging.getLogger().setLevel(logging.ERROR)
        self.logger = grvt_logger
        self.api_key = api_key
        self.account_id = account_id
        self.secret_key = secret_key
        self.use_ws = use_ws
        self._ws_client = None

        self.exchange = GrvtCcxtPro(
            GrvtEnv("prod"),
            self.logger,
            parameters={
                "api_key": api_key,
                "trading_account_id": account_id,
                "private_key": secret_key,
            }
        )
    
    async def init(self):
        await self.exchange.load_markets()
        # Update available symbols from loaded markets
        self.available_symbols["perp"] = []
        for symbol in self.exchange.markets:
            if "_Perp" in symbol:  # GRVT perp format: BTC_USDT_Perp
                self.available_symbols["perp"].append(symbol)
        if self.use_ws:
            await self._init_ws()
        return self

    async def _init_ws(self):
        """Initialize WebSocket client"""
        try:
            from .grvt_ws_client import get_grvt_ws_client
            self._ws_client = await get_grvt_ws_client(
                self.api_key,
                self.account_id,
                self.secret_key
            )

            # Pre-subscribe and init cache
            if self._ws_client and self._ws_client.connected:
                await self._ws_client.subscribe_position()
                await self._ws_client.subscribe_orders()
                # orders stream doesn't send snapshot, init cache via REST
                orders = await super().get_open_orders(None)
                if orders:
                    parsed = self.parse_open_orders(orders)
                    if parsed:
                        for o in parsed:
                            sym = o.get("symbol")
                            if sym:
                                if sym not in self._ws_client._open_orders:
                                    self._ws_client._open_orders[sym] = []
                                self._ws_client._open_orders[sym].append(o)
        except Exception as e:
            self.logger.error(f"Failed to init WS client: {e}")
            self._ws_client = None
    
    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USD'
    
    async def get_mark_price(self, symbol) -> Optional[float]:
        # Try WS first
        if self._ws_client and self._ws_client.connected:
            # Subscribe if not yet
            if symbol not in self._ws_client._ticker_subs:
                await self._ws_client.subscribe_ticker(symbol)
                await asyncio.sleep(0.5)  # Wait for first data

            price = self._ws_client.get_mark_price(symbol)
            if price and self._ws_client.is_price_fresh(symbol):
                return price

        # Fallback to REST
        self.logger.info("[grvt] get_mark_price: using REST fallback")
        res = await self.exchange.fetch_ticker(symbol)
        return float(res['mark_price'])
    
    def parse_order(self, order):
        # 2026-04-25: 에러 응답(str) / metadata 누락 dict 안전 처리
        if not isinstance(order, dict):
            return None
        meta = order.get('metadata')
        if not isinstance(meta, dict):
            return None
        return meta.get('client_order_id')
        
    async def create_order(self, symbol, side, amount, price=None, order_type='market', *, is_reduce_only=False):
        # Try WS first (faster)
        if self._ws_client and self._ws_client.connected:
            try:
                result = await self._ws_client.create_order(symbol, side, amount, price, order_type)
                if result:
                    return result
            except Exception as e:
                self.logger.error(f"WS create_order failed, falling back to REST: {e}")

        # Fallback to REST
        self.logger.info("[grvt] create_order: using REST fallback")
        params = {"client_order_id": rand_uint32()}
        if price is not None:
            order_type = 'limit'

        if order_type == 'market':
            res = await self.exchange.create_order(symbol, 'market', side, amount, price, params=params)
            return self.parse_order(res)

        res = await self.exchange.create_order(symbol, 'limit', side, amount, price, params=params)
        return self.parse_order(res)
    
    def parse_position(self, pos):
        side = 'short' if '-' in pos['size'] else 'long'
        size = pos['size'].replace('-', '')
        return {
            "symbol": pos.get('instrument'),
            "side": side,
            "size": size,
            "entry_price": pos['entry_price'],
            "unrealized_pnl": pos['unrealized_pnl'],
            "liquidation_price": pos.get('est_liquidation_price', None),
            "raw_data": pos,
        }
    
    async def get_orderbook(self, symbol) -> Optional[Dict[str, Any]]:
        """Get orderbook with WS→REST fallback"""
        # Try WS first
        if self._ws_client and self._ws_client.connected:
            if symbol not in self._ws_client._book_subs:
                await self._ws_client.subscribe_orderbook(symbol)
                await asyncio.sleep(0.5)

            book = self._ws_client.get_orderbook(symbol)
            if book and self._ws_client.is_orderbook_fresh(symbol):
                return book

        # Fallback to REST
        self.logger.info("[grvt] get_orderbook: using REST fallback")
        try:
            res = await self.exchange.fetch_order_book(symbol)
            return {
                "bids": [[float(b[0]), float(b[1])] for b in res.get("bids", [])],
                "asks": [[float(a[0]), float(a[1])] for a in res.get("asks", [])]
            }
        except Exception as e:
            self.logger.error(f"get_orderbook error: {e}")
            return None

    async def get_position(self, symbol) -> Optional[Dict[str, Any]]:
        # Try WS first
        if self._ws_client and self._ws_client.connected:
            if not self._ws_client._position_subscribed:
                await self._ws_client.subscribe_position()

            # Subscribe sets event immediately, cache starts as None
            # If position exists, snapshot will have updated cache
            if self._ws_client.is_position_ready():
                return self._ws_client.get_position(symbol)

        # Fallback to REST (WS not connected)
        self.logger.info("[grvt] get_position: using REST fallback")
        try:
            positions = await self.exchange.fetch_positions(symbols=[symbol])
        except Exception as e:
            self.logger.error(e, exc_info=True)
            return None

        if len(positions) > 1:
            self.logger.error('can not have more than 1 position', exc_info=True)
            return None

        if len(positions) == 0:
            return None

        pos = positions[0]
        return self.parse_position(pos)
    
    async def get_collateral(self):
        try:
            res = await self.exchange.get_account_summary("sub-account")
            available_collateral = round(float(res['available_balance']),2)
            total_collateral = round(float(res['total_equity']),2)
        except Exception as e:
            self.logger.error(e, exc_info=True)
            available_collateral = None
            total_collateral = None
            
        return {
            "available_collateral": available_collateral,
            "total_collateral": total_collateral
        }
    
    async def close_position(self, symbol, position, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)
    
    async def close(self, force_close: bool = True):
        """
        Close connection.

        Args:
            force_close: True (default) = 연결 종료, False = 풀에 유지
        """
        # Close WS client if exists
        if self._ws_client:
            try:
                from .grvt_ws_client import release_grvt_ws_client
                await release_grvt_ws_client(
                    self.account_id,
                    env="prod",  # or pass actual env
                    force_close=force_close
                )
            except Exception:
                pass
            self._ws_client = None
        # Close REST session and prevent __del__ from trying again
        if self.exchange._session and not self.exchange._session.closed:
            await self.exchange._session.close()
        self.exchange._session = None
    
    def parse_open_orders(self, orders):
        """id, symbol, type, side, size, price"""
        if len(orders) == 0:
            return None
        parsed = []
        for order in orders:
            #print(order)
            order_id = order['order_id']
            symbol = order['legs'][0]['instrument']
            size = order['legs'][0]['size']
            price = order['legs'][0]['limit_price']
            side = 'buy' if order['legs'][0]['is_buying_asset'] else 'sell'
            parsed.append({"id": order_id, "symbol": symbol, "size": size, "price": price, "side": side})
        return parsed
    
    async def get_open_orders(self, symbol):
        if self._ws_client and self._ws_client.connected:
            if self._ws_client.is_orders_ready():
                orders = self._ws_client.get_open_orders(symbol)
                return orders if orders else None

        # Fallback to REST (WS not connected)
        self.logger.info("[grvt] get_open_orders: using REST fallback")
        orders = await super().get_open_orders(symbol)
        return self.parse_open_orders(orders)
    
    async def cancel_orders(self, symbol, open_orders=None):
        # Try WS first (faster)
        if self._ws_client and self._ws_client.connected:
            try:
                if open_orders is None:
                    # Cancel all orders for symbol via WS
                    success = await self._ws_client.cancel_all_orders(symbol)
                    if success:
                        return [{"status": "cancelled", "symbol": symbol}]
                else:
                    # Cancel specific orders via WS
                    if not isinstance(open_orders, list):
                        open_orders = [open_orders]

                    results = []
                    for item in open_orders:
                        order_id = item.get('id') if isinstance(item, dict) else item
                        success = await self._ws_client.cancel_order(order_id)
                        results.append({"id": order_id, "cancelled": success})
                    return results
            except Exception as e:
                self.logger.error(f"WS cancel_orders failed, falling back to REST: {e}")

        # Fallback to REST
        self.logger.info("[grvt] cancel_orders: using REST fallback")
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []

        if not isinstance(open_orders, list):
            open_orders = [open_orders]

        tasks = []
        for item in open_orders:
            order_id = item['id']
            tasks.append(asyncio.create_task(self.exchange.cancel_order(id=order_id)))
        return await asyncio.gather(*tasks, return_exceptions=True)
        