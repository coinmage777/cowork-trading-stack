"""
Lighter WebSocket Client
========================
WS URL: wss://mainnet.zklighter.elliot.ai/stream

주요 채널:
- market_stats/all: 전체 마켓 가격 정보 (mark_price, index_price)
- user_stats/{ACCOUNT_ID}: 계정 담보/잔고 통계
- account_all/{ACCOUNT_ID}: 포지션, 자산, 오더 전체 정보
- account_all_positions/{ACCOUNT_ID}: 포지션만

사용 방법:
    client = LighterWSClient(account_id=123)
    await client.connect()
    await client.subscribe()
    # 이후 get_mark_price(), get_position(), get_collateral() 등 사용
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)

# 상수
WS_URL_MAINNET = "wss://mainnet.zklighter.elliot.ai/stream"
WS_CONNECT_TIMEOUT = 10
RECONNECT_MIN = 0.5
RECONNECT_MAX = 8.0
FORCE_RECONNECT_INTERVAL = 60  # 강제 재연결 주기 (초)


class LighterWSClient(BaseWSClient):
    """
    Lighter WebSocket 클라이언트.
    BaseWSClient를 상속하되, 고유 기능 유지:
    - 429 rate limit 대응
    - 주기적 강제 재연결 (60초)
    - Delta 기반 오더북
    """

    WS_URL = WS_URL_MAINNET
    WS_CONNECT_TIMEOUT = WS_CONNECT_TIMEOUT
    PING_INTERVAL = None  # 별도 ping 대신 force_reconnect 사용
    RECV_TIMEOUT = 60.0  # 60초간 메시지 없으면 재연결
    RECONNECT_MIN = RECONNECT_MIN
    RECONNECT_MAX = RECONNECT_MAX

    def __init__(
        self,
        account_id: int,
        auth_token: Optional[str] = None,
        auth_token_getter: Optional[callable] = None,
        ws_url: str = WS_URL_MAINNET,
    ):
        super().__init__()
        self.account_id = account_id
        self.auth_token = auth_token
        self._auth_token_getter = auth_token_getter  # 재연결 시 새 토큰 발급용
        self.WS_URL = ws_url  # 인스턴스별 URL 설정

        self._stop = asyncio.Event()
        self._extra_tasks: List[asyncio.Task] = []  # force_reconnect 등
        self._active_subs: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()

        # ========== 캐시 데이터 ==========
        self._market_stats: Dict[int, Dict[str, Any]] = {}
        self._spot_market_stats: Dict[int, Dict[str, Any]] = {}
        self._user_stats: Dict[str, Any] = {}
        self._positions: Dict[int, Dict[str, Any]] = {}
        self._assets: Dict[int, Dict[str, Any]] = {}
        self._orders: Dict[int, List[Dict[str, Any]]] = {}
        self._symbol_to_market_id: Dict[str, int] = {}
        self._market_id_to_symbol: Dict[int, str] = {}

        # ========== Orderbook (delta-based) ==========
        self._orderbooks: Dict[int, Dict[str, Any]] = {}
        self._orderbook_nonces: Dict[int, int] = {}
        self._orderbook_subs: set[int] = set()
        self._orderbook_events: Dict[int, asyncio.Event] = {}
        self._orderbook_asks_dict: Dict[int, Dict[str, float]] = {}
        self._orderbook_bids_dict: Dict[int, Dict[str, float]] = {}

        # ========== 이벤트 (첫 데이터 수신 대기용) ==========
        self._market_stats_ready = asyncio.Event()
        self._user_stats_ready = asyncio.Event()
        self._account_all_ready = asyncio.Event()
        self._orders_ready = asyncio.Event()

    # ==================== Abstract Method Implementations ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """메시지 타입별 처리 (BaseWSClient 호출)"""
        self._dispatch(data)

    async def _resubscribe(self) -> None:
        """재연결 시 구독 복원 (초기 연결과 동일하게 처리)"""
        # 모든 캐시 초기화 (초기 상태로)
        self._orders.clear()
        self._orders_ready.clear()
        self._positions.clear()
        self._user_stats.clear()
        self._assets.clear()
        self._active_subs.clear()

        # Orderbook 초기화
        for mid in list(self._orderbook_subs):
            self._orderbooks.pop(mid, None)
            self._orderbook_nonces.pop(mid, None)
            self._orderbook_asks_dict.pop(mid, None)
            self._orderbook_bids_dict.pop(mid, None)
            if mid in self._orderbook_events:
                self._orderbook_events[mid].clear()

        # 이벤트 초기화
        self._market_stats_ready.clear()
        self._user_stats_ready.clear()
        self._account_all_ready.clear()

        # 재연결 시 새 auth token 발급
        if self._auth_token_getter:
            try:
                self.auth_token = self._auth_token_getter()
            except Exception as e:
                print(f"[LighterWS] Failed to get new auth token: {e}")

        # 초기 연결과 동일하게 subscribe() 호출
        await self.subscribe()

        # 오더북 재구독
        for mid in list(self._orderbook_subs):
            channel = f"order_book/{mid}"
            await self._send_subscribe(channel)
        # 데이터는 백그라운드에서 수신됨 (대기하지 않음)

    def _build_ping_message(self) -> Optional[str]:
        """Lighter는 force_reconnect 사용, ping 불필요"""
        return None

    # ==================== Override connect for Lighter-specific tasks ====================

    async def connect(self) -> bool:
        """WS 연결 (base class 429 대응 + Lighter 전용 force_reconnect 루프)"""
        result = await super().connect()
        if result:
            self._stop.clear()
            # Lighter 전용: 주기적 강제 재연결 루프 시작
            self._extra_tasks.append(asyncio.create_task(self._force_reconnect_loop()))
        return result

    async def close(self) -> None:
        """연결 종료"""
        self._running = False
        self._stop.set()

        # extra tasks 정리
        for t in self._extra_tasks:
            if not t.done():
                t.cancel()
        self._extra_tasks.clear()

        # Base close 호출
        await super().close()
        logger.info("[LighterWS] Closed")

    # ==================== 구독 ====================

    async def subscribe(self) -> None:
        """기본 구독: market_stats/all, user_stats, account_all, account_all_orders"""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        await self._send_subscribe("market_stats/all")
        await self._send_subscribe("spot_market_stats/all")
        await self._send_subscribe(f"user_stats/{self.account_id}")
        await self._send_subscribe(f"account_all/{self.account_id}")
        if self.auth_token:
            await self._send_subscribe(f"account_all_orders/{self.account_id}")

    async def _send_subscribe(self, channel: str) -> None:
        """단일 채널 구독 (중복 방지)"""
        if channel in self._active_subs:
            return
        async with self._send_lock:
            if channel in self._active_subs:
                return
            if not self._ws or not self._running:
                return
            msg = {"type": "subscribe", "channel": channel}
            if self.auth_token and ("orders" in channel or "tx" in channel):
                msg["auth"] = self.auth_token
            print(f"[LighterWS] Subscribe: {channel}")
            await self._ws.send(_json_dumps(msg))
            self._active_subs.add(channel)

    # ==================== Orderbook 구독 ====================

    async def subscribe_orderbook(self, symbol: str) -> bool:
        """Orderbook 채널 구독 (delta-based)."""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            logger.warning(f"[LighterWS] Unknown symbol for orderbook: {symbol}")
            return False

        if mid in self._orderbook_subs:
            return True

        channel = f"order_book/{mid}"
        await self._send_subscribe(channel)
        self._orderbook_subs.add(mid)

        if mid not in self._orderbook_events:
            self._orderbook_events[mid] = asyncio.Event()

        return True

    async def unsubscribe_orderbook(self, symbol: str) -> bool:
        """Orderbook 채널 구독 해제"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return False

        if mid not in self._orderbook_subs:
            return True

        channel = f"order_book/{mid}"
        if channel not in self._active_subs:
            self._orderbook_subs.discard(mid)
            return True

        async with self._send_lock:
            msg = {"type": "unsubscribe", "channel": channel}
            if self._ws and self._running:
                print(f"[LighterWS] Unsubscribe: {channel}")
                await self._ws.send(_json_dumps(msg))
            self._active_subs.discard(channel)
            self._orderbook_subs.discard(mid)

        self._orderbooks.pop(mid, None)
        self._orderbook_nonces.pop(mid, None)
        self._orderbook_asks_dict.pop(mid, None)
        self._orderbook_bids_dict.pop(mid, None)
        if mid in self._orderbook_events:
            self._orderbook_events[mid].clear()

        logger.debug(f"[LighterWS] Unsubscribed orderbook: {symbol} (market_id={mid})")
        return True

    # ==================== 내부 루프 (커스텀) ====================

    async def _recv_loop(self) -> None:
        """메시지 수신 루프 (Lighter 전용 - timeout 포함)"""
        while self._running and not self._stop.is_set():
            if not self._ws:
                await asyncio.sleep(0.1)
                continue
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self.RECV_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[LighterWS] Recv timeout, reconnecting...")
                await self._handle_disconnect()
                break
            except (ConnectionClosed, ConnectionClosedOK):
                logger.warning("[LighterWS] Connection closed, reconnecting...")
                await self._handle_disconnect()
                break
            except Exception as e:
                logger.error(f"[LighterWS] Recv error: {e}")
                await self._handle_disconnect()
                break

            if isinstance(raw, str) and "established" in raw.lower():
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            try:
                self._dispatch(msg)
            except Exception as e:
                logger.exception(f"[LighterWS] Dispatch error: {e}")

    async def _force_reconnect_loop(self) -> None:
        """주기적 강제 재연결 (Lighter WS 특성상 필요)"""
        try:
            while self._running and not self._stop.is_set():
                await asyncio.sleep(FORCE_RECONNECT_INTERVAL)
                if self._stop.is_set():
                    break
                await self._force_reconnect()
        except asyncio.CancelledError:
            pass

    async def _force_reconnect(self) -> None:
        """강제 재연결 수행 (주기적 재연결용)"""
        if self._reconnecting:
            return

        async with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True

            try:
                if await self._do_reconnect():
                    print("[LighterWS] Reconnected (periodic)")
                    logger.info("[LighterWS] Reconnected (periodic)")
            finally:
                self._reconnecting = False

    async def _handle_disconnect(self) -> None:
        """연결 끊김 처리 → 재연결 (Lighter 전용: orderbook 캐시 정리 추가)"""
        # Orderbook 캐시 삭제 (Lighter 고유)
        for mid in list(self._orderbook_subs):
            self._orderbooks.pop(mid, None)
            self._orderbook_nonces.pop(mid, None)
            self._orderbook_asks_dict.pop(mid, None)
            self._orderbook_bids_dict.pop(mid, None)
            if mid in self._orderbook_events:
                self._orderbook_events[mid].clear()

        # Base class의 _handle_disconnect 호출 (소켓 정리 + 재연결)
        await super()._handle_disconnect()

    # ==================== 메시지 디스패치 ====================

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """메시지 타입별 처리"""
        ch = str(msg.get("channel") or "")
        msg_type = str(msg.get("type") or "")

        if msg_type == "pong" or ch == "pong":
            return
        if msg_type == "error":
            logger.error(f"[LighterWS] Error: {msg}")
            return
        if ch.startswith("market_stats:"):
            self._handle_market_stats(msg)
            return
        if ch.startswith("spot_market_stats:"):
            self._handle_spot_market_stats(msg)
            return
        if ch.startswith("user_stats:"):
            self._handle_user_stats(msg)
            return
        if ch.startswith("account_all:"):
            self._handle_account_all(msg)
            return
        if ch.startswith("account_all_positions:"):
            self._handle_positions(msg)
            return
        if ch.startswith("account_all_orders:"):
            self._handle_orders(msg)
            return
        if ch.startswith("order_book:"):
            self._handle_orderbook(msg)
            return

    def _handle_market_stats(self, msg: Dict[str, Any]) -> None:
        """market_stats 처리"""
        ch = msg.get("channel", "")
        data = msg.get("market_stats")
        if not data:
            return

        if ch == "market_stats:all":
            if isinstance(data, dict) and "market_id" in data:
                mid = int(data["market_id"])
                self._market_stats[mid] = data
            elif isinstance(data, dict):
                for k, v in data.items():
                    try:
                        mid = int(k)
                        self._market_stats[mid] = v
                    except Exception:
                        pass
        else:
            if isinstance(data, dict) and "market_id" in data:
                mid = int(data["market_id"])
                self._market_stats[mid] = data

        if not self._market_stats_ready.is_set():
            self._market_stats_ready.set()

    def _handle_spot_market_stats(self, msg: Dict[str, Any]) -> None:
        """spot_market_stats 처리"""
        ch = msg.get("channel", "")
        data = msg.get("spot_market_stats")
        if not data:
            return

        if ch == "spot_market_stats:all":
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        mid = int(k)
                        self._spot_market_stats[mid] = v
                    except Exception:
                        pass
        else:
            if isinstance(data, dict) and "market_id" in data:
                mid = int(data["market_id"])
                self._spot_market_stats[mid] = data

    def _handle_user_stats(self, msg: Dict[str, Any]) -> None:
        """user_stats 처리"""
        stats = msg.get("stats")
        if stats:
            self._user_stats = stats
            if not self._user_stats_ready.is_set():
                self._user_stats_ready.set()

    def _handle_account_all(self, msg: Dict[str, Any]) -> None:
        """account_all 처리 (포지션, 자산)"""
        positions = msg.get("positions")
        if positions and isinstance(positions, dict):
            for k, v in positions.items():
                try:
                    mid = int(k)
                    self._positions[mid] = v
                except Exception:
                    pass

        assets = msg.get("assets")
        if assets:
            if isinstance(assets, dict):
                for k, v in assets.items():
                    try:
                        aid = int(k)
                        self._assets[aid] = v
                    except Exception:
                        pass
            elif isinstance(assets, list):
                for a in assets:
                    if isinstance(a, dict) and "asset_id" in a:
                        self._assets[int(a["asset_id"])] = a

        if not self._account_all_ready.is_set():
            self._account_all_ready.set()

    def _handle_positions(self, msg: Dict[str, Any]) -> None:
        """account_all_positions 처리"""
        positions = msg.get("positions")
        if positions and isinstance(positions, dict):
            for k, v in positions.items():
                try:
                    mid = int(k)
                    self._positions[mid] = v
                except Exception:
                    pass

    def _handle_orders(self, msg: Dict[str, Any]) -> None:
        """
        account_all_orders 처리.
        WS는 변경된 주문만 보낼 수 있으므로, order_index로 병합.
        - 새 주문: 추가
        - 기존 주문 업데이트: 갱신
        - cancelled/filled: 목록에서 제거
        """
        orders = msg.get("orders")
        if orders and isinstance(orders, dict):
            for k, v in orders.items():
                try:
                    mid = int(k)
                    incoming = v if isinstance(v, list) else ([v] if v else [])

                    # 디버그: 들어온 주문 로깅
                    incoming_summary = [
                        {"oid": o.get("order_index"), "status": o.get("status")}
                        for o in incoming
                    ]
                    logger.debug(f"[orders] mid={mid} incoming={incoming_summary}")

                    # 기존 주문을 order_index로 인덱싱
                    existing = self._orders.get(mid, [])
                    order_map: Dict[Any, Dict[str, Any]] = {}
                    for o in existing:
                        oid = o.get("order_index")
                        if oid is not None:
                            order_map[oid] = o

                    # 새로 온 주문 병합
                    for o in incoming:
                        oid = o.get("order_index")
                        if oid is not None:
                            order_map[oid] = o  # 추가 or 덮어쓰기

                    # cancelled/filled 주문 제거, 나머지만 유지
                    open_statuses = ("open", "pending", "partially_filled", "")
                    result = []
                    for o in order_map.values():
                        status = str(o.get("status", "")).lower()
                        if status in open_statuses:
                            result.append(o)

                    # 디버그: 결과 로깅
                    result_summary = [o.get("order_index") for o in result]
                    logger.debug(f"[orders] mid={mid} result={result_summary} (was {len(existing)}, incoming {len(incoming)})")

                    self._orders[mid] = result
                except Exception as e:
                    logger.error(f"[orders] error: {e}")

        if not self._orders_ready.is_set():
            self._orders_ready.set()

    def _handle_orderbook(self, msg: Dict[str, Any]) -> None:
        """order_book 처리 (delta-based)"""
        ch = msg.get("channel", "")
        try:
            mid = int(ch.split(":")[1])
        except (IndexError, ValueError):
            return

        ob_data = msg.get("order_book")
        if not ob_data:
            return

        new_nonce = ob_data.get("nonce")
        begin_nonce = ob_data.get("begin_nonce")
        asks_update = ob_data.get("asks", [])
        bids_update = ob_data.get("bids", [])

        is_first = mid not in self._orderbooks

        if is_first:
            asks_dict: Dict[str, float] = {}
            bids_dict: Dict[str, float] = {}
            for item in asks_update:
                try:
                    px_str = str(item.get("price", "0"))
                    sz = float(item.get("size", 0))
                    if sz > 0:
                        asks_dict[px_str] = sz
                except (ValueError, TypeError):
                    continue
            for item in bids_update:
                try:
                    px_str = str(item.get("price", "0"))
                    sz = float(item.get("size", 0))
                    if sz > 0:
                        bids_dict[px_str] = sz
                except (ValueError, TypeError):
                    continue

            asks = [[float(px), sz] for px, sz in asks_dict.items()]
            bids = [[float(px), sz] for px, sz in bids_dict.items()]
            asks.sort(key=lambda x: x[0])
            bids.sort(key=lambda x: x[0], reverse=True)

            self._orderbooks[mid] = {
                "asks": asks,
                "bids": bids,
                "time": int(time.time() * 1000),
            }
            self._orderbook_asks_dict[mid] = asks_dict
            self._orderbook_bids_dict[mid] = bids_dict
        else:
            last_nonce = self._orderbook_nonces.get(mid)
            if last_nonce is not None and begin_nonce is not None:
                if begin_nonce != last_nonce:
                    logger.warning(
                        f"[LighterWS] Orderbook nonce discontinuity for market {mid}: "
                        f"expected begin_nonce={last_nonce}, got {begin_nonce}. Resetting cache."
                    )
                    self._orderbooks.pop(mid, None)
                    self._orderbook_nonces.pop(mid, None)
                    self._orderbook_asks_dict.pop(mid, None)
                    self._orderbook_bids_dict.pop(mid, None)
                    return

            asks_dict = self._orderbook_asks_dict.get(mid, {})
            bids_dict = self._orderbook_bids_dict.get(mid, {})

            for item in asks_update:
                try:
                    px_str = str(item.get("price", "0"))
                    sz = float(item.get("size", 0))
                    if sz <= 0:
                        asks_dict.pop(px_str, None)
                    else:
                        asks_dict[px_str] = sz
                except (ValueError, TypeError):
                    continue

            for item in bids_update:
                try:
                    px_str = str(item.get("price", "0"))
                    sz = float(item.get("size", 0))
                    if sz <= 0:
                        bids_dict.pop(px_str, None)
                    else:
                        bids_dict[px_str] = sz
                except (ValueError, TypeError):
                    continue

            self._orderbook_asks_dict[mid] = asks_dict
            self._orderbook_bids_dict[mid] = bids_dict

            asks = [[float(px), sz] for px, sz in asks_dict.items()]
            bids = [[float(px), sz] for px, sz in bids_dict.items()]
            asks.sort(key=lambda x: x[0])
            bids.sort(key=lambda x: x[0], reverse=True)

            self._orderbooks[mid] = {
                "asks": asks,
                "bids": bids,
                "time": int(time.time() * 1000),
            }

        if new_nonce is not None:
            self._orderbook_nonces[mid] = new_nonce

        if mid in self._orderbook_events:
            self._orderbook_events[mid].set()

    # ==================== 외부 인터페이스 ====================

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._running

    def set_market_mapping(self, symbol_to_market_id: Dict[str, int]) -> None:
        """symbol -> market_id 매핑 설정"""
        self._symbol_to_market_id = {k.upper(): v for k, v in symbol_to_market_id.items()}
        self._market_id_to_symbol = {v: k for k, v in self._symbol_to_market_id.items()}

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        """첫 데이터 수신 대기"""
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._market_stats_ready.wait(),
                    self._user_stats_ready.wait(),
                    self._account_all_ready.wait(),
                ),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_price_ready(self, symbol: str = "", timeout: float = 5.0) -> bool:  # noqa: ARG002
        """가격 데이터 수신 대기 (Lighter는 전체 마켓 구독이므로 symbol 무시)"""
        del symbol  # unused, for API compatibility
        return await self.wait_ready(timeout=timeout)

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """마크 가격 조회 (캐시)"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return None
        stats = self._market_stats.get(mid)
        if stats:
            for key in ("mark_price", "last_trade_price", "index_price"):
                val = stats.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        pass
        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """가격 조회 (alias for get_mark_price)"""
        return self.get_mark_price(symbol)

    def get_spot_price(self, symbol: str) -> Optional[float]:
        """스팟 마켓 가격 조회"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return None
        stats = self._spot_market_stats.get(mid)
        if stats:
            for key in ("mid_price", "last_trade_price"):
                val = stats.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        pass
        return None

    def get_all_prices(self) -> Dict[str, float]:
        """모든 마켓 가격 반환 {symbol: mark_price}"""
        result = {}
        for mid, stats in self._market_stats.items():
            symbol = self._market_id_to_symbol.get(mid)
            if symbol:
                for key in ("mark_price", "last_trade_price"):
                    val = stats.get(key)
                    if val is not None:
                        try:
                            result[symbol] = float(val)
                            break
                        except Exception:
                            pass
        return result

    def get_orderbook(self, symbol: str, depth: int = 50) -> Optional[Dict[str, Any]]:
        """Orderbook 조회 (캐시)"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return None
        ob = self._orderbooks.get(mid)
        if ob is None:
            return None
        return {
            "asks": ob["asks"][:depth],
            "bids": ob["bids"][:depth],
            "time": ob["time"],
        }

    async def wait_orderbook_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """Orderbook 데이터 수신 대기"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return False

        if mid in self._orderbooks:
            return True

        if mid not in self._orderbook_events:
            self._orderbook_events[mid] = asyncio.Event()

        try:
            await asyncio.wait_for(self._orderbook_events[mid].wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_collateral_ready(self, timeout: float = 5.0) -> bool:
        """담보 데이터 수신 대기 (user_stats 채널)"""
        if self._user_stats:
            return True
        try:
            await asyncio.wait_for(self._user_stats_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_position_ready(self, timeout: float = 5.0) -> bool:
        """포지션 데이터 수신 대기"""
        if self._account_all_ready.is_set():
            return True
        try:
            await asyncio.wait_for(self._account_all_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def get_collateral(self) -> Dict[str, Any]:
        """담보 정보 조회 (캐시)"""
        stats = self._user_stats
        if not stats:
            return {}

        def _f(key: str, default=0.0) -> float:
            try:
                return float(stats.get(key, default))
            except Exception:
                return default

        return {
            "total_collateral": _f("collateral"),
            "available_collateral": _f("available_balance"),
            "portfolio_value": _f("portfolio_value"),
            "leverage": _f("leverage"),
            "margin_usage": _f("margin_usage"),
            "buying_power": _f("buying_power"),
        }

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """특정 심볼 포지션 조회 (캐시)"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return None

        pos = self._positions.get(mid)
        if not pos:
            return None

        return self._normalize_position(pos)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """모든 포지션 반환 {symbol: normalized_position}"""
        result = {}
        for mid, pos in self._positions.items():
            symbol = self._market_id_to_symbol.get(mid)
            if symbol:
                norm = self._normalize_position(pos)
                if norm and norm.get("size", 0) != 0:
                    result[symbol] = norm
        return result

    def _normalize_position(self, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """WS 포지션 데이터 정규화"""
        if not pos:
            return None

        def _f(key: str, default=None):
            val = pos.get(key)
            if val is None:
                return default
            try:
                return float(val)
            except Exception:
                return default

        size_val = _f("position", 0)
        sign = pos.get("sign", 1)
        if sign == -1:
            side = "short"
        else:
            side = "long"

        if size_val == 0:
            return None

        return {
            "symbol": pos.get("symbol", ""),
            "side": side,
            "size": abs(size_val),
            "entry_price": _f("avg_entry_price"),
            "unrealized_pnl": _f("unrealized_pnl"),
            "liquidation_price": _f("liquidation_price"),
            "raw_data": pos,
        }

    def get_assets(self) -> Dict[str, Dict[str, float]]:
        """자산(스팟 잔고) 조회"""
        result = {}
        for aid, asset in self._assets.items():
            if not isinstance(asset, dict):
                continue
            symbol = asset.get("symbol", f"ASSET_{aid}")
            try:
                total = float(asset.get("balance", 0))
                locked = float(asset.get("locked_balance", 0))
                available = total - locked
                result[symbol] = {
                    "total": total,
                    "available": available,
                    "locked": locked,
                }
            except Exception:
                pass
        return result

    def _is_order_open(self, order: Dict[str, Any]) -> bool:
        """주문이 아직 열려있는지 확인"""
        if not order:
            return False
        status = str(order.get("status", "")).lower()
        # open/pending 상태만 true
        return status in ("open", "pending", "partially_filled", "")

    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """특정 심볼의 오픈 오더 조회 (캐시)"""
        mid = self._symbol_to_market_id.get(symbol.upper())
        if mid is None:
            return []

        orders = self._orders.get(mid, [])
        return [self._normalize_order(o, symbol) for o in orders if self._is_order_open(o)]

    def get_all_open_orders(self) -> Dict[str, List[Dict[str, Any]]]:
        """모든 심볼의 오픈 오더 반환"""
        result = {}
        for mid, orders in self._orders.items():
            symbol = self._market_id_to_symbol.get(mid)
            if symbol and orders:
                normalized = [self._normalize_order(o, symbol) for o in orders if self._is_order_open(o)]
                if normalized:
                    result[symbol] = normalized
        return result

    def _normalize_order(self, order: Dict[str, Any], symbol: str = "") -> Dict[str, Any]:
        """WS 오더 데이터 정규화"""
        if not order:
            return {}

        is_ask = order.get("is_ask", False)
        side = "sell" if is_ask else "buy"

        return {
            "id": order.get("order_index"),
            "client_order_id": order.get("client_order_index"),
            "symbol": symbol or self._market_id_to_symbol.get(order.get("market_index", 0), ""),
            "size": str(order.get("initial_base_amount", "")),
            "remaining_size": str(order.get("remaining_base_amount", "")),
            "filled_size": str(order.get("filled_base_amount", "")),
            "price": str(order.get("price", "")),
            "side": side,
            "order_type": order.get("type", ""),
            "status": order.get("status", ""),
            "reduce_only": order.get("reduce_only", False),
            "time_in_force": order.get("time_in_force", ""),
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at"),
        }


# ==================== 공유 풀 (싱글턴) ====================

class LighterWSPool:
    """계정별 WS 클라이언트 풀."""

    def __init__(self):
        self._clients: Dict[int, LighterWSClient] = {}
        self._refcnt: Dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        account_id: int,
        auth_token: Optional[str] = None,
        auth_token_getter: Optional[callable] = None,
        ws_url: str = WS_URL_MAINNET,
        symbol_to_market_id: Optional[Dict[str, int]] = None,
    ) -> LighterWSClient:
        """클라이언트 획득 (없으면 생성)"""
        async with self._lock:
            if account_id in self._clients:
                self._refcnt[account_id] = self._refcnt.get(account_id, 0) + 1
                client = self._clients[account_id]
                if symbol_to_market_id:
                    client.set_market_mapping(symbol_to_market_id)
                # 기존 클라이언트에도 새 auth_token_getter 갱신
                if auth_token_getter:
                    client._auth_token_getter = auth_token_getter
                return client

            client = LighterWSClient(
                account_id=account_id,
                auth_token=auth_token,
                auth_token_getter=auth_token_getter,
                ws_url=ws_url,
            )
            if symbol_to_market_id:
                client.set_market_mapping(symbol_to_market_id)

            await client.connect()
            await client.subscribe()

            self._clients[account_id] = client
            self._refcnt[account_id] = 1
            return client

    async def release(self, account_id: int, force_close: bool = False) -> None:
        """
        클라이언트 해제.

        Args:
            account_id: Account ID
            force_close: True면 참조 카운트 무시하고 즉시 종료
        """
        async with self._lock:
            if account_id not in self._clients:
                return

            if force_close:
                # 강제 종료: 참조 카운트 무시
                client = self._clients.pop(account_id, None)
                self._refcnt.pop(account_id, None)
                if client:
                    await client.close()
                    print(f"[LighterWSPool] Force closed: {account_id}")
            else:
                # 일반 해제: 참조 카운트 감소
                self._refcnt[account_id] = max(0, self._refcnt.get(account_id, 1) - 1)
                if self._refcnt[account_id] == 0:
                    client = self._clients.pop(account_id, None)
                    self._refcnt.pop(account_id, None)
                    if client:
                        await client.close()


# 글로벌 풀 인스턴스
LIGHTER_WS_POOL = LighterWSPool()
