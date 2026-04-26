"""
Paradex WebSocket Client
========================
Paradex WebSocket API를 직접 구현한 클라이언트.
- JSON-RPC 2.0 형식
- JWT 인증 (ccxt에서 토큰 가져오기)
- 채널: account, positions, orders, order_book, markets_summary
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)

PARADEX_WS_URL = "wss://ws.api.prod.paradex.trade/v1"
PARADEX_WS_URL_TESTNET = "wss://ws.api.testnet.paradex.trade/v1"


class ParadexWSClient(BaseWSClient):
    """
    Paradex WebSocket 클라이언트.
    - BaseWSClient 상속
    - JSON-RPC 2.0 형식
    - Private 채널은 JWT 인증 필요
    """

    WS_URL = PARADEX_WS_URL
    PING_INTERVAL = 30.0  # 30초마다 ping
    RECV_TIMEOUT = 60.0
    RECONNECT_MIN = 1.0
    RECONNECT_MAX = 8.0

    def __init__(
        self,
        jwt_token: Optional[str] = None,
        testnet: bool = False,
    ):
        super().__init__()
        self.WS_URL = PARADEX_WS_URL_TESTNET if testnet else PARADEX_WS_URL
        self._jwt_token = jwt_token
        self._authenticated = False
        self._msg_id = 0

        # 구독 관리
        self._subscriptions: Set[str] = set()  # 채널 이름들
        self._send_lock = asyncio.Lock()

        # 캐시
        self._account: Optional[Dict[str, Any]] = None
        self._positions: Dict[str, Dict[str, Any]] = {}  # market -> position
        self._orders: Dict[str, Dict[str, Any]] = {}  # order_id -> order
        self._orderbooks: Dict[str, Dict[str, Any]] = {}  # symbol -> orderbook
        self._tickers: Dict[str, Dict[str, Any]] = {}  # symbol -> ticker

        # 이벤트 (데이터 준비 대기용)
        self._auth_event = asyncio.Event()  # 인증 완료 대기용
        self._account_ready = asyncio.Event()
        self._positions_ready = asyncio.Event()
        self._orders_ready = asyncio.Event()
        self._orderbook_events: Dict[str, asyncio.Event] = {}
        self._ticker_events: Dict[str, asyncio.Event] = {}

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def set_jwt_token(self, token: str) -> None:
        """JWT 토큰 설정 (외부에서 ccxt로 얻은 토큰 주입)"""
        self._jwt_token = token

    # ==================== Connection & Auth ====================

    async def connect(self) -> bool:
        """연결 후 인증"""
        result = await super().connect()
        if result and self._jwt_token:
            await self._authenticate()
        return result

    async def _authenticate(self) -> bool:
        """JWT 인증"""
        if not self._jwt_token:
            logger.warning("[ParadexWS] No JWT token, skipping auth")
            return False

        self._auth_event.clear()
        auth_msg = {
            "jsonrpc": "2.0",
            "method": "auth",
            "params": {"bearer": self._jwt_token},
            "id": self._next_id(),
        }

        try:
            await self._ws.send(_json_dumps(auth_msg))
            # 인증 응답 대기 (최대 5초)
            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=5.0)
                return self._authenticated
            except asyncio.TimeoutError:
                print("[ParadexWS] Auth timeout")
                return False
        except Exception as e:
            print(f"[ParadexWS] Auth failed: {e}")
            return False

    # ==================== Subscribe / Unsubscribe ====================

    async def subscribe(self, channel: str) -> None:
        """채널 구독"""
        if channel in self._subscriptions:
            return

        msg = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": {"channel": channel},
            "id": self._next_id(),
        }

        async with self._send_lock:
            if self._ws:
                await self._ws.send(_json_dumps(msg))
                self._subscriptions.add(channel)
                logger.info(f"[ParadexWS] Subscribed: {channel}")

    async def unsubscribe(self, channel: str) -> None:
        """채널 구독 해제"""
        if channel not in self._subscriptions:
            return

        msg = {
            "jsonrpc": "2.0",
            "method": "unsubscribe",
            "params": {"channel": channel},
            "id": self._next_id(),
        }

        async with self._send_lock:
            if self._ws:
                await self._ws.send(_json_dumps(msg))
                self._subscriptions.discard(channel)
                logger.info(f"[ParadexWS] Unsubscribed: {channel}")

    # ==================== Public Subscriptions ====================

    async def subscribe_ticker(self, symbol: Optional[str] = None) -> None:
        """마켓 요약 (ticker) 구독 - 모든 심볼"""
        await self.subscribe("markets_summary")

    async def subscribe_orderbook(self, symbol: str) -> None:
        """오더북 구독"""
        # Paradex 형식: order_book.{symbol}.snapshot@15@100ms
        channel = f"order_book.{symbol}.snapshot@15@100ms"
        if channel in self._subscriptions:
            return  # 이미 구독됨
        if symbol not in self._orderbook_events:
            self._orderbook_events[symbol] = asyncio.Event()
        await self.subscribe(channel)
        print(f"[ParadexWS] Subscribe: orderbook/{symbol}")

    async def unsubscribe_orderbook(self, symbol: str) -> None:
        """오더북 구독 해제"""
        channel = f"order_book.{symbol}.snapshot@15@100ms"
        if channel not in self._subscriptions:
            return  # 구독 안 되어있음
        await self.unsubscribe(channel)
        self._orderbooks.pop(symbol, None)
        self._orderbook_events.pop(symbol, None)
        print(f"[ParadexWS] Unsubscribe: orderbook/{symbol}")

    async def subscribe_trades(self, symbol: str) -> None:
        """거래 내역 구독"""
        channel = f"trades.{symbol}"
        await self.subscribe(channel)

    # ==================== Private Subscriptions ====================

    async def subscribe_account(self) -> None:
        """계정 상태 구독 (Private)"""
        await self.subscribe("account")

    async def subscribe_positions(self) -> None:
        """포지션 구독 (Private)"""
        await self.subscribe("positions")

    async def subscribe_orders(self, symbol: str = "ALL") -> None:
        """주문 구독 (Private)"""
        channel = f"orders.{symbol}"
        await self.subscribe(channel)

    # ==================== Message Handling ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """메시지 처리"""
        # JSON-RPC 응답
        if "result" in data:
            msg_id = data.get("id")
            result = data.get("result")

            # 인증 응답
            if isinstance(result, dict) and "node_id" in result:
                self._authenticated = True
                self._auth_event.set()
                print(f"[ParadexWS] Authenticated (node_id: {result.get('node_id')})")
            return

        # 에러 응답
        if "error" in data:
            error = data.get("error", {})
            logger.error(f"[ParadexWS] Error: {error}")
            return

        # 구독 메시지
        if data.get("method") == "subscription":
            params = data.get("params", {})
            channel = params.get("channel", "")
            msg_data = params.get("data", {})

            self._dispatch_subscription(channel, msg_data)

    def _dispatch_subscription(self, channel: str, data: Dict[str, Any]) -> None:
        """구독 메시지 dispatch"""

        # Account
        if channel == "account":
            self._handle_account(data)
            return

        # Positions
        if channel == "positions":
            self._handle_position(data)
            return

        # Orders (orders.{symbol} or orders.ALL)
        if channel.startswith("orders"):
            self._handle_order(data)
            return

        # Markets Summary (ticker)
        if channel == "markets_summary":
            self._handle_ticker(data)
            return

        # Order Book
        if channel.startswith("order_book"):
            self._handle_orderbook(channel, data)
            return

        # Trades
        if channel.startswith("trades"):
            self._handle_trade(data)
            return

        logger.debug(f"[ParadexWS] Unhandled channel: {channel}")

    def _handle_account(self, data: Dict[str, Any]) -> None:
        """Account 메시지 처리"""
        self._account = {
            "account": data.get("account"),
            "account_value": self._fnum(data.get("account_value")),
            "free_collateral": self._fnum(data.get("free_collateral")),
            "total_collateral": self._fnum(data.get("total_collateral")),
            "initial_margin_requirement": self._fnum(data.get("initial_margin_requirement")),
            "maintenance_margin_requirement": self._fnum(data.get("maintenance_margin_requirement")),
            "margin_cushion": self._fnum(data.get("margin_cushion")),
            "status": data.get("status"),
            "settlement_asset": data.get("settlement_asset"),
            "updated_at": data.get("updated_at"),
            "seq_no": data.get("seq_no"),
        }
        if not self._account_ready.is_set():
            self._account_ready.set()

    def _handle_position(self, data: Dict[str, Any]) -> None:
        """Position 메시지 처리"""
        market = data.get("market")
        if not market:
            return

        size = self._fnum(data.get("size"))
        side = data.get("side", "").lower()

        # size가 0이면 포지션 없음 → 삭제
        if size is None or size == 0:
            self._positions.pop(market, None)
        else:
            self._positions[market] = {
                "symbol": market,
                "side": side,
                "size": str(abs(size)) if size else "0",
                "entry_price": self._fnum(data.get("average_entry_price")),
                "unrealized_pnl": self._fnum(data.get("unrealized_pnl")),
                "liquidation_price": self._fnum(data.get("liquidation_price")),
                "raw_data": data,
            }

        if not self._positions_ready.is_set():
            self._positions_ready.set()

    def _handle_order(self, data: Dict[str, Any]) -> None:
        """Order 메시지 처리"""
        order_id = data.get("id")
        if not order_id:
            return

        status = (data.get("status") or "").upper()

        # CLOSED 상태면 삭제
        if status == "CLOSED":
            self._orders.pop(order_id, None)
        else:
            self._orders[order_id] = {
                "id": order_id,
                "symbol": data.get("market"),
                "side": (data.get("side") or "").lower(),
                "type": (data.get("type") or "").lower(),
                "size": self._fnum(data.get("size")),
                "price": self._fnum(data.get("price")),
                "remaining_size": self._fnum(data.get("remaining_size")),
                "avg_fill_price": self._fnum(data.get("avg_fill_price")),
                "status": status.lower(),
                "instruction": data.get("instruction"),
                "created_at": data.get("created_at"),
                "raw": data,
            }

        if not self._orders_ready.is_set():
            self._orders_ready.set()

    def _handle_ticker(self, data: Dict[str, Any]) -> None:
        """Markets Summary (Ticker) 메시지 처리"""
        symbol = data.get("symbol")
        if not symbol:
            return

        self._tickers[symbol] = {
            "symbol": symbol,
            "mark_price": self._fnum(data.get("mark_price")),
            "oracle_price": self._fnum(data.get("oracle_price")),
            "last_traded_price": self._fnum(data.get("last_traded_price")),
            "bid": self._fnum(data.get("bid")),
            "ask": self._fnum(data.get("ask")),
            "volume_24h": self._fnum(data.get("volume_24h")),
            "open_interest": self._fnum(data.get("open_interest")),
            "funding_rate": self._fnum(data.get("funding_rate")),
            "updated_at": data.get("created_at"),
        }

        # 이벤트 시그널
        ev = self._ticker_events.get(symbol)
        if ev and not ev.is_set():
            ev.set()

    def _handle_orderbook(self, channel: str, data: Dict[str, Any]) -> None:
        """OrderBook 메시지 처리"""
        # channel: order_book.BTC-USD-PERP.snapshot@15@100ms
        parts = channel.split(".")
        if len(parts) < 2:
            return
        symbol = parts[1]

        inserts = data.get("inserts") or []
        bids = []
        asks = []

        for item in inserts:
            side = item.get("side")
            price = self._fnum(item.get("price"))
            size = self._fnum(item.get("size"))
            if price is None or size is None:
                continue

            if side == "BUY":
                bids.append([price, size])
            elif side == "SELL":
                asks.append([price, size])

        # 정렬: bids 내림차순, asks 오름차순
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])

        self._orderbooks[symbol] = {
            "bids": bids,
            "asks": asks,
            "time": data.get("last_updated_at"),
            "seq_no": data.get("seq_no"),
        }

        # 이벤트 시그널
        ev = self._orderbook_events.get(symbol)
        if ev and not ev.is_set():
            ev.set()

    def _handle_trade(self, data: Dict[str, Any]) -> None:
        """Trade 메시지 처리 (현재는 로깅만)"""
        # 필요시 구현
        pass

    # ==================== Getters ====================

    def get_account(self) -> Optional[Dict[str, Any]]:
        """계정 정보 반환"""
        return self._account

    def get_collateral(self) -> Optional[Dict[str, Any]]:
        """담보 정보 반환"""
        if not self._account:
            return None
        return {
            "available_collateral": self._account.get("free_collateral"),
            "total_collateral": self._account.get("total_collateral"),
        }

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """특정 심볼 포지션 반환"""
        return self._positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """모든 포지션 반환"""
        return dict(self._positions)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """오픈 주문 반환"""
        orders = list(self._orders.values())
        if symbol:
            orders = [o for o in orders if o.get("symbol") == symbol]
        return orders

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """마크 가격 반환"""
        ticker = self._tickers.get(symbol)
        return ticker.get("mark_price") if ticker else None

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """티커 정보 반환"""
        return self._tickers.get(symbol)

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """오더북 반환"""
        return self._orderbooks.get(symbol)

    # ==================== Wait Methods ====================

    async def wait_account_ready(self, timeout: float = 5.0) -> bool:
        """계정 데이터 준비 대기"""
        if self._account_ready.is_set():
            return True
        try:
            await asyncio.wait_for(self._account_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_positions_ready(self, timeout: float = 5.0) -> bool:
        """포지션 데이터 준비 대기"""
        if self._positions_ready.is_set():
            return True
        try:
            await asyncio.wait_for(self._positions_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orders_ready(self, timeout: float = 5.0) -> bool:
        """주문 데이터 준비 대기"""
        if self._orders_ready.is_set():
            return True
        try:
            await asyncio.wait_for(self._orders_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_orderbook_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """오더북 데이터 준비 대기"""
        ev = self._orderbook_events.get(symbol)
        if ev is None:
            ev = asyncio.Event()
            self._orderbook_events[symbol] = ev
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_ticker_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """티커 데이터 준비 대기"""
        ev = self._ticker_events.get(symbol)
        if ev is None:
            ev = asyncio.Event()
            self._ticker_events[symbol] = ev
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ==================== Abstract Method Implementations ====================

    async def _resubscribe(self) -> None:
        """재연결 후 재구독"""
        # 캐시 초기화
        self._account = None
        self._positions.clear()
        self._orders.clear()
        self._orderbooks.clear()
        # tickers는 유지 (public 데이터)

        # 이벤트 초기화
        self._auth_event.clear()
        self._account_ready.clear()
        self._positions_ready.clear()
        self._orders_ready.clear()
        for ev in self._orderbook_events.values():
            ev.clear()

        # 인증
        self._authenticated = False
        if self._jwt_token:
            await self._authenticate()

        # 채널 재구독
        old_subs = list(self._subscriptions)
        self._subscriptions.clear()
        for channel in old_subs:
            await self.subscribe(channel)

    def _build_ping_message(self) -> Optional[str]:
        """Ping 메시지 (Paradex는 표준 websocket ping 사용)"""
        # Paradex는 JSON-RPC ping이 아닌 websocket protocol ping 사용
        # BaseWSClient의 websockets 라이브러리가 자동으로 처리
        # 추가 JSON ping이 필요하면 여기서 정의
        return None  # websockets 자체 ping 사용

    # ==================== Utility ====================

    @staticmethod
    def _fnum(x, default=None) -> Optional[float]:
        """문자열/숫자를 float로 변환"""
        if x is None:
            return default
        try:
            return float(x)
        except (ValueError, TypeError):
            return default


class ParadexWSPool:
    """
    Paradex WebSocket 연결 풀.
    여러 인스턴스에서 동일한 WS 연결 공유.
    """

    def __init__(self):
        self._client: Optional[ParadexWSClient] = None
        self._lock = asyncio.Lock()
        self._refcount = 0

    async def acquire(
        self,
        jwt_token: Optional[str] = None,
        testnet: bool = False,
    ) -> ParadexWSClient:
        """WS 클라이언트 획득"""
        async with self._lock:
            if self._client is None:
                self._client = ParadexWSClient(jwt_token=jwt_token, testnet=testnet)
                await self._client.connect()
            elif jwt_token and not self._client._authenticated:
                # 토큰이 있고 아직 인증 안됐으면 인증
                self._client.set_jwt_token(jwt_token)
                await self._client._authenticate()

            self._refcount += 1
            return self._client

    async def release(self, force_close: bool = False) -> None:
        """
        WS 클라이언트 해제.

        Args:
            force_close: True면 참조 카운트 무시하고 즉시 종료
        """
        async with self._lock:
            if force_close:
                if self._client:
                    await self._client.close()
                    self._client = None
                    self._refcount = 0
                    print("[ParadexWSPool] Force closed")
            else:
                self._refcount = max(0, self._refcount - 1)
                if self._refcount == 0 and self._client:
                    await self._client.close()
                    self._client = None


# 글로벌 풀 인스턴스
PARADEX_WS_POOL = ParadexWSPool()
