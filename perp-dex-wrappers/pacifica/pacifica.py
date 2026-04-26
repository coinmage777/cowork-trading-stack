from mpdex.base import MultiPerpDexMixin, MultiPerpDex
from mpdex.utils.common_pacifica import sign_message
import logging
import time
import uuid
import requests
from solders.keypair import Keypair
import aiohttp
from aiohttp import TCPConnector
from typing import Optional, Dict, Any, List
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, getcontext
import json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.pacifica.fi/api/v1"
WS_URL = "wss://ws.pacifica.fi/ws"

getcontext().prec = 36  # [ADDED] 충분한 정밀도 확보

def _get_signature_header_and_url(req_type:str):
    if req_type == "create_market_order": # market order
        req_url = f"{BASE_URL}/orders/create_market"

    elif req_type == "create_order": # limit order
        req_url = f"{BASE_URL}/orders/create"

    elif req_type == "cancel_order":
        req_url = f"{BASE_URL}/orders/cancel"

    else:
        raise Exception(f"no such request type {req_type}")

    return {
        "timestamp": int(time.time() * 1_000),
        "expiry_window": 5_000,
        "type": f"{req_type}",
    }, req_url

class PacificaExchange(MultiPerpDexMixin, MultiPerpDex):
    # no use of private key, but use agent wallets instead (api)
    def __init__(self, public_key, agent_public_key, agent_private_key):
        super().__init__()
        if not (public_key and agent_public_key and agent_private_key):
            raise ValueError("Pacifica required, pub key, agent pub key, and agent private key")
        self.public_key = public_key                # required
        self.agent_public_key = agent_public_key    # required
        self.agent_private_key = agent_private_key  # required
        self.agent_keypair = Keypair.from_base58_string(agent_private_key)
        self._http: Optional[aiohttp.ClientSession] = None

        # { "BTC": {"tick_size": "1", "lot_size": "0.00001", "max_leverage": 50, ...}, ... }
        self._symbol_meta: Dict[str, Dict[str, Any]] = {}
        self._symbol_list: List[str] = []
        self._initialized: bool = False
        self._leverage_updated: Dict[str, bool] = {}  # symbol -> updated flag

        # 가격 런타임 캐시: { "BTC": {"mark": Decimal, "mid": Decimal|None, "oracle": Decimal|None, "ts": int} }
        self._price_cache: Dict[str, Dict[str, Any]] = {}

        self.has_margin_mode = True  # isolated / cross 지원
        
        # WebSocket
        self.ws_client = None
        # WS support flags
        self.ws_supported = {
            "get_mark_price": True,
            "get_position": True,
            "get_open_orders": True,
            "get_collateral": True,
            "get_orderbook": True,
            "create_order": True,
            "cancel_orders": True,
            "update_leverage": False,
        }


    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                connector=TCPConnector(
                    force_close=True,             # 매 요청 후 소켓 닫기 → 종료 시 잔여 소켓 최소화
                    enable_cleanup_closed=True,   # 종료 중인 SSL 소켓 정리 보조 (로그 억제)
                )
            )
        return self._http
    
    async def close(self, force_close: bool = True):
        """
        Close connection.

        Args:
            force_close: True (default) = 연결 종료, False = 풀에 유지
        """
        if self._http and not self._http.closed:
            await self._http.close()
        if self.ws_client:
            from .pacifica_ws_client import PACIFICA_WS_POOL
            await PACIFICA_WS_POOL.release(self.public_key, force_close=force_close)
            self.ws_client = None
    
    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USDC'
    
    async def init(self) -> "PacificaExchange":
        """
        GET /info → 심볼 목록과 tick_size/lot_size 등을 런타임 캐시에 저장
        """
        if self._initialized:
            return self

        url = f"{BASE_URL}/info"
        s = self._session()
        async with s.get(url) as r:
            r.raise_for_status()
            data = await r.json()

        # 기대 형태: {"success": true, "data": [ {symbol, tick_size, lot_size, ...}, ... ]}
        items = data.get("data") or []
        meta: Dict[str, Dict[str, Any]] = {}
        symbols: List[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").upper()
            if not sym:
                continue
            # 필요한 값만 보관(문자열로 유지, 계산 시 Decimal로 변환)
            meta[sym] = {
                "tick_size": str(it.get("tick_size") or "1"),
                "lot_size": str(it.get("lot_size") or "1"),
                "min_tick": str(it.get("min_tick") or "0"),
                "max_tick": str(it.get("max_tick") or "0"),
                "min_order_size": str(it.get("min_order_size") or "0"),
                "max_order_size": str(it.get("max_order_size") or "0"),
                "max_leverage": int(it.get("max_leverage") or 1),
                "isolated_only": bool(it.get("isolated_only", False)),
            }
            symbols.append(sym)

        self._symbol_meta = meta
        self._symbol_list = sorted(set(symbols))
        self._initialized = True

        # Update available symbols
        self.update_available_symbols()

        # Initialize WebSocket
        await self._create_ws_client()

        return self

    def update_available_symbols(self):
        """Update available_symbols dict from _symbol_list"""
        self.available_symbols['perp'] = []
        #self.available_symbols['spot'] = []
        for sym in self._symbol_list:
            quote = self.get_perp_quote(sym)
            composite_symbol = f"{sym}-{quote}"
            self.available_symbols['perp'].append(composite_symbol)

    async def _create_ws_client(self):
        """Create and initialize WebSocket client from pool"""
        if self.ws_client is not None:
            return

        from .pacifica_ws_client import PACIFICA_WS_POOL

        self.ws_client = await PACIFICA_WS_POOL.acquire(
            public_key=self.public_key,
            agent_public_key=self.agent_public_key,
            agent_keypair=self.agent_keypair,
            subscribe_private=True,
        )
    
    async def initialize_if_needed(self):  # [ADDED]
        if not self._initialized:
            await self.initialize()

    # ---------------------------
    # 수치 보정 유틸
    # ---------------------------
    def _dec(self, x) -> Decimal:  # [ADDED]
        return x if isinstance(x, Decimal) else Decimal(str(x))

    def _format_with_step(self, value: Decimal, step: Decimal) -> str:  # [ADDED]
        """
        step의 소수자릿수에 맞춰 trailing zeros 포함 문자열로 변환
        """
        q = value.quantize(step)  # 소수자릿수 강제
        return format(q, "f")     # '1.2300' 유지

    def _get_symbol_meta(self, symbol: str) -> Dict[str, Any]:  # [ADDED]
        sym = str(symbol).upper()
        meta = self._symbol_meta.get(sym)
        if not meta:
            # 초기화 누락/미지원 심볼 → 안전 기본값
            return {
                "tick_size": "1",
                "lot_size": "1",
                "min_tick": "0",
                "max_tick": "0",
                "min_order_size": "0",
                "max_order_size": "0",
            }
        return meta

    def _adjust_price_tick(self, symbol: str, price, *, rounding=ROUND_HALF_UP) -> str:  # [ADDED]
        """
        tick_size에 맞춰 가격을 반올림하여 문자열로 반환.
        - 기본 반올림: HALF_UP (일반적인 가격 반올림)
        - 필요 시 rounding=ROUND_DOWN/ROUND_UP 등으로 조정 가능
        """
        meta = self._get_symbol_meta(symbol)
        step = self._dec(meta["tick_size"])
        p = self._dec(price)
        # step 배수로 반올림
        units = (p / step).to_integral_value(rounding=rounding)
        adjusted = (units * step).quantize(step)
        # min/max tick 범위가 유효하면 보정
        try:
            min_tick = self._dec(meta.get("min_tick", "0"))
            max_tick = self._dec(meta.get("max_tick", "0"))
            if max_tick > 0:
                if adjusted < min_tick:
                    adjusted = min_tick
                if adjusted > max_tick:
                    adjusted = max_tick
        except Exception:
            pass
        return self._format_with_step(adjusted, step)

    def _adjust_amount_lot(self, symbol: str, amount, *, rounding=ROUND_DOWN) -> str:  # [ADDED]
        """
        lot_size 배수로 수량을 반올림하여 문자열로 반환.
        - 기본은 DOWN(절삭): 과다 수량 전송 방지 목적
        """
        meta = self._get_symbol_meta(symbol)
        step = self._dec(meta["lot_size"])
        a = self._dec(amount)
        if step <= 0:
            return str(amount)
        units = (a / step).to_integral_value(rounding=rounding)
        adjusted = (units * step).quantize(step)
        return self._format_with_step(adjusted, step)

    # ---------------------------
    # Leverage
    # ---------------------------
    async def update_leverage(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Update leverage and/or margin mode for symbol (REST-only).

        Args:
            leverage: If None, only margin_mode is updated (if provided).
            margin_mode: If None, only leverage is updated (if provided).

        Note: At least one of leverage or margin_mode should be provided.
        """
        symbol = symbol.upper()

        if leverage is None and margin_mode is None:
            return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

        # update_leverage is REST-only (not supported via WS)
        result = await self.update_leverage_rest(symbol, leverage, margin_mode)
        results = result.get("results", [])

        # Check success based on what was requested
        success = True
        if leverage is not None and results and not results[0].get("success"):
            success = False
        if margin_mode is not None and results and len(results) > 1 and not results[1].get("success"):
            success = False

        if success:
            return {"status": "ok", "leverage": leverage, "margin_mode": margin_mode, "result": results}
        return {"status": "error", "result": results}

    async def get_leverage_info(self, symbol: str) -> Dict[str, Any]:
        """
        Get leverage info for symbol via /account/settings API.
        Note: If no custom settings, returns default (cross margin, max leverage).
        """
        symbol = symbol.upper()
        meta = self._get_symbol_meta(symbol)
        max_lev = meta.get("max_leverage", 1)

        url = f"{BASE_URL}/account/settings"
        s = self._session()
        params = {"account": self.public_key}

        try:
            async with s.get(url, params=params) as r:
                r.raise_for_status()
                data = await r.json()

            settings_list = data.get("data") or []
            for setting in settings_list:
                if setting.get("symbol") == symbol:
                    return {
                        "symbol": symbol,
                        "leverage": setting.get("leverage", max_lev),
                        "margin_mode": "isolated" if setting.get("isolated") else "cross",
                        "status": "ok",
                        "max_leverage": max_lev,
                        "available_margin_modes": ["cross", "isolated"],
                    }

            # Not found = default settings (cross margin, max leverage)
            return {
                "symbol": symbol,
                "leverage": max_lev,
                "margin_mode": "cross",
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
                "available_margin_modes": ["cross", "isolated"],
                "message": str(e),
            }

    async def update_leverage_rest(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Update leverage and/or margin mode via REST API.

        Args:
            leverage: If None, leverage is not updated.
            margin_mode: If None, margin mode is not updated.
        """
        import asyncio
        timestamp = int(time.time() * 1000)
        expiry_window = 5000

        # Common request fields
        common = {
            "account": self.public_key,
            "agent_wallet": self.agent_public_key,
            "timestamp": timestamp,
            "expiry_window": expiry_window,
        }

        async def post_json(url: str, payload: dict):
            s = self._session()
            async with s.post(url, json=payload, headers={"Content-Type": "application/json"}) as r:
                return await r.json()

        tasks = []
        results = [None, None]  # [leverage_result, margin_result]

        # Build leverage request if leverage is provided
        if leverage is not None:
            lev_sig_header = {"type": "update_leverage", "timestamp": timestamp, "expiry_window": expiry_window}
            lev_sig_payload = {"symbol": symbol, "leverage": leverage}
            _, lev_signature = sign_message(lev_sig_header, lev_sig_payload, self.agent_keypair)
            lev_request = {**common, **lev_sig_payload, "signature": lev_signature}
            tasks.append(("leverage", post_json(f"{BASE_URL}/account/leverage", lev_request)))

        # Build margin mode request if margin_mode is provided
        if margin_mode is not None:
            is_isolated = margin_mode.lower() == "isolated"
            margin_sig_header = {"type": "update_margin_mode", "timestamp": timestamp, "expiry_window": expiry_window}
            margin_sig_payload = {"symbol": symbol, "is_isolated": is_isolated}
            _, margin_signature = sign_message(margin_sig_header, margin_sig_payload, self.agent_keypair)
            margin_request = {**common, **margin_sig_payload, "signature": margin_signature}
            tasks.append(("margin", post_json(f"{BASE_URL}/account/margin", margin_request)))

        # Execute requests concurrently
        if tasks:
            task_results = await asyncio.gather(*[t[1] for t in tasks])
            for i, (task_type, _) in enumerate(tasks):
                if task_type == "leverage":
                    results[0] = task_results[i]
                else:
                    results[1] = task_results[i]

        return {"results": results}

    async def create_order(self, symbol, side, amount, price=None, order_type='market', *, is_reduce_only=False, slippage = "0.1"):
        """
        Create order (WS preferred, REST fallback)
        """
        symbol = symbol.upper()

        amount = self._adjust_amount_lot(symbol, amount, rounding=ROUND_DOWN)
        side_pacifica = "bid" if side.lower() == "buy" else "ask"

        # price가 있냐 없냐로 사실상 정함
        if not price:
            order_type = "market"
            price_adjusted = None
        else:
            order_type = "limit"
            price_adjusted = self._adjust_price_tick(symbol, price, rounding=ROUND_HALF_UP)

        if self.ws_client:
            try:
                return await self.create_order_ws(
                    symbol=symbol,
                    side=side_pacifica,
                    amount=amount,
                    price=price_adjusted,
                    is_reduce_only=is_reduce_only,
                    slippage=slippage,
                )
            except Exception as e:
                logger.debug(f"[pacifica] create_order WS failed, falling back to REST: {e}")

        return await self.create_order_rest(
            symbol=symbol,
            side=side_pacifica,
            amount=amount,
            price=price_adjusted,
            is_reduce_only=is_reduce_only,
            slippage=slippage,
        )

    async def create_order_ws(self, symbol, side, amount, price=None, *, is_reduce_only=False, slippage="0.1"):
        """Create order via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        result = await self.ws_client.create_order_ws(
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            reduce_only=is_reduce_only,
            slippage_percent=str(slippage),
        )

        # Parse response
        code = result.get("code")
        if code == 200:
            data = result.get("data", {})
            return data.get("i")  # order_id
        else:
            raise Exception(f"WS order failed: {result}")

    async def create_order_rest(self, symbol, side, amount, price=None, *, is_reduce_only=False, slippage="0.1"):
        """Create order via REST"""
        # common payload
        signature_payload = {
                "symbol": symbol,
                "reduce_only": False,
                "amount": amount,
                "side": side,
                "client_order_id": str(uuid.uuid4()),
        }
        if price is None:
            # market order
            signature_payload["reduce_only"] = is_reduce_only
            signature_payload["slippage_percent"] = str(slippage)
            signature_header, req_url = _get_signature_header_and_url("create_market_order")
        else:
            # limit order
            signature_payload["price"] = price
            signature_payload["tif"] = "GTC"
            signature_header, req_url = _get_signature_header_and_url("create_order")

        _, signature = sign_message(
            signature_header, signature_payload, self.agent_keypair
        )
        request_header = {
            "account": self.public_key,
            "agent_wallet": self.agent_public_key,
            "signature": signature,
            "timestamp": signature_header["timestamp"],
            "expiry_window": signature_header["expiry_window"],
        }
        headers = {"Content-Type": "application/json"}

        request = {
            **request_header,
            **signature_payload,
        }

        s = self._session()
        async with s.post(req_url, json=request, headers=headers) as r:
            try:
                data = await r.json()
            except aiohttp.ContentTypeError:
                data = await r.text()

        try:
            return (data or {}).get("data", {}).get("order_id")
        except Exception:
            return None

    async def get_position(self, symbol):
        """
        Get position (WS first, REST fallback)
        """
        symbol = symbol.upper()
        
        if self.ws_client:
            try:
                return await self.get_position_ws(symbol)
            except Exception as e:
                logger.debug(f"[pacifica] get_position WS failed, falling back to REST: {e}")

        return await self.get_position_rest(symbol)

    async def get_position_ws(self, symbol: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """Get position via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        await self.ws_client.wait_positions_ready(timeout=timeout)
        pos = self.ws_client.get_position(symbol.upper())
        if pos is None:
            return None

        return self._parse_position_ws(pos)

    def _parse_position_ws(self, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse WS position to standard format"""
        if not pos:
            return None
        amount = pos.get("amount", "0")
        try:
            if float(amount) == 0:
                return None
        except (ValueError, TypeError):
            return None

        side_raw = pos.get("side", "")
        side = "long" if side_raw == "bid" else "short"
        is_isolated = pos.get("is_isolated", False)

        return {
            "symbol": pos.get("symbol"),
            "side": side,
            "size": amount,
            "entry_price": pos.get("entry_price"),
            "unrealized_pnl": None,
            "liquidation_price": pos.get("liquidation_price"),
            "raw_data": pos,
        }

    async def get_position_rest(self, symbol):
        """
        GET /positions (REST)
        """
        url = f"{BASE_URL}/positions"

        s = self._session()
        params = {"account": self.public_key}

        async with s.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()

        data = data.get('data', {})
        for pos in data:
            if pos.get("symbol") == symbol:
                return {
                    "symbol": symbol,
                    "side": "long" if pos.get("side") == "bid" else "short",
                    "size": pos.get("amount"),
                    "entry_price": pos.get("entry_price"),
                    "unrealized_pnl": None,
                    "liquidation_price": pos.get("liquidation_price"),
                    "raw_data": pos,
                }
        return None
    
    async def get_collateral(self):
        """
        Get collateral (WS first, REST fallback)
        """
        if self.ws_client:
            try:
                return await self.get_collateral_ws()
            except Exception as e:
                logger.debug(f"[pacifica] get_collateral WS failed, falling back to REST: {e}")
        return await self.get_collateral_rest()

    async def get_collateral_ws(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Get collateral via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        ready = await self.ws_client.wait_account_info_ready(timeout=timeout)
        if not ready:
            raise TimeoutError("WS account_info not ready")

        return self.ws_client.get_collateral()

    async def get_collateral_rest(self):
        """
        GET /account (REST)
        """
        url = f"{BASE_URL}/account"
        s = self._session()
        params = {"account":self.public_key}
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()

        data = data.get('data',{})

        try:
            return {
                "total_collateral": data.get("account_equity"),
                "available_collateral": data.get("available_to_spend"),
            }
        except:
            return {
                "total_collateral": None,
                "available_collateral": None,
            }
    
    async def get_open_orders(self, symbol):
        """
        Get open orders (WS first, REST fallback)
        """
        symbol = symbol.upper()
        if self.ws_client:
            try:
                return await self.get_open_orders_ws(symbol)
            except Exception as e:
                logger.debug(f"[pacifica] get_open_orders WS failed, falling back to REST: {e}")
        return await self.get_open_orders_rest(symbol)

    async def get_open_orders_ws(self, symbol: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Get open orders via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        await self.ws_client.wait_orders_ready(timeout=timeout)
        orders = self.ws_client.get_open_orders(symbol.upper())

        # Parse to standard format
        results = []
        for order in orders:
            side_raw = order.get("side", "")
            side = "buy" if side_raw == "bid" else "sell"
            results.append({
                "id": order.get("order_id"),
                "symbol": order.get("symbol"),
                "side": side,
                "price": order.get("price"),
                "size": order.get("amount"),
                "filled_size": order.get("filled_amount"),
                "order_type": order.get("order_type"),
                "reduce_only": order.get("reduce_only", False),
            })
        return results

    async def get_open_orders_rest(self, symbol):
        """
        GET /orders (REST)
        """
        url = f"{BASE_URL}/orders"

        s = self._session()
        params = {"account":self.public_key}

        async with s.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()

        data = data.get('data',{})
        results = []
        for pos in data:
            if pos.get("symbol") == symbol:
                results.append({
                    "id": pos.get("order_id"),
                    "symbol": symbol,
                    "side": "buy" if pos.get("side")=="bid" else "sell",
                    "price": pos.get("price"),
                    "size":pos.get("initial_amount"),
                    "filled_size":pos.get("filled_amount"),
                    "order_type":pos.get("order_type")
                })
        return results

    async def cancel_orders(self, symbol, open_orders = None):
        """
        Cancel orders (WS preferred, REST fallback)
        """
        symbol = symbol.upper()
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []
        
        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]

        if self.ws_client:
            try:
                return await self.cancel_orders_ws(symbol, open_orders)
            except Exception as e:
                logger.debug(f"[pacifica] cancel_orders WS failed, falling back to REST: {e}")

        return await self.cancel_orders_rest(symbol, open_orders)

    async def cancel_orders_ws(self, symbol, open_orders=None):
        """
        Cancel orders via WebSocket.
        If open_orders is None, cancel all orders for symbol.
        If open_orders is provided, cancel each order individually.
        """
        if not self.ws_client:
            await self._create_ws_client()

        # If specific orders provided, cancel individually
        if open_orders is not None:
            results = []
            for order in open_orders:
                order_id = order.get("id")
                if not order_id:
                    continue
                try:
                    result = await self.ws_client.cancel_order_ws(
                        symbol=symbol,
                        order_id=int(order_id),
                    )
                    code = result.get("code")
                    if code == 200:
                        results.append({"status": "OK", "order_id": order_id})
                    else:
                        results.append({"status": "error", "order_id": order_id, "result": result})
                except Exception as e:
                    results.append({"status": "error", "order_id": order_id, "error": str(e)})
            return results

        # No specific orders - cancel all
        result = await self.ws_client.cancel_all_orders_ws(symbol=symbol)

        code = result.get("code")
        if code == 200:
            data = result.get("data", {})
            cancelled_count = data.get("cancelled_count", 0)
            return [{"status": "OK", "cancelled_count": cancelled_count}]
        else:
            raise Exception(f"WS cancel_all failed: {result}")

    async def cancel_orders_rest(self, symbol, open_orders=None):
        """Cancel orders via REST (one by one)"""
        
        results = []
        for order in open_orders:
            order_id = order["id"]
            signature_payload = {
                "symbol": symbol,
                "order_id": order_id,
            }
            signature_header, req_url = _get_signature_header_and_url("cancel_order")
            _, signature = sign_message(
                signature_header, signature_payload, self.agent_keypair
            )
            request_header = {
                "account": self.public_key,
                "agent_wallet": self.agent_public_key,
                "signature": signature,
                "timestamp": signature_header["timestamp"],
                "expiry_window": signature_header["expiry_window"],
            }
            headers = {"Content-Type": "application/json"}

            request = {
                **request_header,
                **signature_payload,
            }

            s = self._session()
            async with s.post(req_url, json=request, headers=headers) as r:
                try:
                    data = await r.json()
                except aiohttp.ContentTypeError:
                    data = await r.text()
            try:
                results.append({
                    "id": order_id,
                    "status": data.get("success")
                })
            except Exception as e:
                results.append({
                    "id": order_id,
                    "status": "FAILED",
                    "message": str(e)
                })
        return results
            
    async def refresh_prices(self) -> Dict[str, float]:
        """
        GET /info/prices → 런타임 캐시(self._price_cache) 갱신 후 {symbol: mark(float)} 반환
        """
        url = f"{BASE_URL}/info/prices"
        s = self._session()
        async with s.get(url) as r:
            r.raise_for_status()
            data = await r.json()

        items = data.get("data") or []
        ts_now = int(time.time() * 1000)
        out: Dict[str, float] = {}

        for it in items:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").upper()
            if not sym:
                continue

            # 문자열 숫자 → Decimal → float 변환
            def _f(k):
                v = it.get(k)
                try:
                    return float(Decimal(str(v))) if v is not None else None
                except Exception:
                    return None

            mark = _f("mark")
            mid = _f("mid")
            oracle = _f("oracle")
            ts = int(it.get("timestamp") or ts_now)

            self._price_cache[sym] = {
                "mark": mark,
                "mid": mid,
                "oracle": oracle,
                "ts": ts,
            }
            if mark is not None:
                out[sym] = mark

        return out

    async def get_mark_price(self, symbol: str, *, force_refresh: bool = True, fallback: str = "mark") -> Optional[float]:
        """
        Get mark price (WS first, REST fallback)
        """
        symbol = str(symbol).upper()

        if self.ws_client:
            try:
                return await self.get_mark_price_ws(symbol)
            except Exception as e:
                logger.debug(f"[pacifica] get_mark_price WS failed, falling back to REST: {e}")
        
        return await self.get_mark_price_rest(symbol, force_refresh=force_refresh, fallback=fallback)

    async def get_mark_price_ws(self, symbol: str, timeout: float = 5.0) -> Optional[float]:
        """Get mark price via WebSocket"""
        if not self.ws_client:
            await self._create_ws_client()

        ready = await self.ws_client.wait_prices_ready(timeout=timeout)
        if not ready:
            raise TimeoutError("WS prices not ready")

        return self.ws_client.get_mark_price(symbol.upper())

    async def get_mark_price_rest(self, symbol: str, *, force_refresh: bool = True, fallback: str = "mark") -> Optional[float]:
        """
        Get mark price via REST.
        - 기본: 원격 갱신(force_refresh=True) 후 캐시에서 반환
        - force_refresh=False이면 캐시 우선, 없으면 원격 갱신 시도
        - fallback: 캐시에 mark가 없으면 mid→oracle 순으로 대체할 때 사용하는 키 우선순위
        """
        symbol = str(symbol).upper()

        if force_refresh:
            await self.refresh_prices()

        entry = self._price_cache.get(symbol)
        if entry:
            # mark가 없을 수 있어 보강
            val = entry.get("mark")
            if val is None:
                # fallback 체인 구성
                order = ["mark", "mid", "oracle"]
                # 사용자가 지정한 fallback을 맨 앞으로
                if fallback in order:
                    order.remove(fallback)
                    order.insert(0, fallback)
                for k in order:
                    v = entry.get(k)
                    if isinstance(v, (int, float)):
                        return float(v)
            return float(val) if isinstance(val, (int, float)) else None

        # 캐시에 없으면 한 번 더 갱신 시도
        prices = await self.refresh_prices()
        return prices.get(symbol)

    # ----------------------------
    # Orderbook
    # ----------------------------
    async def get_orderbook(self, symbol: str, agg_level: int = 1, timeout: float = 5.0) -> Dict[str, Any]:
        """
        Get orderbook via WebSocket.

        Args:
            symbol: Trading symbol
            agg_level: Aggregation level (1, 2, 5, 10, 100, 1000)
            timeout: Timeout for waiting data
        """
        if not self.ws_client:
            await self._create_ws_client()

        symbol = symbol.upper()
        await self.ws_client.subscribe_orderbook(symbol, agg_level=agg_level)
        ready = await self.ws_client.wait_orderbook_ready(symbol, timeout=timeout)
        if not ready:
            raise TimeoutError(f"WS orderbook not ready for {symbol}")

        return self.ws_client.get_orderbook(symbol)

    async def unsubscribe_orderbook(self, symbol: str):
        """Unsubscribe from orderbook WebSocket channel"""
        if self.ws_client:
            return await self.ws_client.unsubscribe_orderbook(symbol.upper())

    async def close_position(self, symbol, position=None, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)
        