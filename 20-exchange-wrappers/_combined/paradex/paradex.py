from mpdex.base import MultiPerpDex, MultiPerpDexMixin
import ccxt.async_support as ccxt  # 비동기 CCXT 지원
from starkware.crypto.signature.signature import ec_mult, ALPHA, FIELD_PRIME, EC_GEN
import asyncio
from typing import Optional, Dict, Any, List

from .paradex_ws_client import PARADEX_WS_POOL, ParadexWSClient


class ParadexExchange(MultiPerpDexMixin, MultiPerpDex):
    """
    Paradex Exchange 래퍼.
    - REST API: ccxt 사용
    - WebSocket API: paradex_ws_client 사용 (직접 구현)
    """

    def __init__(
        self,
        wallet_address,
        paradex_address,
        paradex_private_key,
        prefer_ws: bool = True,
    ):
        super().__init__()
        # WS 지원 여부 설정 (super().__init__() 후에 설정해야 함)
        self.ws_supported.update({
            "get_mark_price": True,
            "get_orderbook": True,
            "get_position": True,
            "get_collateral": True,
            "get_open_orders": True,
            # REST only (WS 미지원)
            "create_order": False,
            "cancel_orders": False,
        })
        self._wallet_address = wallet_address
        self._paradex_address = paradex_address
        self._paradex_private_key = paradex_private_key
        self._prefer_ws = prefer_ws

        self.exchange = ccxt.paradex({
            'walletAddress': wallet_address,
            'privateKey': int(paradex_private_key.replace('0x', ''), 16),
        })

        self.exchange.options.update({
            "paradexAccount": {
                "address": paradex_address,
                "publicKey": self.public_key_from_private_key(paradex_private_key),
                "privateKey": int(paradex_private_key.replace('0x', ''), 16),
            }
        })

        # WS 관련
        self._ws_client: Optional[ParadexWSClient] = None
        self._ws_initialized = False
        self._jwt_token: Optional[str] = None

    async def init(self):
        """초기화 (마켓 로드, WS 연결 포함)"""
        # 마켓 정보 로드
        await self.exchange.load_markets()
        self._update_available_symbols()

        # WS 초기화
        if self._prefer_ws:
            await self._init_ws()
        return self

    def _update_available_symbols(self):
        """available_symbols 업데이트"""
        self.available_symbols["perp"] = []
        for symbol, market in self.exchange.markets.items():
            # ccxt 통합 형식: JUP/USD:USDC (type: swap) -> perp
            if market.get("type") == "swap":
                # base 추출: JUP/USD:USDC -> JUP
                base = market.get("base", "")
                quote = self.get_perp_quote(symbol)
                composite_symbol = f"{base}-{quote}"
                self.available_symbols["perp"].append(composite_symbol)

    async def _init_ws(self) -> None:
        """WebSocket 초기화"""
        if self._ws_initialized:
            return

        try:
            # JWT 토큰 획득 (ccxt REST API 사용)
            await self.exchange.authenticate_rest()
            # ccxt 내부에서 JWT를 사용하므로, 직접 가져오기
            jwt = await self._get_jwt_token()

            # WS 연결
            self._ws_client = await PARADEX_WS_POOL.acquire(jwt_token=jwt)

            # 기본 구독 (ticker, account, positions, orders)
            await self._ws_client.subscribe_ticker()
            await self._ws_client.subscribe_account()
            await self._ws_client.subscribe_positions()
            await self._ws_client.subscribe_orders("ALL")

            # REST로 초기 데이터 로드 → WS 캐시에 저장
            await self._load_initial_cache()

            self._ws_initialized = True
            print("[ParadexExchange] WS initialized")

        except Exception as e:
            print(f"[ParadexExchange] WS init failed: {e}, falling back to REST")
            self._prefer_ws = False

    async def _load_initial_cache(self) -> None:
        """REST로 초기 데이터 로드하여 WS 캐시에 저장"""
        if not self._ws_client:
            return

        try:
            # 1. Account
            account = await self.exchange.private_get_account()
            if account:
                self._ws_client._account = {
                    "free_collateral": float(account.get("free_collateral", 0)),
                    "total_collateral": float(account.get("total_collateral", 0)),
                }
                self._ws_client._account_ready.set()

            # 2. Positions
            positions_resp = await self.exchange.private_get_positions()
            positions = positions_resp.get("results", [])
            for pos in positions:
                market = pos.get("market")
                size = pos.get("size")
                if market and size and float(size) != 0:
                    self._ws_client._positions[market] = {
                        "market": market,
                        "side": pos.get("side", "").lower(),
                        "size": str(abs(float(size))),
                        "entry_price": float(pos.get("average_entry_price", 0)),
                        "unrealized_pnl": float(pos.get("unrealized_pnl", 0)),
                        "raw": pos,
                    }
            self._ws_client._positions_ready.set()

            # 3. Open Orders
            orders_resp = await self.exchange.fetch_open_orders()
            for order in orders_resp:
                order_id = order.get("id")
                if order_id:
                    # native 형식 사용 (BTC-USD-PERP), ccxt symbol은 BTC/USD:USDC
                    native_symbol = order.get("info", {}).get("market") or order.get("symbol")
                    self._ws_client._orders[order_id] = {
                        "id": order_id,
                        "symbol": native_symbol,
                        "side": (order.get("side") or "").lower(),
                        "type": (order.get("type") or "").lower(),
                        "size": order.get("amount"),
                        "price": order.get("price"),
                        "status": (order.get("status") or "").lower(),
                        "raw": order,
                    }
            self._ws_client._orders_ready.set()

            print(f"[ParadexExchange] Initial cache loaded: positions={len(self._ws_client._positions)}, orders={len(self._ws_client._orders)}")

        except Exception as e:
            print(f"[ParadexExchange] Failed to load initial cache: {e}")

    async def _get_jwt_token(self) -> Optional[str]:
        """ccxt에서 JWT 토큰 획득"""
        try:
            # ccxt paradex는 authenticate_rest() 호출 시 options['authToken']에 JWT 저장
            await self.exchange.authenticate_rest()
            jwt = self.exchange.options.get('authToken')
            if jwt:
                return jwt

            # 없으면 직접 호출
            response = await self.exchange.private_post_auth({})
            jwt = response.get('jwt_token')
            return jwt
        except Exception as e:
            print(f"[ParadexExchange] Failed to get JWT: {e}")
            return None

    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USD'

    def public_key_from_private_key(self, private_key):
        private_key_int = int(private_key, 16)
        x, _ = ec_mult(private_key_int, EC_GEN, ALPHA, FIELD_PRIME)
        return hex(x)

    def parse_position(self, positions, symbol):
        if not positions:
            return None

        position = None
        for pos in positions:
            if pos.get('market') == symbol:
                position = pos
                break

        if not position or position.get("size") == '0' or position.get("size") == 0:
            return None

        return {
            "symbol": symbol,
            "side": position.get("side", "").lower(),
            "size": str(position.get("size", "0")).replace('-', ''),
            "entry_price": float(position.get("average_entry_price", 0)),
            "unrealized_pnl": float(position.get("unrealized_pnl", 0)),
            "liquidation_price": float(position.get("liquidation_price")) if position.get("liquidation_price") else None,
            "raw_data": position,
        }

    # ==================== Mark Price ====================

    async def get_mark_price(self, symbol):
        """마크 가격 조회 (WS 우선)"""
        if self._prefer_ws and self._ws_client:
            price = self._ws_client.get_mark_price(symbol)
            if price is not None:
                return price
            # WS에 아직 데이터 없으면 대기 후 재시도
            await self._ws_client.wait_ticker_ready(symbol, timeout=3.0)
            price = self._ws_client.get_mark_price(symbol)
            if price is not None:
                return price

        # REST fallback
        print(f"[ParadexExchange] get_mark_price: REST fallback")
        res = await self.exchange.fetch_ticker(symbol)
        return res['last']

    # ==================== Orderbook ====================

    async def get_orderbook(self, symbol) -> Optional[Dict[str, Any]]:
        """오더북 조회 (WS 우선)"""
        if self._prefer_ws and self._ws_client:
            # 구독 확인/추가
            await self._ws_client.subscribe_orderbook(symbol)
            await self._ws_client.wait_orderbook_ready(symbol, timeout=3.0)
            book = self._ws_client.get_orderbook(symbol)
            if book:
                return book

        # REST fallback (ccxt 사용)
        print(f"[ParadexExchange] get_orderbook: REST fallback")
        try:
            res = await self.exchange.fetch_order_book(symbol)
            return {
                "bids": res.get("bids", []),
                "asks": res.get("asks", []),
                "time": res.get("timestamp"),
            }
        except Exception as e:
            print(f"[get_orderbook] error: {e}")
            return None

    async def unsubscribe_orderbook(self, symbol) -> None:
        """오더북 구독 해제"""
        if self._ws_client:
            await self._ws_client.unsubscribe_orderbook(symbol)

    # ==================== Position ====================

    async def get_position(self, symbol):
        """포지션 조회 (WS 우선)"""
        if self._prefer_ws and self._ws_client:
            # 데이터 준비 대기
            ready = await self._ws_client.wait_positions_ready(timeout=3.0)
            if ready:
                # 데이터 수신 완료 → None이면 포지션 없음
                pos = self._ws_client.get_position(symbol)
                return pos  # None도 정상 반환

        # REST fallback
        print(f"[ParadexExchange] get_position: REST fallback")
        await self.exchange.authenticate_rest()
        try:
            positions = await self.exchange.private_get_positions()
            return self.parse_position(positions['results'], symbol)
        except Exception as e:
            print(f"[ParadexExchange] get_position error: {e}")
            return None

    # ==================== Collateral ====================

    async def get_collateral(self):
        """담보 조회 (WS 우선)"""
        if self._prefer_ws and self._ws_client:
            # 데이터 준비 대기
            ready = await self._ws_client.wait_account_ready(timeout=3.0)
            if ready:
                coll = self._ws_client.get_collateral()
                if coll:
                    return coll

        # REST fallback
        print(f"[ParadexExchange] get_collateral: REST fallback")
        await self.exchange.authenticate_rest()
        return self.parse_collateral(await self.exchange.private_get_account())

    def parse_collateral(self, collateral):
        available_collateral = round(float(collateral['free_collateral']), 2)
        total_collateral = round(float(collateral['total_collateral']), 2)
        return {
            'available_collateral': available_collateral,
            'total_collateral': total_collateral
        }

    # ==================== Open Orders ====================

    async def get_open_orders(self, symbol):
        """오픈 주문 조회 (WS 우선)"""
        if self._prefer_ws and self._ws_client:
            # 데이터 준비 대기
            ready = await self._ws_client.wait_orders_ready(timeout=3.0)
            if ready:
                # 데이터 수신 완료 → 빈 리스트도 정상
                orders = self._ws_client.get_open_orders(symbol)
                return self._parse_ws_orders(orders)

        # REST fallback
        print(f"[ParadexExchange] get_open_orders: REST fallback")
        orders = await super().get_open_orders(symbol)
        return self.parse_orders(orders)

    def _parse_ws_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """WS 주문 데이터를 표준 형식으로 변환"""
        parsed = []
        for o in orders:
            parsed.append({
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "type": o.get("type"),
                "side": o.get("side"),
                "size": o.get("size"),
                "price": o.get("price"),
            })
        return parsed

    # ==================== Order Operations (REST only) ====================

    async def create_order(self, symbol, side, amount, price=None, order_type='market', *, is_reduce_only=False):
        if price is not None:
            order_type = 'limit'

        if order_type == 'market':
            return self.parse_orders(await self.exchange.create_order(symbol, 'market', side, amount, price))
        return self.parse_orders(await self.exchange.create_order(symbol, 'limit', side, amount, price))

    def parse_orders(self, orders):
        if not orders:
            return []

        # 단일 dict일 경우 리스트로 감싸기
        if isinstance(orders, dict):
            orders = [orders]

        parsed = []
        for order in orders:
            parsed.append({
                "id": order.get("id"),
                "symbol": order.get("symbol"),
                "type": order.get("type"),
                "side": order.get("side"),
                "size": order.get("amount"),
                "price": order.get("price")
            })

        return parsed

    async def close_position(self, symbol, position, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)

    async def cancel_orders(self, symbol, open_orders=None):
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []

        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]

        tasks = []
        for order in open_orders:
            order_id = order["id"]
            tasks.append(asyncio.create_task(self.exchange.cancel_order(order_id)))

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            parsed_results = []
            for i, res in enumerate(results):
                order_id = open_orders[i]["id"]
                if isinstance(res, Exception):
                    print(f"[cancel_order] order_id={order_id} failed: {res}")
                    parsed_results.append({
                        "id": order_id,
                        "status": "FAILED",
                        "error": str(res)
                    })
                else:
                    parsed_results.append({
                        "id": res.get("id"),
                        "symbol": res.get("market"),
                        "type": res.get("type"),
                        "side": res.get("side"),
                        "price": res.get("price"),
                        "status": res.get("status")
                    })
            return parsed_results
        except Exception as e:
            print(f"[cancel_orders] Unexpected error: {e}")
            return []

    # ==================== Close ====================

    async def close(self, force_close: bool = True):
        """
        연결 종료.

        Args:
            force_close: True (default) = 연결 종료, False = 풀에 유지
        """
        # WS 해제
        if self._ws_initialized:
            await PARADEX_WS_POOL.release(force_close=force_close)
            self._ws_client = None
            self._ws_initialized = False

        # REST 종료
        await self.exchange.close()
