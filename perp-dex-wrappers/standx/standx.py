"""
StandX Perps Exchange Wrapper

Implements MultiPerpDex interface for StandX perpetual trading.
"""
import asyncio
import json
import logging
import time
import aiohttp
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any, List

from mpdex.base import MultiPerpDex, MultiPerpDexMixin
from .standx_auth import StandXAuth

logger = logging.getLogger(__name__)


STANDX_PERPS_BASE = "https://perps.standx.com"


class StandXExchange(MultiPerpDexMixin, MultiPerpDex):
    """
    StandX Perps Exchange Wrapper

    Usage:
        # With private key (auto sign)
        ex = StandXExchange(
            wallet_address="0x...",
            chain="bsc",
            evm_private_key="0x...",
        )
        await ex.init()

        # With browser signing
        ex = StandXExchange(
            wallet_address="0x...",
            chain="bsc"
        )
        await ex.init(login_port=7081)
    """

    def __init__(
        self,
        wallet_address: str,
        chain: str = "bsc",
        evm_private_key: Optional[str] = None,
        session_token: Optional[str] = None,
        http_timeout: float = 30.0,
    ):
        super().__init__()
        self.has_spot = False

        self.wallet_address = wallet_address
        self.chain = chain
        self._http_timeout = http_timeout
        # WS support flags
        self.ws_supported = {
            "get_mark_price": True,
            "get_position": True,  # REST initial cache + WS updates
            "get_open_orders": True,  # REST initial cache + WS updates
            "get_collateral": False,  # WS exists, but no data
            "get_orderbook": True,
            "create_order": True,  # Order Response Stream (ws-api/v1)
            "cancel_orders": True,  # Order Response Stream (ws-api/v1)
            "update_leverage": False,
        }

        # WS preference (can be disabled for REST fallback)
        self._prefer_ws = True
        self._prefer_order_ws = True

        # Auth handler
        self._auth = StandXAuth(
            wallet_address=wallet_address,
            chain=chain,
            evm_private_key=evm_private_key,
            session_token=session_token,
            http_timeout=http_timeout,
        )

        # Symbol info cache
        self._symbol_meta: Dict[str, Dict] = {}

        # Collateral symbol
        self.COLLATERAL_SYMBOL = "DUSD"

        # WebSocket clients
        self.ws_client = None  # Market Stream (ws-stream/v1)
        self.order_ws_client = None  # Order Response Stream (ws-api/v1)

        # REST cache for collateral (rate limit 방지)
        self._collateral_cache: Optional[Dict[str, Any]] = None
        self._collateral_cache_at: float = 0.0  # monotonic timestamp
        self._collateral_cache_ttl_ms: float = 1000.0  # 1초 캐시 (기본값)

        # REST fallback counters (연속 fallback 횟수 추적)
        self._fallback_lock = asyncio.Lock()
        self._ws_fallback_count: Dict[str, int] = {
            "get_mark_price": 0,
            "get_position": 0,
            "get_open_orders": 0,
        }
        self._order_ws_fallback_count: Dict[str, int] = {
            "create_order": 0,
            "cancel_orders": 0,
        }

        # Leverage update cache (symbol -> (leverage, margin_mode))
        self._leverage_cache: Dict[str, Dict[str, Any]] = {}
        self.has_margin_mode = True  # isolated / cross 지원

    async def init(self, login_port: Optional[int] = None, open_browser: bool = True) -> "StandXExchange":
        """
        Initialize exchange: login and fetch symbol info

        Args:
            login_port: Port for browser signing (None = use private key)
            open_browser: Auto open browser for signing
        """
        await self._auth.login(port=login_port, open_browser=open_browser)
        await self._update_available_symbols()

        # Initialize WebSocket clients
        await self._create_ws_client()
        if self._prefer_order_ws:
            await self._create_order_ws_client()

        # Subscribe to user channels and load initial cache
        if self._prefer_ws and self.ws_client:
            await self.ws_client.subscribe_user_channels()
            await self._load_initial_cache()

        return self

    async def _create_ws_client(self):
        """Create and authenticate WebSocket client (Market Stream)"""
        if self.ws_client is not None:
            return

        from .standx_ws_client import WS_POOL

        self.ws_client = await WS_POOL.acquire(
            wallet_address=self.wallet_address,
            jwt_token=self._auth.token,
        )
        # Reconnect 후 REST API로 캐시 갱신하도록 콜백 등록
        self.ws_client.on_reconnect = self._load_initial_cache

    async def _create_order_ws_client(self):
        """Create and authenticate Order WebSocket client (Order Response Stream)"""
        if self.order_ws_client is not None:
            return

        from .standx_ws_client import ORDER_WS_POOL

        self.order_ws_client = await ORDER_WS_POOL.acquire(
            wallet_address=self.wallet_address,
            jwt_token=self._auth.token,
            auth_handler=self._auth,
        )
        logger.debug("[StandXExchange] Order WS initialized")

    async def _load_initial_cache(self) -> None:
        """Load initial data via REST and set in WS cache"""
        if not self.ws_client:
            return

        try:
            # 1. Positions
            positions_resp = await self._auth_get(f"{STANDX_PERPS_BASE}/api/query_positions")
            if isinstance(positions_resp, list):
                self.ws_client.set_initial_positions(positions_resp)

            # 2. Open Orders
            orders_resp = await self._auth_get(f"{STANDX_PERPS_BASE}/api/query_open_orders")
            orders = orders_resp.get("result", []) if isinstance(orders_resp, dict) else []
            self.ws_client.set_initial_orders(orders)

            # 3. Collateral/Balance
            balance_resp = await self._auth_get(f"{STANDX_PERPS_BASE}/api/query_balance")
            if isinstance(balance_resp, dict):
                self.ws_client.set_initial_collateral(balance_resp)

        except Exception as e:
            logger.debug(f"[StandXExchange] Failed to load initial cache: {e}")

    async def _reauth(self) -> bool:
        """Re-authenticate when token expires"""
        logger.debug("[standx] token expired, re-authenticating...")
        try:
            self._auth._token = None
            self._auth._logged_in = False
            await self._auth.login()

            # Update WS client token if exists
            if self.ws_client:
                self.ws_client.jwt_token = self._auth.token
                self.ws_client._authenticated = False
                if self.ws_client._ws and self.ws_client._running:
                    await self.ws_client.authenticate(self._auth.token)

            # Update Order WS client token if exists
            if self.order_ws_client:
                self.order_ws_client.jwt_token = self._auth.token
                self.order_ws_client._auth_handler = self._auth
                self.order_ws_client._authenticated = False
                if self.order_ws_client._ws and self.order_ws_client._running:
                    await self.order_ws_client._do_auth()

            logger.debug("[standx] re-authentication successful")
            return True
        except Exception as e:
            logger.debug(f"[standx] re-authentication failed: {e}")
            return False

    async def _auth_get(self, url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None) -> aiohttp.ClientResponse:
        """GET request with auto re-auth on 401/403"""
        if headers is None:
            headers = {}
        headers.update(self._auth.get_auth_headers())

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status in (401, 403):
                    if await self._reauth():
                        headers.update(self._auth.get_auth_headers())
                        async with session.get(url, headers=headers, params=params) as retry_resp:
                            return await self._handle_response(retry_resp)
                return await self._handle_response(resp)

    async def _auth_post(self, url: str, data: Optional[str] = None, headers: Optional[Dict] = None) -> Dict[str, Any]:
        """POST request with auto re-auth on 401/403"""
        if headers is None:
            headers = {}
        headers.update(self._auth.get_auth_headers())

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.post(url, data=data, headers=headers) as resp:
                if resp.status in (401, 403):
                    if await self._reauth():
                        # Re-sign the request if needed
                        if data and "x-request-signature" in headers:
                            headers.update(self._auth.sign_request(data))
                        headers.update(self._auth.get_auth_headers())
                        async with session.post(url, data=data, headers=headers) as retry_resp:
                            return await self._handle_response(retry_resp)
                return await self._handle_response(resp)

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        """Handle response and return JSON"""
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Request failed: {resp.status} {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    async def close(self, force_close: bool = True):
        """
        Cleanup - release WebSocket clients.

        Args:
            force_close: If True (default), actually close and remove from pool.
                         Set False to keep connection alive for reuse.
        """
        if self.ws_client:
            from .standx_ws_client import WS_POOL
            await WS_POOL.release(self.wallet_address, force_close=force_close)
            self.ws_client = None

        if self.order_ws_client:
            from .standx_ws_client import ORDER_WS_POOL
            await ORDER_WS_POOL.release(self.wallet_address, force_close=force_close)
            self.order_ws_client = None

    def get_fallback_stats(self) -> Dict[str, Any]:
        """
        Get consecutive REST fallback counts for each WS client.

        Returns:
            {
                "ws_client": {
                    "get_mark_price": 0,
                    "get_position": 0,
                    "get_open_orders": 0,
                    "total": 0,
                },
                "order_ws_client": {
                    "create_order": 0,
                    "cancel_orders": 0,
                    "total": 0,
                }
            }
        """
        ws_total = sum(self._ws_fallback_count.values())
        order_ws_total = sum(self._order_ws_fallback_count.values())

        return {
            "ws_client": {
                **self._ws_fallback_count,
                "total": ws_total,
            },
            "order_ws_client": {
                **self._order_ws_fallback_count,
                "total": order_ws_total,
            },
        }

    async def reset_fallback_stats(self) -> None:
        """Reset all fallback counters to 0"""
        async with self._fallback_lock:
            for key in self._ws_fallback_count:
                self._ws_fallback_count[key] = 0
            for key in self._order_ws_fallback_count:
                self._order_ws_fallback_count[key] = 0

    async def _increment_fallback(self, method: str, is_order_ws: bool = False) -> None:
        """Increment fallback counter for a method"""
        async with self._fallback_lock:
            if is_order_ws:
                self._order_ws_fallback_count[method] += 1
            else:
                self._ws_fallback_count[method] += 1

    async def _reset_fallback(self, method: str, is_order_ws: bool = False) -> None:
        """Reset fallback counter for a method (WS success)"""
        async with self._fallback_lock:
            if is_order_ws:
                self._order_ws_fallback_count[method] = 0
            else:
                self._ws_fallback_count[method] = 0

    # ----------------------------
    # Symbol Info
    # ----------------------------
    async def _update_available_symbols(self):
        """Fetch available trading symbols"""
        self.available_symbols["perp"] = []

        # Get all symbol info
        symbols = await self._query_symbol_meta()
        for info in symbols:
            symbol = info.get("symbol")
            # status == "trading" means the symbol is active
            if symbol and info.get("status") == "trading":
                self.available_symbols["perp"].append(symbol)
                self._symbol_meta[symbol] = info

    async def _query_symbol_meta(self, symbol: Optional[str] = None) -> List[Dict]:
        """GET /api/query_symbol_info"""
        url = f"{STANDX_PERPS_BASE}/api/query_symbol_info"
        params = {}
        if symbol:
            params["symbol"] = symbol
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"query_symbol_info failed: {resp.status} {text}")
                return await resp.json()

    def _get_symbol_meta(self, symbol: str) -> Dict:
        """Get cached symbol info"""
        if symbol not in self._symbol_meta:
            raise ValueError(f"Unknown symbol: {symbol}")
        return self._symbol_meta[symbol]

    # ----------------------------
    # Price
    # ----------------------------
    async def get_mark_price(self, symbol: str) -> float:
        """Get mark price (WS first, REST fallback)"""
        if self._prefer_ws:
            try:
                result = await self.get_mark_price_ws(symbol, timeout=0.5)
                await self._reset_fallback("get_mark_price")
                return float(result)
            except (TimeoutError, Exception) as e:
                await self._increment_fallback("get_mark_price")
                logger.debug(f"[standx] get_mark_price falling back to REST: {e}")
        price = await self.get_mark_price_rest(symbol)
        return float(price)

    async def get_mark_price_ws(self, symbol: str, timeout: float = 5.0) -> str:
        """Get mark price via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        # Subscribe if not already
        await self.ws_client.subscribe_price(symbol)

        # Wait for data
        ready = await self.ws_client.wait_price_ready(symbol, timeout=timeout)
        if not ready:
            raise TimeoutError(f"WS price not ready for {symbol}")

        price = self.ws_client.get_mark_price(symbol)
        if price is None:
            raise ValueError(f"No price data for {symbol}")

        return price

    async def get_mark_price_rest(self, symbol: str) -> str:
        """GET /api/query_symbol_price"""
        url = f"{STANDX_PERPS_BASE}/api/query_symbol_price"
        params = {"symbol": symbol}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"query_symbol_price failed: {resp.status} {text}")
                data = await resp.json()
                return data.get("mark_price", "0")

    # ----------------------------
    # Collateral / Balance
    # ----------------------------
    async def get_collateral(self) -> Dict[str, Any]:
        """
        Get collateral/balance (WS first, REST fallback)

        Returns:
            {
                "available_collateral": float,
                "total_collateral": float,
                "equity": float,
                "upnl": float,
            }
        """
        """
        # Try WS (cached data from initial load + WS updates)
        if self._prefer_ws and self.ws_client:
            ready = await self.ws_client.wait_collateral_ready(timeout=0.5)
            if ready:
                balance = self.ws_client.get_collateral()
                if balance:
                    return self._parse_collateral(balance)
        
        # REST fallback
        logger.debug("[StandXExchange] get_collateral: REST fallback")
        """
        return await self.get_collateral_rest()

    async def get_collateral_ws(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Get collateral via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        # Subscribe if not already
        await self.ws_client.subscribe_balance()

        # Wait for data
        ready = await self.ws_client.wait_collateral_ready(timeout=timeout)
        if not ready:
            raise TimeoutError("WS collateral not ready")

        balance = self.ws_client.get_collateral()
        if balance is None:
            raise ValueError("No balance data available")

        return self._parse_collateral(balance)

    async def get_collateral_rest(self, force: bool = False) -> Dict[str, Any]:
        """
        GET /api/query_balance with runtime cache

        Args:
            force: True면 캐시 무시하고 강제 fetch
        """
        now = time.monotonic()
        elapsed_ms = (now - self._collateral_cache_at) * 1000

        # 캐시가 유효하면 캐시 사용
        if not force and self._collateral_cache and elapsed_ms < self._collateral_cache_ttl_ms:
            return self._collateral_cache

        # REST API 호출
        url = f"{STANDX_PERPS_BASE}/api/query_balance"
        data = await self._auth_get(url)
        parsed = self._parse_collateral(data)

        # 캐시 업데이트
        self._collateral_cache = parsed
        self._collateral_cache_at = now

        return parsed

    def _parse_collateral(self, data: Dict) -> Dict[str, Any]:
        """Parse balance response"""
        return {
            "available_collateral": float(data.get("cross_available", 0)),
            "total_collateral": float(data.get("balance", 0)),
            "equity": float(data.get("equity", 0)),
            "upnl": float(data.get("upnl", 0)),
            "cross_balance": float(data.get("cross_balance", 0)),
            "isolated_balance": float(data.get("isolated_balance", 0)),
        }

    # ----------------------------
    # Position
    # ----------------------------
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get position (WS first, REST fallback)
        Uses cached data from initial REST load + WS updates
        """
        # Try WS first
        if self._prefer_ws:
            try:
                result = await self.get_position_ws(symbol, timeout=0.5)
                await self._reset_fallback("get_position")
                return result
            except TimeoutError:
                await self._increment_fallback("get_position")

        # REST fallback
        logger.debug("[StandXExchange] get_position: REST fallback")
        return await self.get_position_rest(symbol)

    async def get_position_ws(self, symbol: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """Get position via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        # Subscribe if not already
        await self.ws_client.subscribe_position()

        # Wait for data
        ready = await self.ws_client.wait_position_ready(timeout=timeout)
        if not ready:
            raise TimeoutError(f"WS position not ready for {symbol}")

        pos = self.ws_client.get_position(symbol)
        if pos is None:
            return None

        return self._parse_position(pos)

    async def get_position_rest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """GET /api/query_positions"""
        url = f"{STANDX_PERPS_BASE}/api/query_positions"
        params = {"symbol": symbol}
        positions = await self._auth_get(url, params=params)

        for pos in positions:
            if pos.get("symbol") == symbol and pos.get("status") == "open":
                return self._parse_position(pos)
        return None

    def _parse_position(self, pos: Dict) -> Dict[str, Any]:
        """Parse position response"""
        """StnadX는 qty가 0이여도 position 정보를 줌"""
        qty = Decimal(pos.get("qty", "0"))
        if qty == 0:
            return None
        side = "long" if qty > 0 else "short"
        size = str(abs(qty))

        return {
            "symbol": pos.get("symbol"),
            "side": side,
            "size": size,
            "entry_price": pos.get("entry_price", "0"),
            "unrealized_pnl": pos.get("upnl", "0"),
            "liquidation_price": pos.get("liq_price", "0"),
            "raw_data": pos,
        }

    # ----------------------------
    # Orders
    # ----------------------------
    def _build_order_payload(
        self,
        symbol: str,
        side: str,
        amount: float | str,
        price: Optional[float] = None,
        order_type: str = "market",
        is_reduce_only: bool = False,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build order payload with validation"""
        # Determine order type
        if price is not None:
            order_type = "limit"

        # Get symbol info for formatting
        info = self._get_symbol_meta(symbol)
        qty_decimals = info.get("qty_tick_decimals", 3)
        price_decimals = info.get("price_tick_decimals", 2)
        min_order_qty = float(info.get("min_order_qty", 0))
        max_order_qty = float(info.get("max_order_qty", float("inf")))

        # Validate order quantity
        if amount < min_order_qty:
            raise ValueError(f"Order quantity {amount} is below minimum {min_order_qty} for {symbol}")
        if amount > max_order_qty:
            raise ValueError(f"Order quantity {amount} exceeds maximum {max_order_qty} for {symbol}")

        # Format quantity
        qty_str = self._format_decimal(amount, qty_decimals)

        # Build payload
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.lower(),
            "order_type": order_type.lower(),
            "qty": qty_str,
            "reduce_only": is_reduce_only,
        }

        # Time in force
        if time_in_force:
            payload["time_in_force"] = time_in_force.lower()
        else:
            payload["time_in_force"] = "ioc" if order_type == "market" else "gtc"

        # Price for limit orders
        if order_type == "limit" and price is not None:
            payload["price"] = self._format_decimal(price, price_decimals)

        # Client order ID
        if client_order_id:
            payload["cl_ord_id"] = client_order_id

        return payload

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float | str,
        price: Optional[float] = None,
        order_type: str = "market",
        is_reduce_only: bool = False,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
        skip_rest: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Create order (WS first, REST fallback)

        Args:
            symbol: Trading pair (e.g., "BTC-USD")
            side: "buy" or "sell"
            amount: Order quantity
            price: Order price (required for limit orders)
            order_type: "market" or "limit"
            is_reduce_only: Only reduce position
            time_in_force: "gtc", "ioc", or "alo"
            client_order_id: Custom order ID
        """
        if isinstance(amount,str):
            amount = float(amount)

        # pair_trader에서 tif= 로 넘어올 수 있음 → time_in_force 매핑
        if time_in_force is None and "tif" in kwargs:
            time_in_force = kwargs["tif"]

        payload = self._build_order_payload(
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            order_type=order_type,
            is_reduce_only=is_reduce_only,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )

        # Try WS first
        if self._prefer_order_ws and self.order_ws_client:
            try:
                result = await self.order_ws_client.create_order(payload)
                await self._reset_fallback("create_order", is_order_ws=True)
                return result
            except Exception as e:
                await self._increment_fallback("create_order", is_order_ws=True)
                logger.debug(f"[StandXExchange] create_order WS failed, falling back to REST: {e}")
                if skip_rest:
                    logger.debug('rest api skipped. no order at this moment')
                    return None

        if not skip_rest:
            # REST fallback
            return await self._post_signed("/api/new_order", payload)

    async def cancel_orders(self, symbol: str, open_orders: Optional[List] = None) -> Dict[str, Any]:
        """
        Cancel all open orders for symbol

        POST /api/cancel_orders
        """
        # First get open orders
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)
            
        if not open_orders:
            return []

        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]

        # Extract order IDs
        order_ids = [o.get("id") for o in open_orders if o.get("id")]

        if not order_ids:
            return {"canceled": 0}

        payload = {"order_id_list": order_ids}
        result = await self._post_signed("/api/cancel_orders", payload)

        return {"canceled": len(order_ids), "result": result}

    async def cancel_order(self, order_id: Optional[int] = None, client_order_id: Optional[str] = None, *, skip_rest: bool = False) -> Dict[str, Any]:
        """
        Cancel single order (WS first, REST fallback)

        POST /api/cancel_order
        """
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id required")

        # Try WS first
        if self._prefer_order_ws and self.order_ws_client:
            try:
                result = await self.order_ws_client.cancel_order(
                    order_id=order_id,
                    cl_ord_id=client_order_id,
                )
                await self._reset_fallback("cancel_orders", is_order_ws=True)
                return result
            except Exception as e:
                await self._increment_fallback("cancel_orders", is_order_ws=True)
                logger.debug(f"[StandXExchange] cancel_order WS failed, falling back to REST: {e}")
                if skip_rest:
                    logger.debug('rest api skipped. no order canceled at this moment')
                    return None

        if not skip_rest:
            # REST fallback
            payload: Dict[str, Any] = {}
            if order_id:
                payload["order_id"] = order_id
            if client_order_id:
                payload["cl_ord_id"] = client_order_id

            return await self._post_signed("/api/cancel_order", payload)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get open orders (WS first, REST fallback)
        Uses cached data from initial REST load + WS updates
        """
        # Try WS (cached data from initial load + WS updates)
        if self._prefer_ws and self.ws_client:
            ready = await self.ws_client.wait_orders_ready(timeout=0.5)
            if ready:
                orders = self.ws_client.get_open_orders(symbol)
                await self._reset_fallback("get_open_orders")
                return [self._parse_order(o) for o in orders]

        # REST fallback
        await self._increment_fallback("get_open_orders")
        logger.debug("[StandXExchange] get_open_orders: REST fallback")
        url = f"{STANDX_PERPS_BASE}/api/query_open_orders"
        params = {"symbol": symbol} if symbol else {}
        data = await self._auth_get(url, params=params)
        orders = data.get("result", [])
        return [self._parse_order(o) for o in orders]

    def _parse_order(self, order: Dict) -> Dict[str, Any]:
        """Parse order response"""
        return {
            "id": order.get("id"),
            "cl_ord_id": order.get("cl_ord_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "order_type": order.get("order_type"),
            "price": order.get("price"),
            "size": order.get("qty"),
            "filled_size": order.get("fill_qty", "0"),
            "status": order.get("status"),
            "reduce_only": order.get("reduce_only", False),
            "time_in_force": order.get("time_in_force"),
            "created_at": order.get("created_at"),
        }

    # ----------------------------
    # Close Position
    # ----------------------------
    async def close_position(self, symbol: str, position: Optional[Dict] = None, *, is_reduce_only: bool = True) -> Optional[Dict]:
        """Close position with market order"""
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)

    # ----------------------------
    # Leverage / Margin
    # ----------------------------
    async def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """POST /api/change_leverage"""
        payload = {
            "symbol": symbol,
            "leverage": leverage,
        }
        return await self._post_signed("/api/change_leverage", payload)

    async def update_leverage(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Update leverage and/or margin mode for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTC-USD")
            leverage: If None, only margin_mode is updated (if provided).
            margin_mode: If None, only leverage is updated (if provided).

        Note: At least one of leverage or margin_mode should be provided.
        """
        if leverage is None and margin_mode is None:
            return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

        target_leverage = leverage
        target_margin_mode = margin_mode.lower() if margin_mode else None

        # Execute requests based on what is provided
        try:
            tasks = []
            results = [None, None]  # [leverage_result, margin_result]

            if target_leverage is not None:
                tasks.append(("leverage", self.change_leverage(symbol, target_leverage)))
            if target_margin_mode is not None:
                tasks.append(("margin", self.change_margin_mode(symbol, target_margin_mode)))

            if tasks:
                task_results = await asyncio.gather(*[t[1] for t in tasks])
                for i, (task_type, _) in enumerate(tasks):
                    if task_type == "leverage":
                        results[0] = task_results[i]
                    else:
                        results[1] = task_results[i]

            # Update cache with what was changed
            cached = self._leverage_cache.get(symbol, {})
            if target_leverage is not None:
                cached["leverage"] = target_leverage
            if target_margin_mode is not None:
                cached["margin_mode"] = target_margin_mode
            self._leverage_cache[symbol] = cached

            return {
                "status": "ok",
                "leverage": target_leverage,
                "margin_mode": target_margin_mode,
                "results": results,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def change_margin_mode(self, symbol: str, margin_mode: str) -> Dict[str, Any]:
        """
        POST /api/change_margin_mode

        Args:
            margin_mode: "cross" or "isolated"
        """
        payload = {
            "symbol": symbol,
            "margin_mode": margin_mode.lower(),
        }
        return await self._post_signed("/api/change_margin_mode", payload)

    async def get_leverage_info(self, symbol: str) -> Dict[str, Any]:
        """GET /api/query_position_config"""
        url = f"{STANDX_PERPS_BASE}/api/query_position_config"
        params = {"symbol": symbol}
        try:
            data = await self._auth_get(url, params=params)
            # data: {'leverage': '40', 'margin_mode': 'cross', 'symbol': 'BTC-USD'}
            info = self._get_symbol_meta(symbol)
            max_lev = int(info.get("max_leverage", 20))
            return {
                "symbol": data.get("symbol", symbol),
                "leverage": int(data.get("leverage", 1)),
                "margin_mode": data.get("margin_mode", "cross"),
                "status": "ok",
                "max_leverage": max_lev,
                "available_margin_modes": ["cross", "isolated"],
            }
        except Exception as e:
            return {
                "symbol": symbol,
                "leverage": None,
                "margin_mode": None,
                "status": "error",
                "max_leverage": None,
                "available_margin_modes": ["cross", "isolated"],
                "message": str(e),
            }

    # ----------------------------
    # Market Data
    # ----------------------------
    async def get_orderbook(self, symbol: str, timeout: float = 5.0) -> Dict[str, Any]:
        """
        Get orderbook via WS
        """
        return await self.get_orderbook_ws(symbol, timeout=timeout)

    async def get_orderbook_ws(self, symbol: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Get orderbook via WebSocket (no REST fallback)"""
        if not self.ws_client:
            await self._create_ws_client()

        # Subscribe if not already
        await self.ws_client.subscribe_orderbook(symbol)

        # Wait for data
        ready = await self.ws_client.wait_orderbook_ready(symbol, timeout=timeout)
        if not ready:
            raise TimeoutError(f"WS orderbook not ready for {symbol}")

        orderbook = self.ws_client.get_orderbook(symbol)
        if orderbook is None:
            raise ValueError(f"No orderbook data for {symbol}")

        return orderbook

    async def get_orderbook_rest(self, symbol: str) -> Dict[str, Any]:
        """GET /api/query_depth_book"""
        url = f"{STANDX_PERPS_BASE}/api/query_depth_book"
        params = {"symbol": symbol}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"query_depth_book failed: {resp.status} {text}")
                data = await resp.json()
                return self._parse_orderbook(data)

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

    async def unsubscribe_orderbook(self, symbol: str):
        #return # 오더북 하나니깐 그냥 놔둠
        """Unsubscribe from orderbook WebSocket channel"""
        if self.ws_client:
            await self.ws_client.unsubscribe_orderbook(symbol)

    async def get_recent_trades(self, symbol: str) -> List[Dict]:
        """GET /api/query_recent_trades"""
        url = f"{STANDX_PERPS_BASE}/api/query_recent_trades"
        params = {"symbol": symbol}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"query_recent_trades failed: {resp.status} {text}")
                return await resp.json()

    async def get_symbol_market(self, symbol: str) -> Dict[str, Any]:
        """GET /api/query_symbol_market"""
        url = f"{STANDX_PERPS_BASE}/api/query_symbol_market"
        params = {"symbol": symbol}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"query_symbol_market failed: {resp.status} {text}")
                return await resp.json()

    # ----------------------------
    # User Trades
    # ----------------------------
    async def get_trades(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """GET /api/query_trades"""
        url = f"{STANDX_PERPS_BASE}/api/query_trades"
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        data = await self._auth_get(url, params=params)
        return data.get("result", [])

    # ----------------------------
    # Internal Helpers
    # ----------------------------
    async def _post_signed(self, endpoint: str, payload: Dict) -> Dict[str, Any]:
        """POST request with body signature and auto re-auth on 401/403"""
        url = f"{STANDX_PERPS_BASE}{endpoint}"
        payload_str = json.dumps(payload, separators=(",", ":"))

        # Get auth and signature headers
        headers = {
            "Content-Type": "application/json",
            **self._auth.get_auth_headers(),
            **self._auth.sign_request(payload_str),
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._http_timeout)) as session:
            async with session.post(url, data=payload_str, headers=headers) as resp:
                text = await resp.text()

                # Re-auth on 401/403
                if resp.status in (401, 403):
                    if await self._reauth():
                        # Re-sign with new auth
                        headers = {
                            "Content-Type": "application/json",
                            **self._auth.get_auth_headers(),
                            **self._auth.sign_request(payload_str),
                        }
                        async with session.post(url, data=payload_str, headers=headers) as retry_resp:
                            retry_text = await retry_resp.text()
                            if retry_resp.status != 200:
                                raise RuntimeError(f"{endpoint} failed: {retry_resp.status} {retry_text}")
                            try:
                                return json.loads(retry_text)
                            except json.JSONDecodeError:
                                return {"raw": retry_text}

                if resp.status != 200:
                    raise RuntimeError(f"{endpoint} failed: {resp.status} {text}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}

    @staticmethod
    def _format_decimal(value: float, decimals: int) -> str:
        """Format number with specific decimal places"""
        d = Decimal(str(value))
        quantizer = Decimal(10) ** -decimals
        d = d.quantize(quantizer, rounding=ROUND_DOWN)
        return format(d, "f").rstrip("0").rstrip(".")

    def get_perp_quote(self, symbol: str, *, is_basic_coll: bool = False) -> str:
        """Get quote currency for perp"""
        return "DUSD"
