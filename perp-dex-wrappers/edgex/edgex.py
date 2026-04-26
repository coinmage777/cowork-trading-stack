from mpdex.base import MultiPerpDex, MultiPerpDexMixin
import logging
import time
import aiohttp
import uuid
import hashlib
from eth_hash.auto import keccak  # 꼭 이걸 써야 함
from starkware.crypto.signature.fast_pedersen_hash import pedersen_hash
from starkware.crypto.signature.signature import sign, ec_mult, verify, ALPHA, FIELD_PRIME, EC_GEN
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import asyncio
from typing import Optional, Dict, Any

from .edgex_ws_client import EdgeXPublicWSClient, EdgeXPrivateWSClient

logger = logging.getLogger(__name__)

class EdgexExchange(MultiPerpDexMixin, MultiPerpDex):

    ws_supported = {
        "get_mark_price": True,
        "get_orderbook": True,
        "get_position": True,
        "get_collateral": False,  # WS doesn't provide available balance
        "get_open_orders": True,
        "create_order": False,
        "cancel_orders": False,
        "close_position": False,
    }

    def __init__(self, account_id, private_key, *, prefer_ws: bool = True):
        super().__init__()
        self.base_url = 'https://pro.edgex.exchange'
        self.base_url_spot = 'https://spot.edgex.exchange'
        self.account_id = account_id
        self.private_key_hex = private_key.replace("0x", "")

        self.K_MODULUS = int("<HEX_64>", 16)
        self.market_info = {}  # symbol → metadata
        self.usdt_coin_id = '1000'

        # WS 설정
        self.prefer_ws = prefer_ws
        self._public_ws: Optional[EdgeXPublicWSClient] = None
        self._private_ws: Optional[EdgeXPrivateWSClient] = None
        self._contract_id_map: Dict[str, str] = {}  # symbol -> contractId
    
    async def init(self):
        await self.get_meta_data()
        await self.get_meta_data(is_spot=True)
        self.update_available_symbols()
        self._build_contract_id_map()

        # WS 클라이언트 초기화 (prefer_ws일 경우)
        if self.prefer_ws:
            await self._init_ws_clients()

        return self

    def _build_contract_id_map(self):
        """Build symbol -> contractId mapping"""
        for symbol, info in self.market_info.items():
            if 'contractId' in info:
                self._contract_id_map[symbol] = info['contractId']

    async def _init_ws_clients(self):
        """Initialize WebSocket clients"""
        try:
            # Public WS
            self._public_ws = EdgeXPublicWSClient()
            connected = await self._public_ws.connect()
            if connected:
                logger.info("[EdgeX] Public WS connected")
            else:
                logger.warning("[EdgeX] Public WS connection failed, will use REST")
                self._public_ws = None

            # Private WS (needs auth)
            signature, timestamp = self._generate_ws_auth_signature()
            self._private_ws = EdgeXPrivateWSClient(self.account_id, signature, timestamp)
            connected = await self._private_ws.connect()
            if connected:
                logger.info("[EdgeX] Private WS connected")
                # Wait for initial snapshot
                await self._private_ws.wait_snapshot_ready(timeout=5.0)
            else:
                logger.warning("[EdgeX] Private WS connection failed, will use REST")
                self._private_ws = None

        except Exception as e:
            logger.error(f"[EdgeX] WS init error: {e}")
            self._public_ws = None
            self._private_ws = None

    def _generate_ws_auth_signature(self) -> tuple:
        """Generate signature for private WS authentication (SDK style)"""
        timestamp = str(int(time.time() * 1000))

        # SDK style: path includes accountId without ? separator
        path = f"/api/v1/private/wsaccountId={self.account_id}"
        sign_content = f"{timestamp}GET{path}"

        # Keccak256 hash
        msg_hash = int.from_bytes(keccak(sign_content.encode()), "big")
        msg_hash = msg_hash % self.K_MODULUS

        # Sign
        private_key_int = int(self.private_key_hex, 16)
        r, s = sign(msg_hash, private_key_int)

        # SDK uses just r+s (no y coordinate)
        signature = r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()

        return signature, timestamp

    def update_available_symbols(self):
        self.available_symbols['spot'] = []
        self.available_symbols['perp'] = []
        
        for k, v in self.market_info.items():
            if '/' in k:
                # spot
                self.available_symbols['spot'].append(k)
                #print(k)
            else:
                # perp
                coin = k.split('USD')[0]
                quote_id = v['contract']['quoteCoinId']
                quote = self.get_perp_quote(coin)
                composite_symbol = f"{coin}-{quote}"
                #print(k,quote,quote_id)
                self.available_symbols['perp'].append(composite_symbol)
            
    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USD'
    
    def round_step_size(self, value: Decimal, step_size: str) -> Decimal:
        step = Decimal(step_size)
        precision = abs(step.as_tuple().exponent)
        return value.quantize(step, rounding=ROUND_DOWN)
    
    async def get_meta_data(self,is_spot=False):
        if is_spot:
            url = f"{self.base_url_spot}/api/v1/public/meta/getMetaData"
        else:
            url = f"{self.base_url}/api/v1/public/meta/getMetaData"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    #print(f"[get_meta_data] HTTP {resp.status}")
                    return None
                res = await resp.json()
                
                data = res.get("data", {})
                meta = data
                if is_spot:
                    market_list = data.get("symbolList", [])
                else:
                    market_list = data.get("contractList", [])

                for market in market_list:
                    
                    name = market["symbolName"] if is_spot else market["contractName"]
                    
                    if "TEMP" in name:
                        continue

                    if is_spot:
                        self.market_info[name] = {
                            "contract": market,
                            "meta": meta,
                            "symbolId": market["symbolId"],
                            "tickSize": market["tickSize"],
                            "stepSize": market["stepSize"],
                            "minOrderSize": market["minOrderSize"],
                            "maxOrderSize": market["maxOrderSize"],
                            "defaultTakerFeeRate": market["takerFeeRate"],
                        }
                    else:
                        self.market_info[name] = {
                            "contract": market,
                            "meta": meta,
                            "contractId": market["contractId"],
                            "tickSize": market["tickSize"],
                            "stepSize": market["stepSize"],
                            "minOrderSize": market["minOrderSize"],
                            "maxOrderSize": market["maxOrderSize"],
                            "defaultTakerFeeRate": market["defaultTakerFeeRate"],
                        }

                return market_list
    
    def generate_signature(self, method, path, params, timestamp=None):
        if not timestamp:
            timestamp = str(int(time.time() * 1000))
        
        sorted_items = sorted(params.items())
        param_str = "&".join(f"{k}={v}" for k, v in sorted_items)
        
        message = timestamp+method+path+param_str
        msg_bytes = message.encode("utf-8")
        
        msg_hash = int.from_bytes(keccak(msg_bytes), "big")
        msg_hash = msg_hash % self.K_MODULUS # FIELD_PRIME
        
        private_key_int = int(self.private_key_hex, 16)
        
        r, s = sign(msg_hash, private_key_int)
        _, y =  ec_mult(private_key_int, EC_GEN, ALPHA, FIELD_PRIME)
        
        y_hex = y.to_bytes(32, "big").hex()
        
        stark_signature = r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + y_hex
        
        return stark_signature, timestamp

    async def get_mark_price(self, symbol):
        # spot has no restapi endpoint, have to use ws
        is_spot = '/' in symbol
        if is_spot:
            logger.warning("[edgex] spot is not supported yet")
            return

        contract_id = self._contract_id_map.get(symbol)
        if not contract_id:
            contract_info = self.market_info.get(symbol)
            if not contract_info:
                logger.warning(f"[EdgeX] Unknown symbol: {symbol}")
                return None
            contract_id = contract_info['contractId']

        # Try WS first
        if self._public_ws and self._public_ws.connected:
            # Subscribe if not already
            await self._public_ws.subscribe_ticker(contract_id)
            # Wait for data
            if await self._public_ws.wait_ticker_ready(contract_id, timeout=3.0):
                price = self._public_ws.get_mark_price(contract_id)
                if price:
                    return Decimal(str(price))

        # Fallback to REST
        return await self._get_mark_price_rest(symbol, contract_id)

    async def _get_mark_price_rest(self, symbol, contract_id):
        """Get mark price via REST API"""
        params = {"contractId": contract_id}
        oracle_url = f"{self.base_url}/api/v1/public/quote/getTicker"

        async with aiohttp.ClientSession() as session:
            async with session.get(oracle_url, params=params) as resp:
                ticker_data = await resp.json()
                last_price = Decimal(ticker_data["data"][0]["lastPrice"])
                return last_price

    async def get_orderbook(self, symbol, limit: int = 50) -> Optional[Dict[str, Any]]:
        """Get orderbook via WS (WS only, no REST fallback for depth)

        Args:
            symbol: Trading symbol
            limit: Number of bids/asks to return (default 50)
        """
        is_spot = '/' in symbol
        if is_spot:
            logger.warning("[edgex] spot orderbook is not supported yet")
            return None

        contract_id = self._contract_id_map.get(symbol)
        if not contract_id:
            contract_info = self.market_info.get(symbol)
            if not contract_info:
                logger.warning(f"[EdgeX] Unknown symbol: {symbol}")
                return None
            contract_id = contract_info['contractId']

        if not self._public_ws or not self._public_ws.connected:
            logger.warning("[EdgeX] Orderbook requires WS connection")
            return None

        # Subscribe with 200 levels (server-side)
        await self._public_ws.subscribe_orderbook(contract_id, depth=200)
        # Wait for snapshot
        if await self._public_ws.wait_orderbook_ready(contract_id, timeout=5.0):
            return self._public_ws.get_orderbook(contract_id, depth=limit)

        return None

    async def unsubscribe_orderbook(self, symbol) -> None:
        """Unsubscribe from orderbook WS channel"""
        contract_id = self._contract_id_map.get(symbol)
        if not contract_id:
            contract_info = self.market_info.get(symbol)
            if contract_info:
                contract_id = contract_info['contractId']

        if contract_id and self._public_ws:
            await self._public_ws.unsubscribe_orderbook(contract_id)

    async def create_order(self, symbol, side, amount, price=None, order_type='market', *, is_reduce_only=False):
        is_spot = '/' in symbol
        if is_spot:
            logger.warning("[edgex] spot is not supported yet")
            return
        if price != None:
            order_type = 'limit'

        time_in_force = 'IMMEDIATE_OR_CANCEL' if order_type.upper() == 'MARKET' else 'GOOD_TIL_CANCEL'
        
        contract_info = self.market_info[symbol]
        tick_size = Decimal(contract_info['tickSize'])
        step_size = contract_info['stepSize']

        size = Decimal(str(amount))
        size = self.round_step_size(size, step_size)
        price_dec = Decimal(str(price)) if price else Decimal(0)

        client_order_id = str(uuid.uuid4())

        if is_spot:
            symbol_id = contract_info['symbolId']
            value = price_dec * size
            value = self.round_step_size(value, '0.0001')

            body = {
                "price": str(price_dec if order_type.upper() != 'MARKET' else 0),
                "size": str(size),
                "type": order_type.upper(),
                "timeInForce": time_in_force,
                "reduceOnly": 'false',
                "symbolId": symbol_id,
                "side": side.upper(),
                "clientOrderId": client_order_id,
            }
            #print(body)
            
        else:
            LIMIT_ORDER_WITH_FEES = 3
            
            contract_id = contract_info['contractId']
            
            resolution = Decimal(int(contract_info['contract']['starkExResolution'], 16))
            fee_rate = Decimal(contract_info['defaultTakerFeeRate'])

            # Price calculation
            if order_type.upper() == 'MARKET':
                # Oracle price fetch
                oracle_url = f"{self.base_url}/api/v1/public/quote/getTicker"
                async with aiohttp.ClientSession() as session:
                    async with session.get(oracle_url, params={"contractId": contract_id}) as resp:
                        ticker_data = await resp.json()
                        oracle_price = Decimal(ticker_data["data"][0]["oraclePrice"])
                if side.upper() == 'BUY':
                    price = oracle_price * Decimal("1.1")
                    price = price.quantize(tick_size, rounding=ROUND_HALF_UP)
                else:
                    price = oracle_price * Decimal("0.9")
                    price = price.quantize(tick_size, rounding=ROUND_HALF_UP)
            else:
                price = price_dec.quantize(tick_size, rounding=ROUND_HALF_UP)

            # Calculate value after price is determined
            value = price * size
            value = self.round_step_size(value, '0.0001')

            is_buy = side.upper() == 'BUY'

            
            l2_nonce = int(hashlib.sha256(client_order_id.encode()).hexdigest()[:8], 16)
            l2_expire_time = str(int(time.time() * 1000) + 14 * 24 * 60 * 60 * 1000)
            expire_time = str(int(l2_expire_time) - 10 * 24 * 60 * 60 * 1000)

            amt_synth = int((size * resolution).to_integral_value())
            amt_coll = int((value * Decimal("1e6")).to_integral_value())
            amt_fee = int((value * fee_rate * Decimal("1e6")).to_integral_value())
            expire_ts = int(int(l2_expire_time) / (1000 * 60 * 60))

            asset_id_synth = int(contract_info['contract']['starkExSyntheticAssetId'], 16)
            asset_id_coll = int(contract_info['meta']['global']['starkExCollateralCoin']['starkExAssetId'], 16)

            # L2 order hash
            h = pedersen_hash(asset_id_coll if is_buy else asset_id_synth,
                            asset_id_synth if is_buy else asset_id_coll)
            h = pedersen_hash(h, asset_id_coll)
            packed_0 = (amt_coll if is_buy else amt_synth)
            packed_0 = (packed_0 << 64) + (amt_synth if is_buy else amt_coll)
            packed_0 = (packed_0 << 64) + amt_fee
            packed_0 = (packed_0 << 32) + l2_nonce
            h = pedersen_hash(h, packed_0)
            packed_1 = LIMIT_ORDER_WITH_FEES
            pid = int(self.account_id)
            packed_1 = (packed_1 << 64) + pid
            packed_1 = (packed_1 << 64) + pid
            packed_1 = (packed_1 << 64) + pid
            packed_1 = (packed_1 << 32) + expire_ts
            packed_1 = (packed_1 << 17)
            h = pedersen_hash(h, packed_1)

            private_key_int = int(self.private_key_hex, 16)
            r, s = sign(h, private_key_int)
            l2_signature = r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()

            body = {
                "accountId": self.account_id,
                "contractId": contract_id,
                "price": str(price if order_type.upper() != 'MARKET' else 0),
                "size": str(size),
                "type": order_type.upper(),
                "timeInForce": time_in_force,
                "side": side.upper(),
                "reduceOnly": 'false',
                "clientOrderId": client_order_id,
                "expireTime": expire_time,
                "l2Nonce": str(l2_nonce),
                "l2Value": str(value),
                "l2Size": str(size),
                "l2LimitFee": str((value * fee_rate).quantize(Decimal("1.000000"))),
                "l2ExpireTime": l2_expire_time,
                "l2Signature": l2_signature
            }

        method = "POST"
        path = "/api/v1/private/order/createOrder"
        signature, ts = self.generate_signature(method, path, body)
        url = f"{self.base_url_spot}{path}" if is_spot else f"{self.base_url}{path}"
        headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-edgeX-Api-Timestamp": ts,
                    "X-edgeX-Api-Signature": signature,
                }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url=url,
                json=body,
                headers=headers
            ) as resp:
                return await resp.json()

    def parse_position(self, position_list, position_asset_list, symbol):
        contract_id = self.market_info[symbol]['contractId']

        size = 0
        pos = None
        side = None
        entry_price = None
        unrealized_pnl = None

        for position in position_list:
            if position['contractId'] == contract_id:
                pos = position
                size = position['openSize']
                side = 'short' if '-' in size else 'long'
                size = size.replace('-', '')

        for position in position_asset_list:
            if position['contractId'] == contract_id:
                entry_price = position.get('avgEntryPrice', None)
                unrealized_pnl = position.get('unrealizedPnl', None)
                liquidation_price = position.get('liquidatePrice', None)

        if size == 0:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": float(entry_price) if entry_price else None,
            "unrealized_pnl": round(float(unrealized_pnl), 2) if unrealized_pnl else None,
            "liquidation_price": liquidation_price,
            "raw_data": pos,
        }
        
    
    async def get_position(self, symbol):
        contract_id = self._contract_id_map.get(symbol)
        if not contract_id:
            contract_info = self.market_info.get(symbol)
            if not contract_info:
                logger.warning(f"[EdgeX] Unknown symbol: {symbol}")
                return None
            contract_id = contract_info['contractId']

        # Try WS first
        if self._private_ws and self._private_ws._snapshot_received:
            pos_data = self._private_ws.get_position(contract_id)
            if pos_data:
                return self._parse_ws_position(pos_data, symbol)
            # No position for this symbol
            return None

        # Fallback to REST
        return await self._get_position_rest(symbol)

    def _parse_ws_position(self, pos_data: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """Parse position from WS data"""
        size_str = pos_data.get('openSize', '0')
        if size_str == '0' or size_str == '0.000':
            return None

        # Determine side from sign
        size_float = float(size_str)
        if size_float == 0:
            return None

        side = 'short' if size_float < 0 else 'long'
        size = abs(size_float)

        # Calculate entry price from openValue / openSize
        open_value = float(pos_data.get('openValue', '0'))
        entry_price = abs(open_value / size_float) if size_float != 0 else 0

        # Unrealized PnL not directly available in WS data
        # Would need current price to calculate
        unrealized_pnl = 0

        return {
            "symbol": symbol,
            "side": side,
            "size": str(size),
            "entry_price": round(entry_price, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "liquidation_price": None, #없음
            "raw_data": pos_data,
        }

    async def _get_position_rest(self, symbol):
        """Get position via REST API"""
        method = "GET"
        path = "/api/v1/private/account/getAccountAsset"
        params = {
            "accountId": self.account_id,
        }

        signature, timestamp = self.generate_signature(method, path, params)

        headers = {
            "X-edgeX-Api-Timestamp": timestamp,
            "X-edgeX-Api-Signature": signature
        }

        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if query_str:
            url = f"{self.base_url}{path}?{query_str}"
        else:
            url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"[get_position] HTTP {resp.status}")
                    logger.debug(f"[edgex] response: {await resp.text()}")
                    return None
                data = await resp.json()
                position_list = data['data']['positionList']
                position_asset_list = data['data']['positionAssetList']
                return self.parse_position(position_list, position_asset_list, symbol)
    
    async def close_position(self, symbol, position, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)

    async def close(self):
        """Close WS connections"""
        if self._public_ws:
            await self._public_ws.close()
            self._public_ws = None
        if self._private_ws:
            await self._private_ws.close()
            self._private_ws = None

    async def get_collateral(self):
        # REST only (WS doesn't provide available balance)
        return await self._get_collateral_rest()

    async def _get_collateral_rest(self):
        """Get collateral via REST API"""
        method = "GET"
        path = "/api/v1/private/account/getAccountAsset"
        params = {
            "accountId": self.account_id,
        }

        signature, timestamp = self.generate_signature(method, path, params)

        headers = {
            "X-edgeX-Api-Timestamp": timestamp,
            "X-edgeX-Api-Signature": signature
        }

        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if query_str:
            url = f"{self.base_url}{path}?{query_str}"
        else:
            url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"[get_collateral] HTTP {resp.status}")
                    logger.debug(f"[edgex] response: {await resp.text()}")
                    return None
                data = await resp.json()
                collateral = data['data']['collateralAssetModelList']
                return self.parse_collateral(collateral)
            
    def parse_collateral(self,collateral):
        for col in collateral:
            if col['coinId'] == self.usdt_coin_id:
                available_collateral = round(float(col['availableAmount']),2)
                total_collateral = round(float(col['totalEquity']),2)
                return {'available_collateral': available_collateral, 'total_collateral': total_collateral}
            
    async def get_open_orders(self, symbol):
        contract_id = self._contract_id_map.get(symbol)
        if not contract_id:
            contract_info = self.market_info.get(symbol)
            if not contract_info:
                logger.warning(f"[EdgeX] Unknown symbol: {symbol}")
                return []
            contract_id = contract_info['contractId']

        # Try WS first
        if self._private_ws and self._private_ws._snapshot_received:
            orders = self._private_ws.get_open_orders(contract_id)
            return self._parse_ws_open_orders(orders, symbol)

        # Fallback to REST
        return await self._get_open_orders_rest(symbol, contract_id)

    def _parse_ws_open_orders(self, orders: list, symbol: str) -> list:
        """Parse open orders from WS data"""
        if not orders:
            return []

        return [
            {
                "symbol": symbol,
                "id": o.get("id"),  # field is "id" not "orderId"
                "size": o.get("size"),
                "price": o.get("price"),
                "side": o.get("side").lower(), # buy or sell
                "order_type": o.get("type"),
                "status": o.get("status")
            }
            for o in orders
        ]

    async def _get_open_orders_rest(self, symbol, contract_id):
        """Get open orders via REST API"""
        method = "GET"
        path = "/api/v1/private/order/getActiveOrderPage"
        params = {
            "accountId": self.account_id,
            "size": "200",
            "filterContractIdList": contract_id,
        }

        signature, timestamp = self.generate_signature(method, path, params)

        headers = {
            "X-edgeX-Api-Timestamp": timestamp,
            "X-edgeX-Api-Signature": signature
        }

        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{self.base_url}{path}?{query_str}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []

                res = await resp.json()
                orders = res.get("data", {}).get("dataList", [])
                return self.parse_open_orders(orders)
            
    def parse_open_orders(self, orders):
        if not orders:
            return []

        return [
            {
                "symbol": self._get_symbol_from_contract_id(o["contractId"]),
                "id": o["id"],
                "size": o["size"],
                "price": o["price"],
                "side": o["side"],
                "order_type": o["type"],
                "status": o["status"]
            }
            for o in orders if o.get("status") == "OPEN"
        ]
        
    def _get_symbol_from_contract_id(self, contract_id):
        for symbol, info in self.market_info.items():
            if info.get("contractId") == contract_id:
                return symbol
        return None  # 없을 경우 None 반환 (혹은 raise 예외)

    async def cancel_orders(self, symbol, open_orders = None):
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []
        
        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]

        order_ids = [o["id"] for o in open_orders]

        method = "POST"
        path = "/api/v1/private/order/cancelOrderById"

        # ✅ 서명용 문자열에서 orderIdList=id1&id2 포맷 필요
        order_id_str = "&".join(order_ids)
        params = {
            "accountId": self.account_id,
            "orderIdList": order_id_str  # ⚠️ 문자열이어야 함
        }

        signature, timestamp = self.generate_signature(method, path, params)

        headers = {
            "X-edgeX-Api-Timestamp": timestamp,
            "X-edgeX-Api-Signature": signature,
            "Content-Type": "application/json"
        }

        # ✅ 요청 본문은 리스트 형태
        body = {
            "accountId": self.account_id,
            "orderIdList": order_ids
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}{path}", json=body, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"[cancel_orders] HTTP {resp.status}")
                    logger.debug(f"[edgex] response: {await resp.text()}")
                    return []

                res = await resp.json()
                cancel_map = res.get("data", {}).get("cancelResultMap", {})
                return [{"id": k, "status": v} for k, v in cancel_map.items()]

