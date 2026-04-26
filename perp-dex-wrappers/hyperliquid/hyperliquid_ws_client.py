import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from .base_ws_client import BaseWSClient, _json_dumps

logger = logging.getLogger(__name__)

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"  # 메인넷 WS
WS_CONNECT_TIMEOUT = 15
WS_READ_TIMEOUT = 60
PING_INTERVAL = None  # recv timeout만 사용
RECONNECT_MIN = 1.0
RECONNECT_MAX = 8.0


def _clean_coin_key_for_perp(key: str) -> Optional[str]:
    """
    Perp/일반 심볼 정규화:
    - '@...' 내부키 제외
    - 'AAA/USDC'처럼 슬래시 포함은 Spot 처리로 넘김
    - 그 외 upper()
    """
    if not key:
        return None
    k = str(key).strip()
    if k.startswith("@"):
        return None
    if "/" in k:
        return None
    return k.upper() or None

def _clean_spot_key_from_pair(key: str) -> Optional[str]:
    """
    'AAA/USDC' → 'AAA' (베이스 심볼만 사용)
    """
    if not key:
        return None
    if "/" not in key:
        return None
    base, _quote = key.split("/", 1)
    base = base.strip().upper()
    return base or None

def _sub_key(sub: dict) -> str:
    """구독 payload를 정규화하여 키 문자열로 변환."""
    # type + 주요 파라미터만 안정적으로 정렬
    t = str(sub.get("type"))
    u = (sub.get("user") or "").lower()
    d = (sub.get("dex") or "").lower()
    c = sub.get("coin") or ""  # coin은 case-sensitive (kPEPE 등)
    return f"{t}|u={u}|d={d}|c={c}"

class HLWSClientRaw(BaseWSClient):
    """
    최소 WS 클라이언트:
    - 단건 구독 메시지: {"method":"subscribe","subscription": {...}}
    - ping: {"method":"ping"}
    - 자동 재연결/재구독
    - Spot 토큰 인덱스 맵을 REST로 1회 로드하여 '@{index}' 키를 Spot 심볼로 변환

    BaseWSClient를 상속하지만, 대부분의 로직은 고유 구현 유지.
    """

    WS_URL = HYPERLIQUID_WS_URL
    PING_INTERVAL = None  # recv timeout만 사용
    RECV_TIMEOUT = WS_READ_TIMEOUT  # 60초
    RECONNECT_MIN = RECONNECT_MIN
    RECONNECT_MAX = RECONNECT_MAX

    def __init__(self, dex: Optional[str] = None, address: Optional[str] = None, proxy: Optional[str] = None):
        super().__init__(proxy=proxy)

        self.dex = dex.lower() if dex else None
        self.address = address

        # self._ws는 BaseWSClient에서 상속
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()  # 연결 완료 시그널 (Pool에서 race condition 방지용)

        # --------- 멀티-유저 캐시(주소 소문자 키) ---------
        self._user_subs: set[str] = set()  # 이미 구독한 user 주소 집합(소문자)
        self._open_orders_ready_by_user: Dict[str, asyncio.Event] = {}

        self._user_margin_by_dex: Dict[str, Dict[str, Dict[str, float]]] = {}         # user -> dex -> margin dict
        self._user_positions_by_dex_norm: Dict[str, Dict[str, Dict[str, Any]]] = {}   # user -> dex -> {coin->norm}
        self._user_positions_by_dex_raw: Dict[str, Dict[str, List[Dict[str, Any]]]] = {} # user -> dex -> raw list
        self._user_balances: Dict[str, Dict[str, float]] = {}                         # user -> {token->amt}
        self._user_open_orders: Dict[str, List[Dict[str, Any]]] = {}                  # user -> list[order]

        self._post_id = 0                            # comment: post 요청용 증가 id
        self._post_waiters: Dict[int, asyncio.Future] = {}  # comment: id -> Future

        # 가격 캐시(전역)
        self.prices: Dict[str, float] = {}
        self.spot_prices: Dict[str, float] = {}
        self.spot_pair_prices: Dict[str, float] = {}

        # Spot 메타(공유로 주입)
        self.spot_index_to_name: Dict[int, str] = {}
        self.spot_name_to_index: Dict[str, int] = {}
        self.spot_asset_index_to_pair: Dict[int, str] = {}
        self.spot_asset_index_to_bq: Dict[int, tuple[str, str]] = {}

        # Perp 원본 이름 매핑 (대문자키 -> 원본, 오더북 구독 시 case-sensitive 대응)
        self.perp_original_names: Dict[str, str] = {}

        self._subscriptions: List[Dict[str, Any]] = []
        self._send_lock = asyncio.Lock()
        self._active_subs: set[str] = set()
        self._price_events: Dict[str, asyncio.Event] = {}
        self._dynamic_dex_subs: set[str] = set()  # Track dynamically added DEX allMids (for resubscribe)

        # [ADDED] Orderbook 캐시: coin(upper) -> {"bids": [...], "asks": [...], "time": int}
        self._orderbooks: Dict[str, Dict[str, Any]] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = {}  # coin -> Event (첫 스냅샷 대기용)
        self._orderbook_sub_counts: Dict[str, int] = {}  # 구독 레퍼런스 카운트 (coin -> count)
        self._orderbook_sub_lock = asyncio.Lock()  # 레퍼런스 카운트 race condition 방지

        # [ADDED] activeAssetData 캐시: (user, coin) -> data
        self._active_asset_data: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._active_asset_events: Dict[Tuple[str, str], asyncio.Event] = {}
    
    def user_count(self) -> int:
        return len(self._user_subs)

    def has_user(self, address: Optional[str]) -> bool:
        if not address:
            return False
        return address.lower().strip() in self._user_subs
    
    def _next_post_id(self) -> int:
        self._post_id += 1
        return self._post_id
    
    async def _post(self, req_type: str, payload: dict, timeout: float = 6.0) -> dict:
        """
        WS 'post' 요청 공통 루틴.
        req_type: 'info' | 'action'
        payload:  Info 또는 Exchange payload
        반환: 서버 응답의 data.response(dict)
        """
        if not self._ws:
            raise RuntimeError("WebSocket is not connected")
        req_id = self._next_post_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._post_waiters[req_id] = fut
        msg = {
            "method": "post",
            "id": req_id,
            "request": {
                "type": req_type,
                "payload": payload,
            },
        }
        await self._ws.send(_json_dumps(msg))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            # resp는 {"type":"info"|"action"|"error","payload": ...}
            return resp
        finally:
            self._post_waiters.pop(req_id, None)

    async def post_info(self, payload: dict, timeout: float = 6.0) -> dict:
        return await self._post("info", payload, timeout=timeout)

    async def post_action(self, payload: dict, timeout: float = 8.0) -> dict:
        return await self._post("action", payload, timeout=timeout)
    
    # ---------- 유저 구독/뷰 관리 ----------
    async def ensure_user_streams(self, address: Optional[str]) -> None:
        """
        단일 소켓에서 특정 user 스트림(webData 계열/spotState/openOrders)을 추가 구독.
        """
        if not address:
            return
        u = address.lower().strip()
        if not u or u in self._user_subs:
            return
        # race condition 방지: await 전에 먼저 set에 추가하여 다른 코루틴 중복 진입 방지
        self._user_subs.add(u)
        self._open_orders_ready_by_user.setdefault(u, asyncio.Event())
        self._user_margin_by_dex.setdefault(u, {})
        self._user_positions_by_dex_norm.setdefault(u, {})
        self._user_positions_by_dex_raw.setdefault(u, {})
        self._user_balances.setdefault(u, {})
        self._user_open_orders.setdefault(u, [])
        # 구독 메시지 전송
        await self._send_subscribe({"type": "allDexsClearinghouseState", "user": u})
        await self._send_subscribe({"type": "spotState", "user": u})
        await self._send_subscribe({"type": "openOrders", "user": u, "dex": "ALL_DEXS"})

    # ---------------- per-user getter ----------------
    def get_balances_by_user(self, address: str) -> Dict[str, float]:
        return dict(self._user_balances.get(address.lower().strip(), {}))

    def get_margin_by_dex_for_user(self, address: str) -> Dict[str, Dict[str, float]]:
        return dict(self._user_margin_by_dex.get(address.lower().strip(), {}))

    def get_positions_norm_for_user(self, address: str) -> Dict[str, Dict[str, Any]]:
        return dict(self._user_positions_by_dex_norm.get(address.lower().strip(), {}))

    def get_positions_raw_for_user(self, address: str) -> Dict[str, Dict[str, Any]]:
        return dict(self._user_positions_by_dex_raw.get(address.lower().strip(), {}))
    
    def get_open_orders_for_user(self, address: str) -> List[Dict[str, Any]]:
        return list(self._user_open_orders.get(address.lower().strip(), []))

    async def wait_open_orders_ready(self, timeout: float = 2.0, address: Optional[str] = None) -> bool:
        """
        해당 주소의 openOrders 첫 스냅샷 대기. address가 없으면 active_user 기준.
        """
        u = (address or "").lower().strip()
        if not u:
            return False
        ev = self._open_orders_ready_by_user.get(u)
        if not ev:
            return False
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except Exception:
            return False
        
    # ---------- 기본 구독(가격)만 유지 ----------
    def build_subscriptions(self) -> List[Dict[str, Any]]:
        subs: List[Dict[str, Any]] = []
        if self.dex:
            subs.append({"type": "allMids", "dex": self.dex})
        else:
            subs.append({"type": "allMids"})
        # user 스트림은 ensure_user_streams에서 개별 추가
        return subs

    async def ensure_core_subs(self) -> None:
        if self.dex:
            await self._send_subscribe({"type": "allMids", "dex": self.dex})
        else:
            await self._send_subscribe({"type": "allMids"})
        # user 스트림은 외부에서 ensure_user_streams 호출

    def _normalize_open_order(self, o: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        원본 open order o를 표준 dict로 변환.
        필수 키: order_id, symbol
        추가 키: side('A'|'B'), price(float), size(float), timestamp(int), raw
        - coin == '@{pairIdx}' → 'BASE/QUOTE'로 매핑(spot)
        - coin == 'AAA' 또는 'xyz:XYZ100' → 그대로 대문자 심볼(Perp)
        """
        try:
            coin_raw = str(o.get("coin") or "")
            # 심볼 해석
            if coin_raw.startswith("@"):
                # 스팟 페어 인덱스
                try:
                    pair_idx = int(coin_raw[1:])
                except Exception:
                    return None
                pair = self.spot_asset_index_to_pair.get(pair_idx)
                if not pair:
                    # 페어 맵이 아직 준비 전이면 보류(다음 메시지에서 갱신됨)
                    return None
                symbol = str(pair).upper()
            else:
                # 텍스트 페어 또는 Perp 심볼
                symbol = coin_raw.upper()

            # 수치 필드
            def fnum(x, default=None):
                try:
                    return float(x)
                except Exception:
                    return default

            out = {
                "order_id": o.get("oid"),
                "symbol": symbol,
                "side": "short" if o.get("side") == 'A' else 'long',
                "price": fnum(o.get("limitPx")),
                "size": fnum(o.get("sz")),
                #"timestamp": int(o.get("timestamp")) if o.get("timestamp") is not None else None,
                #"raw": o,
            }
            return out if out["order_id"] is not None and out["symbol"] else None
        except Exception:
            return None
   
    # 외부(풀)에서 Spot 메타를 주입
    def set_spot_meta(
        self,
        idx2name: Dict[int, str],
        name2idx: Dict[str, int],
        pair_by_index: Dict[int, str],
        bq_by_index: Dict[int, tuple[str, str]],
    ) -> None:
        # 내부에서 그대로 참조해도 되지만, 방어적으로 복사
        self.spot_index_to_name = dict(idx2name or {})
        self.spot_name_to_index = {str(k).upper(): int(v) for k, v in (name2idx or {}).items()}
        self.spot_asset_index_to_pair = dict(pair_by_index or {})
        self.spot_asset_index_to_bq = dict(bq_by_index or {})

    def set_perp_original_names(self, perp_asset_map: Dict[str, tuple]) -> None:
        """
        perp_asset_map에서 원본 이름 매핑 추출.
        key(대문자) -> original_name (6번째 요소)
        """
        self.perp_original_names.clear()
        for key, val in (perp_asset_map or {}).items():
            if len(val) >= 6 and val[5]:
                # 대문자 키 → 원본 이름
                self.perp_original_names[key.upper()] = val[5]

    def _event_key(self, kind: str, key: str) -> str:
        return f"{kind}|{str(key).upper().strip()}"
    
    def _notify_perp(self, coin: str) -> None:
        try:
            ev = self._price_events.get(self._event_key("perp", coin))
            if ev and not ev.is_set():
                ev.set()
        except Exception:
            pass

    def _notify_spot_base(self, base: str) -> None:
        try:
            ev = self._price_events.get(self._event_key("spot_base", base))
            if ev and not ev.is_set():
                ev.set()
        except Exception:
            pass

    def _notify_spot_pair(self, pair: str) -> None:
        try:
            ev = self._price_events.get(self._event_key("spot_pair", pair))
            if ev and not ev.is_set():
                ev.set()
        except Exception:
            pass

    # 외부 API: 첫 틱(또는 이미 캐시 보유)까지 대기
    async def wait_price_ready(
        self,
        symbol: str,
        timeout: float = 5.0,
        *,
        kind: Optional[str] = None,    # 'perp' | 'spot_base' | 'spot_pair' (None이면 자동 판단)
    ) -> bool:
        """
        - kind가 명시되면 해당 타입의 캐시를 점검하고 그 이벤트만 대기.
        - kind가 None이면: '/' 포함 → spot_pair, 그 외 → perp 로 간주.
          (PURR 같은 모호한 베이스 토큰은 반드시 kind를 지정하세요: kind='spot_base')
        """
        s = str(symbol).strip().upper()
        k = (kind or ("spot_pair" if "/" in s else "perp")).lower()

        # 1) 즉시 보유 체크
        has_val = False
        if k == "perp":
            has_val = (self.get_price(s) is not None)
        elif k == "spot_pair":
            has_val = (self.get_spot_pair_px(s) is not None)
        else:
            raise ValueError(f"wait_price_ready: invalid kind={kind!r}")

        if has_val:
            return True

        # 2) 이벤트 생성 후 대기
        ek = self._event_key(k, s)
        ev = self._price_events.get(ek)
        if ev is None:
            ev = asyncio.Event()
            self._price_events[ek] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._running
    
    async def ensure_connected_and_subscribed(self) -> None:
        if not self.connected:
            await self.connect()
            await self.subscribe()
        else:
            # 이미 연결되어 있으면 누락 구독 재보장
            await self.ensure_core_subs()

    async def ensure_allmids_for(self, dex: Optional[str]) -> None:
        """
        하나의 WS 커넥션에서 여러 DEX allMids를 구독할 수 있게 한다.
        - dex=None 또는 'hl' → {"type":"allMids"}
        - 그 외 → {"type":"allMids","dex": "<dex>"}
        중복 구독은 내부 dedup으로 자동 방지.
        """
        key = None
        if dex is None or str(dex).lower() == "hl":
            sub = {"type": "allMids"}
            key = _sub_key(sub)
            if key not in self._active_subs:
                await self._send_subscribe(sub)
            self._dynamic_dex_subs.add("hl")  # Track for resubscribe
        else:
            d = str(dex).lower().strip()
            sub = {"type": "allMids", "dex": d}
            key = _sub_key(sub)
            if key not in self._active_subs:
                await self._send_subscribe(sub)
            self._dynamic_dex_subs.add(d)  # Track for resubscribe

    async def _send_subscribe(self, sub: dict) -> None:
        """subscribe 메시지 전송(중복 방지)."""
        key = _sub_key(sub)
        if key in self._active_subs:
            return
        async with self._send_lock:
            if key in self._active_subs:
                return
            # conn이 None이면 스킵 (재연결 후 _resub_all_channels에서 재구독됨)
            if not self._ws:
                logger.warning(f"_send_subscribe skipped (conn is None): {key}")
                return
            payload = {"method": "subscribe", "subscription": sub}
            logger.debug(f"[HyperliquidWS] Subscribe: {key}")
            await self._ws.send(_json_dumps(payload))
            self._active_subs.add(key)

    async def send_subscribe(self, sub: dict) -> None:
        """Public method to subscribe (for external use)."""
        await self._send_subscribe(sub)

    async def send_unsubscribe(self, sub: dict) -> None:
        """unsubscribe 메시지 전송."""
        key = _sub_key(sub)
        async with self._send_lock:
            if not self._ws:
                logger.warning(f"send_unsubscribe skipped (conn is None): {key}")
                return
            payload = {"method": "unsubscribe", "subscription": sub}
            await self._ws.send(_json_dumps(payload))
            self._active_subs.discard(key)

    def _normalize_position(self, pos: Dict[str, Any]) -> Dict[str, Any]:
        """
        webData3.clearinghouseState.assetPositions[*].position → 표준화 dict
        반환 키:
        - coin: str
        - size: float(절대값), side: 'long'|'short'
        - entry_px, position_value, upnl, roe, liq_px, margin_used: float|None
        - lev_type: 'cross'|'isolated'|..., lev_value: int|None, max_leverage: int|None
        """
        def f(x, default=None):
            try:
                return float(x)
            except Exception:
                return default
        coin = str(pos.get("coin") or "").upper()
        szi = f(pos.get("szi"), 0.0) or 0.0
        side = "long" if szi > 0 else ("short" if szi < 0 else "flat")
        lev = pos.get("leverage") or {}
        lev_type = str(lev.get("type") or "").lower() or None
        try:
            lev_value = int(float(lev.get("value"))) if lev.get("value") is not None else None
        except Exception:
            lev_value = None
        return {
            "coin": coin,
            "size": abs(float(szi)),
            "side": side,
            "entry_px": f(pos.get("entryPx"), None),
            "position_value": f(pos.get("positionValue"), None),
            "upnl": f(pos.get("unrealizedPnl"), None),
            "roe": f(pos.get("returnOnEquity"), None),
            "liq_px": f(pos.get("liquidationPx"), None),
            "margin_used": f(pos.get("marginUsed"), None),
            "margin_mode": lev_type,
            "leverage": lev_value,
            "max_leverage": (int(float(pos.get("maxLeverage"))) if pos.get("maxLeverage") is not None else None),
            "raw": pos,  # 원본도 보관(디버깅/확장용)
        }

    
    def get_spot_pair_px(self, pair: str) -> Optional[float]:
        """
        스팟 페어 가격 조회(내부 캐시 기반, 우선순위):
        1) spot_pair_ctxs['BASE/QUOTE']의 midPx → markPx → prevDayPx
        2) spot_pair_prices['BASE/QUOTE'] (allMids로부터 받은 숫자)
        3) 페어가 BASE/USDC이면 spot_prices['BASE'] (allMids에서 받은 BASE 단가)
        """
        if not pair:
            return None
        p = str(pair).strip().upper()

        """
        # 1) webData2/3에서 온 페어 컨텍스트가 있으면 거기서 mid/mark/prev 순으로 사용
        ctx = self.spot_pair_ctxs.get(p)
        if isinstance(ctx, dict):
            for k in ("midPx", "markPx", "prevDayPx"):
                v = ctx.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        continue
        """

        # 2) allMids에서 유지하는 페어 가격 맵(숫자) 사용
        v = self.spot_pair_prices.get(p)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

        # 3) BASE/USDC인 경우 BASE 단가(spot_prices['BASE'])를 사용
        if p.endswith("/USDC") and "/" in p:
            base = p.split("/", 1)[0].strip().upper()
            v2 = self.spot_prices.get(base)
            if v2 is not None:
                try:
                    return float(v2)
                except Exception:
                    pass

        return None

    async def connect(self) -> bool:
        """WS 연결 (base class 429 대응 사용)"""
        return await super().connect()

    async def close(self) -> None:
        self._running = False
        self._stop.set()

        # BaseWSClient 패턴: 개별 task 취소
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()

        await self._safe_close(self._ws)
        self._ws = None

    # ==================== Abstract Method Implementations ====================

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """BaseWSClient 추상 메서드 구현 - 기존 _dispatch 호출"""
        self._dispatch(data)

    async def _resubscribe(self) -> None:
        """BaseWSClient 추상 메서드 구현 - 기존 resubscribe 호출"""
        await self.resubscribe()

    def _build_ping_message(self) -> Optional[str]:
        """BaseWSClient 추상 메서드 구현 - JSON ping"""
        return _json_dumps({"method": "ping"})

    async def subscribe(self) -> None:
        """
        단건 구독 전송(중복 방지): build_subscriptions() 결과를 _send_subscribe로 보냅니다.
        """
        if not self._ws:
            raise RuntimeError("WebSocket is not connected")

        subs = self.build_subscriptions()
        self._subscriptions = subs  # 재연결 시 재사용

        for sub in subs:
            await self._send_subscribe(sub)
            logger.info(f"SUB -> {_json_dumps({'method':'subscribe','subscription':sub})}")

    async def resubscribe(self) -> None:
        """
        재연결 시 누락 구독을 복원.
        - 핵심 가격 구독(self._subscriptions: allMids)
        - 사용자 스트림(_user_subs: allDexsClearinghouseState/spotState/openOrders)
        """
        if not self._ws:
            return

        # 0) 캐시된 데이터 초기화 (stale data 방지)
        # 유저 데이터 (재구독 시 새로운 데이터로 갱신됨)
        for u in list(self._user_subs):
            self._user_margin_by_dex[u] = {}
            self._user_positions_by_dex_norm[u] = {}
            self._user_positions_by_dex_raw[u] = {}
            self._user_open_orders[u] = []
            self._user_balances[u] = {}
            # 이벤트 초기화 (새 데이터 대기할 수 있도록)
            if u in self._open_orders_ready_by_user:
                self._open_orders_ready_by_user[u].clear()

        # 오더북 캐시 초기화
        self._orderbooks.clear()
        for ev in self._orderbook_events.values():
            ev.clear()

        # 가격 캐시 초기화
        self.prices.clear()
        self.spot_prices.clear()
        self.spot_pair_prices.clear()
        for ev in self._price_events.values():
            ev.clear()

        # 1) 클라이언트 측 중복 방지 셋 초기화
        self._active_subs.clear()

        # 2) 가격 채널(allMids) 재구독
        for sub in self._subscriptions or []:
            await self._send_subscribe(sub)
            logger.info(f"RESUB -> {_json_dumps({'method':'subscribe','subscription':sub})}")

        # 2-1) 동적으로 추가된 DEX allMids 재구독
        for dex in self._dynamic_dex_subs:
            if dex == "hl":
                sub = {"type": "allMids"}
            else:
                sub = {"type": "allMids", "dex": dex}
            await self._send_subscribe(sub)
            logger.info(f"RESUB(dex) -> {_json_dumps({'method':'subscribe','subscription':sub})}")

        # 3) 유저 스트림 재구독 (최소 필요 3종)
        for u in list(self._user_subs):
            for sub in (
                {"type": "allDexsClearinghouseState", "user": u},
                {"type": "spotState", "user": u},
                {"type": "openOrders", "user": u, "dex": "ALL_DEXS"},
            ):
                await self._send_subscribe(sub)
                logger.info(f"RESUB(user) -> {_json_dumps({'method':'subscribe','subscription':sub})}")

        # [ADDED] 4) 오더북 재구독 (count > 0인 것들만)
        for coin in list(self._orderbook_sub_counts.keys()):
            if self._orderbook_sub_counts.get(coin, 0) > 0:
                sub = {"type": "l2Book", "coin": coin, "nSigFigs": None}
                await self._send_subscribe(sub)
                logger.info(f"RESUB(orderbook) -> {_json_dumps({'method':'subscribe','subscription':sub})}")

    # ---------------------- 루프/콜백 ----------------------

    async def _recv_loop(self) -> None:
        """메시지 수신 루프 (BaseWSClient override)"""
        assert self._ws is not None
        ws = self._ws
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=WS_READ_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("recv timeout; forcing reconnect")
                await self._handle_disconnect()
                break
            except (ConnectionClosed, ConnectionClosedOK):
                logger.warning("ws closed; reconnecting")
                await self._handle_disconnect()
                break
            except Exception as e:
                logger.error(f"recv error: {e}", exc_info=True)
                await self._handle_disconnect()
                break

            # 서버 초기 문자열 핸드셰이크 처리
            if isinstance(raw, str) and raw == "Websocket connection established.":
                logger.debug(raw)
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                logger.debug(f"non-json message: {str(raw)[:200]}")
                continue

            try:
                self._dispatch(msg)
            except Exception:
                logger.exception("dispatch error")

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """
        서버 메시지 처리:
        - allMids: data = {'mids': { '<symbol or @pairIdx>': '<px_str>', ... } }
        - '@{pairIdx}'는 spotMeta.universe의 spotInfo.index로 매핑
        """
        ch = str(msg.get("channel") or msg.get("type") or "")
        if not ch:
            logger.debug(f"no channel key in message: {msg}")
            return

        if ch == "error":
            data_str = str(msg.get("data") or "")
            if "Already subscribed" in data_str:
                logger.debug(f"[WS info] {data_str}")
            else:
                logger.error(f"[WS error] {data_str}")
            return
        
        if ch == "pong":
            logger.debug("received pong")
            return

        # [ADDED] l2Book 채널 처리
        if ch == "l2Book":
            self._handle_l2book(msg)
            return
        
        if ch == "post":
            data = msg.get("data") or {}
            req_id = data.get("id")
            resp = data.get("response") or {}
            fut = self._post_waiters.get(int(req_id)) if isinstance(req_id, int) else None
            if fut and not fut.done():
                fut.set_result(resp)   # resp: {"type": "...", "payload": {... or str}}
            return
        
        if ch == "openOrders":
            data = msg.get("data") or {}
            u = str(data.get("user") or "").lower().strip()
            orders = data.get("orders") or []
            normalized = []
            for o in orders:
                no = self._normalize_open_order(o) if isinstance(o, dict) else None
                if no:
                    normalized.append(no)
            if u:
                self._user_open_orders[u] = normalized
                ev = self._open_orders_ready_by_user.get(u)
                if ev and not ev.is_set():
                    ev.set()
            return
        
        if ch == "allMids":
            data = msg.get("data") or {}
            
            if isinstance(data, dict) and isinstance(data.get("mids"), dict):
                mids: Dict[str, Any] = data["mids"]
                n_pair = n_pair_text = n_perp = 0

                for raw_key, raw_mid in mids.items():
                    # 1) '@{pairIdx}' → spotInfo.index
                    if isinstance(raw_key, str) and raw_key.startswith("@"):
                        try:
                            pair_idx = int(raw_key[1:])
                            px = float(raw_mid)
                        except Exception:
                            continue

                        pair_name = self.spot_asset_index_to_pair.get(pair_idx)   # 'BASE/QUOTE'
                        bq_tuple  = self.spot_asset_index_to_bq.get(pair_idx)     # (BASE, QUOTE)

                        if not pair_name or not bq_tuple:
                            # 페어 맵 미준비 → 보류
                            continue

                        base, quote = bq_tuple

                        # 1-1) 페어 가격 캐시
                        self.spot_pair_prices[pair_name] = px
                        self._notify_spot_pair(pair_name)
                        
                        # 1-2) 쿼트가 USDC인 경우 base 단일 가격도 채움
                        if quote == "USDC":
                            self.spot_prices[base] = px
                            self._notify_spot_base(base)
                        
                        n_pair += 1
                        continue

                    # 2) 텍스트 페어 'AAA/USDC' → pair 캐시, USDC 쿼트면 base 캐시
                    maybe_spot_base = _clean_spot_key_from_pair(raw_key)
                    if maybe_spot_base:
                        try:
                            px = float(raw_mid)
                        except Exception:
                            px = None
                        if px is not None:
                            pair_name = raw_key.strip().upper()
                            self.spot_pair_prices[pair_name] = px
                            self._notify_spot_pair(pair_name)

                            if pair_name.endswith("/USDC"):
                                self.spot_prices[maybe_spot_base] = px
                                self._notify_spot_base(maybe_spot_base)
                                
                        n_pair_text += 1
                        continue

                    # 3) Perp/기타 심볼
                    perp_key = _clean_coin_key_for_perp(raw_key)
                    if not perp_key:
                        continue
                    try:
                        px = float(raw_mid)
                    except Exception:
                        continue
                    self.prices[perp_key] = px
                    self._notify_perp(perp_key)
                    n_perp += 1

            return

        # 포지션(코인별)
        elif ch == "spotState":
            # 예시 구조: {'channel':'spotState','data':{'user': '0x...','spotState': {'balances': [...]}}}
            data = msg.get("data") or {}
            u = str(data.get("user") or "").lower().strip()
            balances = {}
            spot_balance = {} # balances는 기존 다른 코드들을 위해서 안건드림
            for b in (data.get("spotState") or {}).get("balances", []) or []:
                if not isinstance(b, dict):
                    continue
                try:
                    name = str(b.get("coin") or b.get("tokenName") or b.get("token") or "").upper()
                    total = float(b.get("total") or 0.0)
                    entry_ntl = float(b.get("entryNtl") or 0.0)
                    hold = float(b.get("hold") or 0.0)
                    available = total - hold
                    if name:
                        balances[name] = total
                        spot_balance[name] = {"total":total,
                                               "available":available,
                                               "locked":hold,
                                               "entry_ntl":entry_ntl,
                                               }
                except Exception:
                    continue
            balances['spot_balance'] = spot_balance
            if u:
                self._user_balances[u] = balances
            return

        # [ADDED] activeAssetData 채널 처리
        if ch == "activeAssetData":
            data = msg.get("data") or {}
            user = str(data.get("user") or "").lower().strip()
            coin = str(data.get("coin") or "").upper()
            if user and coin:
                key = (user, coin)
                self._active_asset_data[key] = data
                ev = self._active_asset_events.get(key)
                if ev and not ev.is_set():
                    ev.set()
            return

        # 통합 Perp 계정 상태
        if ch == "allDexsClearinghouseState":
            data = msg.get("data") or {}
            u = str(data.get("user") or "").lower().strip()
            ch_states = data.get("clearinghouseStates") or []
            if not u:
                return
            margin_by_dex: Dict[str, Dict[str, float]] = {}
            positions_norm_by_dex: Dict[str, Dict[str, Any]] = {}
            positions_raw_by_dex: Dict[str, List[Dict[str, Any]]] = {}
            for item in ch_states:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                dex_in, chs = item[0], (item[1] or {})
                dex_key = "hl" if (dex_in is None or str(dex_in).strip() == "") else str(dex_in).lower().strip()
                ms = chs.get("marginSummary") or {}
                def fnum(x, default=0.0):
                    try: return float(x)
                    except Exception: return default
                margin_by_dex[dex_key] = {
                    "accountValue": fnum(ms.get("accountValue")),
                    "totalNtlPos": fnum(ms.get("totalNtlPos")),
                    "totalRawUsd": fnum(ms.get("totalRawUsd")),
                    "totalMarginUsed": fnum(ms.get("totalMarginUsed")),
                    "crossMaintenanceMarginUsed": fnum(chs.get("crossMaintenanceMarginUsed")),
                    "withdrawable": fnum(chs.get("withdrawable")),
                    "time": chs.get("time"),
                }
                norm_map, raw_list = {}, []
                for ap in chs.get("assetPositions") or []:
                    pos = (ap or {}).get("position") or {}
                    if not pos: continue
                    raw_list.append(pos)
                    coin_raw = str(pos.get("coin") or "")
                    key_u = coin_raw.upper()
                    try:
                        norm = self._normalize_position(pos)
                        norm_map[key_u] = norm
                        if ":" in coin_raw:
                            norm_map[coin_raw] = norm
                    except Exception:
                        continue
                positions_norm_by_dex[dex_key] = norm_map
                positions_raw_by_dex[dex_key] = raw_list
            self._user_margin_by_dex[u] = margin_by_dex
            self._user_positions_by_dex_norm[u] = positions_norm_by_dex
            self._user_positions_by_dex_raw[u] = positions_raw_by_dex
            return

    async def _handle_disconnect(self) -> None:
        await self._safe_close_only()
        await self._reconnect_with_backoff()

    async def _safe_close_only(self) -> None:
        # 먼저 conn을 None으로 설정 (다른 코드가 죽은 소켓 사용 방지)
        old_conn = self._ws
        self._ws = None
        if old_conn:
            try:
                # 죽은 소켓에서 hang 방지를 위해 timeout 적용
                await asyncio.wait_for(old_conn.close(), timeout=2.0)
            except Exception:
                pass

    # _reconnect_with_backoff는 BaseWSClient에서 상속

    def get_price(self, symbol: str) -> Optional[float]:
        """Perp/일반 심볼 가격 조회(캐시)."""
        return self.prices.get(symbol.upper())

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Get mark price for symbol (alias for get_price)."""
        return self.get_price(symbol)

    def get_active_asset_data(self, coin: str, user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get cached activeAssetData for a coin."""
        u = (user or self.address or "").lower().strip()
        return self._active_asset_data.get((u, coin.upper()))

    # -------------------- [ADDED] Orderbook 기능 --------------------
    def _get_original_coin_name(self, coin_upper: str) -> str:
        """대문자 coin명 → 원본 coin명 (case-sensitive 대응)"""
        return self.perp_original_names.get(coin_upper, coin_upper)

    def _normalize_symbol_key(self, symbol: str) -> str:
        """
        심볼을 내부 캐시 키로 정규화 (원본 케이스 유지).
        - 'BASE/QUOTE' → 'BASE/QUOTE' (대문자)
        - 'dex:COIN' → 'dex:coin' (dex 소문자, coin 원본)
        - 'BTC' → 'BTC' (원본, kPEPE → kPEPE)
        """
        s = str(symbol).strip()
        if "/" in s:
            return s.upper()
        if ":" in s:
            parts = s.split(":", 1)
            coin_upper = parts[1].upper()
            original_coin = self._get_original_coin_name(coin_upper)
            return f"{parts[0].lower()}:{original_coin}"
        # 일반 Perp: 원본 이름 사용
        s_upper = s.upper()
        return self._get_original_coin_name(s_upper)

    def _resolve_coin_for_orderbook(self, symbol: str) -> str:
        """
        심볼 → WS 구독에 사용할 coin 키 반환 (원본 케이스 유지).
        - Spot 'BASE/QUOTE' → '@{pairIdx}'
        - Perp 'dex:COIN' → 'dex:coin' (dex 소문자, coin 원본)
        - Perp 'BTC' → 원본 (kPEPE 등)
        """
        s = str(symbol).strip()

        # 1) Spot 페어 'BASE/QUOTE' → '@{pairIdx}'
        if "/" in s:
            s_upper = s.upper()
            idx = self.spot_asset_index_to_pair
            for pair_idx, pair_name in idx.items():
                if pair_name.upper() == s_upper:
                    return f"@{pair_idx}"
            # 매핑 실패 → 그대로 반환(서버에서 에러 반환됨)
            return s_upper

        # 2) Perp 'dex:COIN' 형태 → 'dex:coin' (원본 케이스)
        if ":" in s:
            parts = s.split(":", 1)
            dex_part = parts[0].lower()
            coin_upper = parts[1].upper()
            original_coin = self._get_original_coin_name(coin_upper)
            return f"{dex_part}:{original_coin}"

        # 3) 일반 Perp → 원본 이름
        s_upper = s.upper()
        return self._get_original_coin_name(s_upper)

    async def subscribe_orderbook(self, symbol: str) -> None:
        """
        특정 심볼의 l2Book 구독 (레퍼런스 카운팅).
        - count가 0→1이 될 때만 실제 구독 메시지 전송
        - 구독 메시지: {"method":"subscribe","subscription":{"type":"l2Book","coin":"<coin>","nSigFigs":null}}
        """
        coin = self._resolve_coin_for_orderbook(symbol)
        norm_key = self._normalize_symbol_key(symbol)

        async with self._orderbook_sub_lock:
            current_count = self._orderbook_sub_counts.get(coin, 0)
            self._orderbook_sub_counts[coin] = current_count + 1

            # 대기 이벤트 준비 (lock 안에서)
            if norm_key not in self._orderbook_events:
                self._orderbook_events[norm_key] = asyncio.Event()

            # 첫 구독자일 때만 실제 구독 메시지 전송 (lock 유지)
            if current_count == 0:
                sub = {"type": "l2Book", "coin": coin, "nSigFigs": None}
                await self._send_subscribe(sub)

    async def unsubscribe_orderbook(self, symbol: str) -> bool:
        """
        l2Book 구독 해제 (레퍼런스 카운팅).
        - count가 1→0이 될 때만 실제 unsubscribe 메시지 전송
        - {"method":"unsubscribe","subscription":{"type":"l2Book","coin":"<coin>","nSigFigs":null}}
        """
        try:
            coin = self._resolve_coin_for_orderbook(symbol)
            norm_key = self._normalize_symbol_key(symbol)

            async with self._orderbook_sub_lock:
                current_count = self._orderbook_sub_counts.get(coin, 0)

                # 이미 구독 안 되어있으면 무시
                if current_count <= 0:
                    return True

                # 카운트 감소
                new_count = current_count - 1
                self._orderbook_sub_counts[coin] = new_count

                # 마지막 구독자가 아니면 여기서 종료
                if new_count > 0:
                    return True

                # 마지막 구독자: lock 유지한 상태에서 unsubscribe 처리
                logger.debug(f"[HyperliquidWS] Unsubscribe: l2Book/{coin}")
                unsub = {"type": "l2Book", "coin": coin, "nSigFigs": None}
                msg = {"method": "unsubscribe", "subscription": unsub}
                async with self._send_lock:
                    if self._ws:
                        await self._ws.send(_json_dumps(msg))
                    else:
                        logger.warning(f"unsubscribe_orderbook skipped (conn is None): {coin}")

                # 캐시/이벤트 정리 (lock 유지 상태)
                self._orderbooks.pop(norm_key, None)
                self._orderbook_events.pop(norm_key, None)
                sub_key = _sub_key(unsub)
                self._active_subs.discard(sub_key)
                self._orderbook_sub_counts.pop(coin, None)

            return True
        except Exception as e:
            logger.error(f"unsubscribe_orderbook error: {e}")
            logger.debug(f"unsubscribe_orderbook error: {e}")
            return False

    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        캐시된 오더북 반환.
        반환 형식: {"bids": [[price, size, n], ...], "asks": [[price, size, n], ...], "time": int}
        - bids: 가격 내림차순 (최고 매수가 먼저)
        - asks: 가격 오름차순 (최저 매도가 먼저)
        """
        norm_key = self._normalize_symbol_key(symbol)
        return self._orderbooks.get(norm_key)

    async def wait_orderbook_ready(self, symbol: str, timeout: float = 5.0) -> bool:
        """첫 오더북 스냅샷 대기."""
        norm_key = self._normalize_symbol_key(symbol)
        ev = self._orderbook_events.get(norm_key)
        if ev is None:
            ev = asyncio.Event()
            self._orderbook_events[norm_key] = ev
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _handle_l2book(self, msg: Dict[str, Any]) -> None:
        """
        l2Book 채널 메시지 처리.
        예시 data: {"coin": "@142", "time": 1767022392394, "levels": [[bids...], [asks...]]}
        levels[0] = bids (첫 번째가 최고 매수가), levels[1] = asks (첫 번째가 최저 매도가)
        """
        data = msg.get("data") or {}
        coin_raw = str(data.get("coin") or "")
        levels = data.get("levels") or []
        ts = data.get("time")

        if not coin_raw or len(levels) < 2:
            return

        # coin_raw → 정규화 키 (symbol)
        if coin_raw.startswith("@"):
            # Spot: '@{pairIdx}' → 'BASE/QUOTE'
            try:
                pair_idx = int(coin_raw[1:])
            except Exception:
                return
            pair_name = self.spot_asset_index_to_pair.get(pair_idx)
            if not pair_name:
                return
            norm_key = pair_name #.upper()
        elif ":" in coin_raw:
            # Perp with dex: 'hyna:BTC' → 'hyna:BTC' (dex 소문자, coin 대문자)
            parts = coin_raw.split(":", 1)
            norm_key = f"{parts[0].lower()}:{parts[1]}" #.upper()}"
        else:
            # 일반 Perp: 'BTC' → 'BTC'
            norm_key = coin_raw #.upper()

        # levels 파싱: [[{px, sz, n}, ...], [{px, sz, n}, ...]]
        def parse_level(lvl_list: List[Dict[str, Any]]) -> List[List]:
            result = []
            for item in lvl_list:
                try:
                    px = float(item.get("px") or 0)
                    sz = float(item.get("sz") or 0)
                    n = int(item.get("n") or 0)
                    result.append([px, sz, n])
                except Exception:
                    continue
            return result

        bids = parse_level(levels[0])
        asks = parse_level(levels[1])
        
        self._orderbooks[norm_key] = {
            "bids": bids,
            "asks": asks,
            "time": ts,
        }

        # 이벤트 시그널
        ev = self._orderbook_events.get(norm_key)
        if ev and not ev.is_set():
            ev.set()

class HLWSClientPool:
    USER_SUB_LIMIT = 10  # comment: 한 WS 소켓당 허용할 유저 스트림 수 제한
    """
    (ws_url, address) 단위로 HLWSClientRaw를 1개만 생성/공유하는 풀.
    - 동일 주소에서 다중 DEX allMids는 하나의 커넥션에서 추가 구독한다.
    - address가 None/""이면 '가격 전용' 공유 커넥션으로 취급(유저 스트림 없음).
    """
    def __init__(self) -> None:
        # 단일 소켓 리스트로 단순화
        self._sockets: List[HLWSClientRaw] = []                  # comment: 열린 WS 소켓들
        self._addr_to_socket: Dict[str, HLWSClientRaw] = {}      # comment: address(lower) → socket
        self._refcnt_by_socket: Dict[HLWSClientRaw, int] = {}    # comment: socket 별 참조 카운트

        # 단일 락(같은 ws_url만 사용한다는 전제)
        self._lock = asyncio.Lock()

        # 연결 직렬화용 세마포어(동시에 1개만 connect)
        self._connect_sema = asyncio.Semaphore(1)  # comment: 너무 빠른 동시 연결 → 429 예방

        # 공유 메타
        self._shared_lock = asyncio.Lock()
        self._shared_primed: bool = False
        self._shared_dex_order: List[str] = ["hl"]
        self._shared_spot_idx2name: Dict[int, str] = {}
        self._shared_spot_name2idx: Dict[str, int] = {}
        self._shared_spot_pair_by_index: Dict[int, str] = {}
        self._shared_spot_bq_by_index: Dict[int, tuple[str, str]] = {}

    # ---------------- 공유 메타 ----------------
    async def prime_shared_meta(self, *, dex_order=None, idx2name=None, name2idx=None, pair_by_index=None, bq_by_index=None) -> None:
        async with self._shared_lock:
            if self._shared_primed:
                return
            ks, seen = [], set()
            for k in (dex_order or ["hl"]):
                kk = str(k).lower().strip()
                if kk and kk not in seen:
                    ks.append(kk); seen.add(kk)
            self._shared_dex_order = ks or ["hl"]
            self._shared_spot_idx2name = dict(idx2name or {})
            self._shared_spot_name2idx = {str(k).upper(): int(v) for k, v in (name2idx or {}).items()}
            self._shared_spot_pair_by_index = dict(pair_by_index or {})
            self._shared_spot_bq_by_index = dict(bq_by_index or {})
            self._shared_primed = True

    def _apply_shared_to_socket_unlocked(self, c: HLWSClientRaw) -> None:
        c.set_spot_meta(
            self._shared_spot_idx2name,
            self._shared_spot_name2idx,
            self._shared_spot_pair_by_index,
            self._shared_spot_bq_by_index,
        )

    # ---------------- 소켓 선택/생성 ----------------
    def _pick_socket_for_address(self, address: Optional[str]) -> Optional[HLWSClientRaw]:
        """
        - 이미 배정된 주소면 해당 소켓 반환
        - 아니면 현재 소켓 중 user_count < LIMIT 인 첫 소켓 반환
        - 없으면 None(→ 새 소켓 생성)
        """
        a = (address or "").lower().strip()
        if a and a in self._addr_to_socket:
            return self._addr_to_socket[a]
        for s in self._sockets:
            if s.user_count() < self.USER_SUB_LIMIT:
                return s
        return None

    # ---------------- 퍼블릭 API ----------------
    async def acquire(
        self,
        *,
        address: Optional[str],
        dex: Optional[str] = None,
        dex_order: Optional[List[str]] = None,
        idx2name: Optional[Dict[int, str]] = None,
        name2idx: Optional[Dict[str, int]] = None,
        pair_by_index: Optional[Dict[int, str]] = None,
        bq_by_index: Optional[Dict[int, Tuple[str, str]]] = None,
    ) -> HLWSClientRaw:
        # 공유 메타 1회 주입
        await self.prime_shared_meta(
            dex_order=dex_order,
            idx2name=idx2name,
            name2idx=name2idx,
            pair_by_index=pair_by_index,
            bq_by_index=bq_by_index,
        )

        async with self._lock:
            sock = self._pick_socket_for_address(address)
            new_socket = False
            if sock is None:
                sock = HLWSClientRaw(
                    dex=None,
                    address=None,
                )
                async with self._shared_lock:
                    self._apply_shared_to_socket_unlocked(sock)
                new_socket = True

            # refcnt++
            self._refcnt_by_socket[sock] = self._refcnt_by_socket.get(sock, 0) + 1
            if new_socket:
                self._sockets.append(sock)

        # 새 소켓을 실제로 연결하는 구간은 직렬화
        if new_socket:
            async with self._connect_sema:
                await sock.ensure_connected_and_subscribed()
                sock._ready.set()  # 연결 완료 시그널
                # 소량의 간격(버스트 완화)
                await asyncio.sleep(0.2)
        else:
            # 기존 소켓: 연결 완료 대기 (다른 코루틴이 연결 중일 수 있음)
            await asyncio.wait_for(sock._ready.wait(), timeout=30.0)

        # 가격 구독/유저 구독
        await sock.ensure_allmids_for(dex)
        if address:
            addr_l = address.lower().strip()
            await sock.ensure_user_streams(addr_l)
            self._addr_to_socket[addr_l] = sock

        return sock

    async def release(
        self,
        *,
        address: Optional[str] = None,
        client: Optional[HLWSClientRaw] = None,
        force_close: bool = False
    ) -> None:
        """
        - client를 명시하면 그 소켓을 해제(권장)
        - 아니면 address 매핑으로 소켓을 찾아 해제
        - force_close=True면 참조 카운트 무시하고 즉시 종료
        """
        async with self._lock:
            target: Optional[HLWSClientRaw] = client
            addr_l = (address or "").lower().strip()
            if target is None and addr_l and addr_l in self._addr_to_socket:
                target = self._addr_to_socket.pop(addr_l, None)

            if target is None:
                # 등록된 첫 소켓(있다면)으로 대체(비권장 경로)
                target = self._sockets[0] if self._sockets else None
                if target is None:
                    return

            if force_close:
                # 강제 종료: 참조 카운트 무시
                try:
                    await target.close()
                    logger.debug(f"[HLWSPool] Force closed: {addr_l[:10] if addr_l else 'public'}...")
                finally:
                    self._refcnt_by_socket.pop(target, None)
                    try:
                        self._sockets.remove(target)
                    except ValueError:
                        pass
            else:
                # 일반 해제: 참조 카운트 감소
                self._refcnt_by_socket[target] = max(0, self._refcnt_by_socket.get(target, 1) - 1)
                if self._refcnt_by_socket[target] == 0:
                    try:
                        await target.close()
                    finally:
                        self._refcnt_by_socket.pop(target, None)
                        try:
                            self._sockets.remove(target)
                        except ValueError:
                            pass

WS_POOL = HLWSClientPool()