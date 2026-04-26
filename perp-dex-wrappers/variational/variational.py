from mpdex.base import MultiPerpDex, MultiPerpDexMixin
import asyncio
from typing import Optional, Dict, Any, List, Tuple
import os
import json
from pathlib import Path
from curl_cffi import requests as curl_requests
from eth_utils import to_checksum_address
from .variational_auth import VariationalAuth
import time

BASE_URL = "https://omni.variational.io"

# 엔드포인트 매핑(고정)
ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "create_limit_order": ("POST", "/api/orders/new/limit"),
    "create_market_order": ("POST", "/api/orders/new/market"),
    "fetch_position": ("GET", "/api/positions"),
    "fetch_open_positions": ("GET", "/api/orders/v2?status=pending"),
    "get_collateral": ("GET", "/api/settlement_pools/details"), # cookie 유효성 검증 겸용
    "supported_assets": ("GET", "/api/metadata/supported_assets"),
    "indicative_quote": ("POST", "/api/quotes/indicative"),
    "cancel_order": ("POST", "/api/orders/cancel"),
    "logout": ("GET", "/api/auth/logout"),
}


# 숫자 변환 유틸
def _fnum(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

# 입력 cookie dict에서 vr-token 값만 추출
def _extract_vr_token_from_cookies(cookies: Optional[Dict[str, str]]) -> Optional[str]:
    """
    입력 dict에서 vr-token 값을 추출한다.
    - 키는 대소문자 무시, '_'는 '-'로 정규화한다. (예: VR_TOKEN, Vr-Token 등)
    - 값이 문자열이면 strip 후 빈 문자열이면 무시한다.
    """
    # 선행 _has_valid_cookies 의존 제거: dict 검증부터 수행
    if not isinstance(cookies, dict) or not cookies:
        return None

    for k, v in cookies.items():
        if not isinstance(k, str):
            continue
        key = k.strip().lower().replace("_", "-")
        if key == "vr-token" and v is not None:
            # 값 정제
            if isinstance(v, str):
                vv = v.strip()
                if vv:
                    return vv
                # 공백 문자열은 무효로 간주하고 계속 탐색
            else:
                # 문자열이 아니더라도 truthy 면 사용(일반적이지 않음)
                return v
    return None
    
# vr-token 로더(.cache/variational_session_{address}.json → 없으면 홈 폴백)
def _load_vr_token_from_cache(address: str) -> Optional[str]:
    if not address:
        return None
    addr = address.lower()
    if not addr.startswith("0x"):
        addr = f"0x{addr}"

    candidates = [
        Path.cwd() / ".cache" / f"variational_session_{addr}.json" #,
        #Path.home() / ".cache" / "mpdex" / f"variational_session_{addr}.json",
    ]
    for p in candidates:
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data.get("cookie_vr_token") or data.get("token")
        except Exception:
            continue
    return None


# indicative quote 정제
def _extract_indicative_core(resp: dict) -> dict:
    if not isinstance(resp, dict):
        raise ValueError("indicative quote 응답은 dict(JSON)이어야 합니다.")

    instr = resp.get("instrument") or {}
    mr = resp.get("margin_requirements") or {}
    ql = resp.get("qty_limits") or {}

    core = {
        "instrument": {
            "instrument_type": instr.get("instrument_type"),
            "underlying": instr.get("underlying"),
            "funding_interval_s": instr.get("funding_interval_s"),
            "settlement_asset": instr.get("settlement_asset"),
        },
        "qty": resp.get("qty"),
        "bid": _fnum(resp.get("bid")),
        "ask": _fnum(resp.get("ask")),
        "mark_price": _fnum(resp.get("mark_price")),
        "index_price": _fnum(resp.get("index_price")),
        "quote_id": resp.get("quote_id"),
        "margins": {
            "existing": {
                "initial_margin": _fnum((mr.get("existing_margin") or {}).get("initial_margin")),
                "maintenance_margin": _fnum((mr.get("existing_margin") or {}).get("maintenance_margin")),
            },
            "delta_bid": {
                "initial_margin": _fnum((mr.get("bid_margin_delta") or {}).get("initial_margin")),
                "maintenance_margin": _fnum((mr.get("bid_margin_delta") or {}).get("maintenance_margin")),
            },
            "delta_ask": {
                "initial_margin": _fnum((mr.get("ask_margin_delta") or {}).get("initial_margin")),
                "maintenance_margin": _fnum((mr.get("ask_margin_delta") or {}).get("maintenance_margin")),
            },
            "max_notional_bid": _fnum(mr.get("bid_max_notional_delta")),
            "max_notional_ask": _fnum(mr.get("ask_max_notional_delta")),
            "estimated_fees_bid": _fnum(mr.get("estimated_fees_bid")),
            "estimated_fees_ask": _fnum(mr.get("estimated_fees_ask")),
        },
        "qty_limits": {
            "bid": {
                "min_qty_tick": _fnum((ql.get("bid") or {}).get("min_qty_tick")),
                "min_qty": _fnum((ql.get("bid") or {}).get("min_qty")),
                "max_qty": _fnum((ql.get("bid") or {}).get("max_qty")),
            },
            "ask": {
                "min_qty_tick": _fnum((ql.get("ask") or {}).get("min_qty_tick")),
                "min_qty": _fnum((ql.get("ask") or {}).get("min_qty")),
                "max_qty": _fnum((ql.get("ask") or {}).get("max_qty")),
            },
        },
    }

    bid, ask = core["bid"], core["ask"]
    mid = spread = spread_bps = None
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread = ask - bid
        spread_bps = (spread / mid) * 1e4 if mid else None

    core["derived"] = {"mid_price": mid, "spread": spread, "spread_bps": spread_bps}
    return core


# 포지션 요약 정제(특정 코인)
def _extract_position_for_coin(positions, coin: str) -> Optional[dict]:
    if isinstance(positions, str):
        try:
            positions = json.loads(positions)
        except Exception:
            return None
    if not positions:
        return None

    sym = (coin or "").upper()
    items = positions if isinstance(positions, list) else [positions]
    for p in items:
        try:
            info = (p.get("position_info") or {})
            inst = (info.get("instrument") or {})
            if (inst.get("underlying") or "").upper() != sym:
                continue
            qty = _fnum(info.get("qty"))
            side = "long" if (qty or 0) > 0 else ("short" if (qty or 0) < 0 else "flat")
            return {
                "symbol": inst.get("underlying"),
                "side": side,
                "size": str(abs(qty)) if qty is not None else None,
                "entry_price": _fnum(info.get("avg_entry_price")),
                "unrealized_pnl": p.get("upnl", 0),
                "liquidation_price": p.get("estimated_liquidation_price", None),
                "raw_data": p,
            }
        except Exception:
            continue
    return None


# 오픈 주문 요약 정제(필터: coin 또는 "all")
def _extract_open_orders_core(payload, coin: str = "all") -> List[dict]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        items = payload.get("result") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []

    out: List[dict] = []
    for o in items:
        if not isinstance(o, dict):
            continue
        inst = (o.get("instrument") or {})
        order_type = (o.get("order_type") or "").lower()

        price = None
        if order_type == "limit":
            price = _fnum(o.get("limit_price"))
        if price is None:
            price = _fnum(o.get("price"))
        if price is None:
            price = _fnum(o.get("mark_price"))

        if coin.lower() == "all" or (inst.get("underlying") or "").upper() == coin.upper():
            out.append(
                {
                    "id": o.get("order_id"),
                    "coin": inst.get("underlying"),
                    "side": (o.get("side") or "").lower() or None,
                    "order_type": order_type or None,
                    "status": (o.get("status") or "").lower() or None,
                    "size": _fnum(o.get("qty")),
                    "price": price,
                    "rfq_id": o.get("rfq_id"),
                }
            )
    return out

class VariationalExchange(MultiPerpDexMixin, MultiPerpDex):
    def __init__(
        self,
        evm_wallet_address: str = None, # required
        session_cookies: Optional[Dict[str, str]] = None, # 1) cache있으면 사용, 2) cache 안될 경우 sign login OR webloging
        evm_private_key: Optional[str] = None, # 필수 아님
        options: Any = None, # options # 다른 프로그램에서 사용할 경우 대비
    ):
        super().__init__()
        if not evm_wallet_address:
            raise ValueError("evm_wallet_address is required")
        self.is_rfq = True
        self.address = to_checksum_address(evm_wallet_address)
        self._pk = evm_private_key
        self.options = options or {}
        self.options.setdefault("probe_qty", "0.0001") # any qty is ok
        self.options.setdefault("max_slippage", 0.01)  # 1%
        self.options.setdefault("min_price_refresh_ms", 500)  # 최소 
        self.options.setdefault("auto_login_on_demand", True)  # 자동 로그인 허용 플래그
        self.options.setdefault("funding_interval_s", 3600) # 3600 으로 강제됨, 처음 받는 response와 달리 항시 3600
        self._impersonate = self.options.get("impersonate", "chrome")
        self._timeout = float(self.options.get("timeout", 10.0))
        self.session_cookies = session_cookies
        # 로그인 도우미
        self._auth = VariationalAuth(wallet_address=self.address, evm_private_key=self._pk, session_cookies=self.session_cookies)
        # 런타임 캐시
        #   self._rt_cache[coin.upper()] = {
        #     "instrument": {...},
        #     "quote_id": str|None, # not necessary
        #     "mark_price": float|None,
        #     "qty": str|None,
        #     "funding_interval_s": int|None,
        #   }
        self._rt_cache: Dict[str, Dict[str, Any]] = {}
        self._asset_list: List[str] = [] # 지원 코인 리스트 캐시
        self._initialized: bool = False # 초기화 완료 플래그

        # 세션 상태(초기화/로그인 이후에는 캐시만 사용)
        self._session_ready: bool = False
        self._vr_token: Optional[str] = None  # 메모리 보관
        self._last_viewed_coin: str = "BTC"  # referer용 기본 코인 (get_orderbook 호출 시 업데이트)

    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USD'

    async def _probe_cookie_valid(self, vr_token: str) -> bool:
        if not vr_token:
            return False
        method, path = ENDPOINTS["get_collateral"]
        url = BASE_URL + path
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "vr-connected-address": self.address,
        }
        cookies = {"vr-token": vr_token}
        async with curl_requests.AsyncSession(impersonate=self._impersonate, timeout=self._timeout) as s:
            r = await s.get(url, headers=headers, cookies=cookies)
            return int(r.status_code) == 200

    async def login(
        self,
        *,
        address: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        persist_to_cache: bool = False,
        allow_auto: bool = False,
    ) -> Dict[str, Any]:
        """
        - cookies(vr-token)가 주어지면 /on_chain/usdc/balance 프로빙으로 유효성 확인 후 세션 채택.
        - cookies 미지정 시 .cache의 vr-token을 프로빙. OK면 채택.
        - allow_auto=True면 VariationalAuth.login() 자동 로그인 시도 후 캐시 로드.
        - persist_to_cache=True면 입력 vr-token을 .cache에 저장.
        """
        addr = address or self.address
        if to_checksum_address(addr) != to_checksum_address(self.address):
            raise ValueError("login(): 입력 address가 인스턴스 주소와 다릅니다.")

        # 1) 입력 쿠키 우선
        candidate = _extract_vr_token_from_cookies(cookies) if cookies else None
        if candidate and await self._probe_cookie_valid(candidate):
            self.session_cookies = {"vr-token": candidate}
            self._vr_token = candidate
            self._session_ready = True
            if persist_to_cache:
                self._auth._cookie_vr_token = candidate  # noqa: SLF001
                self._auth._token = None  # noqa: SLF001
                self._auth.save_cached_session()
            return {"ok": True, "source": "input_cookies"}

        # 2) 캐시
        cached = _load_vr_token_from_cache(self.address)
        if cached and await self._probe_cookie_valid(cached):
            self.session_cookies = {"vr-token": cached}
            self._vr_token = cached
            self._session_ready = True
            return {"ok": True, "source": "cache"}

        # 3) 자동 로그인
        if allow_auto:
            if self._pk:
                print("지갑 프라이빗키로 자동로그인 진행.")
                await self._auth.login(port=None)
            else:
                print("로그인이 필요합니다. 아래 나오는 로컬서버를 열고 지갑 서명을 진행하쇼.")
                port = int(self.options.get("login_port", 7469))
                await self._auth.login(port=port, open_browser=True)
            new_vr = _load_vr_token_from_cache(self.address)
            if new_vr and await self._probe_cookie_valid(new_vr):
                self.session_cookies = {"vr-token": new_vr}
                self._vr_token = new_vr
                self._session_ready = True
                return {"ok": True, "source": "auto_login"}
            return {"ok": False, "error": "auto_login_failed"}
        return {"ok": False, "error": "no_valid_cookie_and_auto_disabled"}

    async def verify_session(self) -> bool:
        vr = self._vr_token or _extract_vr_token_from_cookies(self.session_cookies) or _load_vr_token_from_cache(self.address)
        return await self._probe_cookie_valid(vr) if vr else False

    # Public: 로그아웃(세션 초기화, 캐시 제거 옵션)
    # not true logout, server doesnot delete vr-token until expiration
    async def logout(self, *, clear_cache: bool = False) -> None:
        """
        GET /api/auth/logout
        - headers: vr-connected-wallet: <address>  (서비스 명세에 따라 이 헤더 사용)
        - cookies: vr-token
        성공 시 {"message":"SUCCESS"} 기대, 응답 쿠키의 vr-token은 빈 값.
        """
        vr = self._vr_token or _extract_vr_token_from_cookies(self.session_cookies) or _load_vr_token_from_cache(self.address)
        if not vr:
            # 로컬만 정리
            self.session_cookies = {}
            self._vr_token = None
            self._session_ready = False
            if clear_cache:
                try:
                    self._auth.clear_cached_session()
                except Exception:
                    pass
            return {"ok": True, "remote": False, "message": "no session; local cleared"}

        method, path = ENDPOINTS["logout"]
        url = BASE_URL + path
        headers = {
            "accept": "*/*",
            "vr-connected-address": self.address,
        }
        
        cookies = {"vr-token": vr}
        async with curl_requests.AsyncSession(impersonate=self._impersonate, timeout=self._timeout) as s:
            r = await s.get(url, headers=headers, cookies=cookies)
            ok = False
            msg = None
            try:
                body = r.json()
                msg = body.get("message")
                ok = (r.status_code == 200 and msg == "SUCCESS")
            except Exception:
                ok = (r.status_code == 200)
            # 로컬 세션 정리
            self.session_cookies = {}
            self._vr_token = None
            self._session_ready = False
            if clear_cache:
                try:
                    self._auth.clear_cached_session()
                except Exception:
                    pass
            return {"ok": ok, "remote": True, "status": r.status_code, "message": msg}

    # ---------------------------
    # 내부: 헤더/쿠키 확보(없으면 로그인 시도)
    # ---------------------------
    async def _headers_and_cookies(self, coin: Optional[str] = None) -> Tuple[dict, dict]:
        """
        initialize() 또는 명시적 login() 이후에는 self._vr_token(또는 .cache)만 사용.
        이 경로에서는 더 이상 원격 프로빙/로그인을 수행하지 않는다.
        """
        if self._vr_token:
            vr = self._vr_token
        else:
            vr = _load_vr_token_from_cache(self.address)
            if not vr:
                if not self._initialized and self.options.get("auto_login_on_demand", True):
                    # 아직 초기화가 안됐으면 initialize에서 로그인 수행 시도
                    await self.initialize()
                    vr = self._vr_token or _load_vr_token_from_cache(self.address)
                if not vr:
                    raise RuntimeError("유효한 세션(vr-token)이 없습니다. 먼저 initialize() 또는 login()을 호출하세요.")

        # referer: coin이 있으면 해당 coin, 없으면 _last_viewed_coin (기본 BTC)
        referer_coin = coin.upper() if coin else self._last_viewed_coin
        referer = f"https://omni.variational.io/perpetual/{referer_coin}"
        #print(referer)
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "vr-connected-address": self.address,
            "origin": "https://omni.variational.io",
            "referer": referer,
        }
        cookies = {"vr-token": vr}
        return headers, cookies

    # ---------------------------
    # 내부: HTTP 호출
    # ---------------------------
    async def _request(self, method: str, path: str, *, params=None, json_body=None, coin: Optional[str] = None) -> Any:
        headers, cookies = await self._headers_and_cookies(coin=coin)
        url = BASE_URL + path
        async with curl_requests.AsyncSession(impersonate=self._impersonate, timeout=self._timeout) as s:
            if method.upper() == "GET":
                r = await s.get(url, params=params, headers=headers, cookies=cookies)
            elif method.upper() == "POST":
                r = await s.post(url, json=json_body, headers=headers, cookies=cookies)
            elif method.upper() == "PUT":
                r = await s.put(url, json=json_body, headers=headers, cookies=cookies)
            else:
                r = await s.request(method.upper(), url, params=params, json=json_body, headers=headers, cookies=cookies)
            r.raise_for_status()
            ct = (r.headers or {}).get("content-type", "")
            try:
                return r.json() if "application/json" in ct else r.text
            except Exception:
                return r.text
    
    # ---------------------------
    # 내부: 런타임 캐시 유틸
    # ---------------------------
    def _cache_update_from_core(self, coin: str, core: dict) -> None:
        """
        indicative core에서 instrument/quote_id/mark_price/qty를 런타임 캐시에 반영
        """
        if not isinstance(core, dict):
            return
        inst = core.get("instrument") or {}
        now_ms = int(time.monotonic() * 1000)
        entry = {
            "instrument": inst,
            "bid":core.get('bid'),
            "ask":core.get('ask'),
            "quote_id": core.get("quote_id"),
            "mark_price": core.get("mark_price"),
            "qty": core.get("qty"),
            "funding_interval_s": inst.get("funding_interval_s"),
            "last_price_at_ms": now_ms,
        }
        
        self._rt_cache[coin.upper()] = entry

    def _get_cached_instrument(self, coin: str, funding_interval_s: Optional[int] = None) -> Optional[dict]:
        entry = self._rt_cache.get(coin.upper())
        if not entry:
            return None
        inst = entry.get("instrument")
        if not isinstance(inst, dict):
            return None
        if funding_interval_s is not None:
            if int(entry.get("funding_interval_s") or 0) != int(funding_interval_s):
                return None
        return inst
    
    async def init(self):
        res = await self.initialize() # login and initialize
        if res.get('ok'):
            return self
        else:
            raise RuntimeError(f"init 실패: {res}")

    async def initialize(self) -> dict:
        """
        순서:
          1) login(cookies=self.session_cookies, allow_auto=True)로 유효 세션 확보(프로빙은 여기서만 수행)
          2) supported_assets 호출
          3) has_perp=True && !is_close_only_mode 심볼만 대상으로 instrument/price(seed) 캐시
        """
        if self._initialized:
            return {"ok": True, "assets": list(self._asset_list), "seeded": len(self._rt_cache)}

        # 1) 로그인(여기서만 원격 프로빙/자동 로그인 수행)
        login_res = await self.login(cookies=self.session_cookies, allow_auto=True, persist_to_cache=False)
        if not login_res.get("ok"):
            raise RuntimeError(f"initialize: login 실패: {login_res}")

        # 세션 토큰 메모리 고정
        self._vr_token = _extract_vr_token_from_cookies(self.session_cookies) or _load_vr_token_from_cache(self.address)
        self._session_ready = True

        # 2) supported_assets
        method, path = ENDPOINTS["supported_assets"]
        raw = await self._request(method, path)
        data = raw if isinstance(raw, dict) else json.loads(str(raw))

        # 3) 시드 캐시 구성
        self._asset_list = []
        self.available_symbols["perp"] = []
        now_ms = int(time.monotonic() * 1000)
        for sym, items in data.items():
            if not isinstance(items, list) or not items:
                continue
            it = items[0] if isinstance(items[0], dict) else None
            if not it:
                continue
            if not it.get("has_perp", False):
                continue
            if it.get("is_close_only_mode", False):
                continue
            
            coin = str(it.get("asset") or sym).upper()
            
            # 현재는 3600으로 강제하고 있음
            #funding_interval_s = int(it.get("funding_interval_s", 3600))
            funding_interval_s = self.options.get("funding_interval_s")

            price = _fnum(it.get("price"))

            instrument = {
                "instrument_type": "perpetual_future",
                "underlying": coin,
                "funding_interval_s": funding_interval_s,
                "settlement_asset": "USDC",
            }
            self._rt_cache[coin] = {
                "instrument": instrument,
                "quote_id": None,
                "mark_price": price,
                "qty": None,
                "funding_interval_s": funding_interval_s,
                "last_price_at_ms": now_ms,
            }
            self._asset_list.append(coin)
            self.available_symbols["perp"].append(coin)

        self._asset_list.sort()
        self._initialized = True
        return {"ok": True, "assets": list(self._asset_list), "seeded": len(self._rt_cache)}
    
    # ---------------------------
    # 내부: API별 래퍼
    # ---------------------------
    async def _fetch_indicative_quote(self, coin: str, qty: str | float, funding_interval_s: int = 3600) -> dict:
        method, path = ENDPOINTS["indicative_quote"]
        payload = {
            "instrument": {
                "underlying": str(coin),
                "funding_interval_s": int(funding_interval_s),
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_future",
            },
            "qty": str(qty),
        }
        data = await self._request(method, path, json_body=payload, coin=coin)
        core = _extract_indicative_core(data if isinstance(data, dict) else json.loads(str(data)))
        self._cache_update_from_core(coin, core)  # [ADDED] 런타임 캐시 반영
        return core

    async def _create_market_order(
        self,
        *,
        coin: str,
        side: str,
        quote_id: str,
        max_slippage: float = 0.01, # default to 1%
        is_reduce_only: bool = False,
    ):
        if not quote_id:
            raise ValueError("quote_id가 비어있습니다.")
        method, path = ENDPOINTS["create_market_order"]
        payload = {"quote_id": quote_id, "side": side.lower(), "max_slippage": max_slippage, "is_reduce_only": is_reduce_only}
        return await self._request(method, path, json_body=payload, coin=coin)

    async def _create_limit_order(
        self,
        *,
        coin: str,
        side: str,
        qty: str | float,
        price: str | float,
        instrument: dict,
        is_reduce_only: bool = False,
        is_auto_resize: bool = False,
        use_mark_price: bool = False,
    ):
        method, path = ENDPOINTS["create_limit_order"]
        payload = {
            "order_type": "limit",
            "limit_price": price,
            "side": side.lower(),
            "instrument": instrument,
            "qty": str(qty),
            "is_auto_resize": bool(is_auto_resize),
            "use_mark_price": bool(use_mark_price),
            "is_reduce_only": bool(is_reduce_only),
        }
        return await self._request(method, path, json_body=payload, coin=coin)
    
    async def _fetch_positions_all(self):
        method, path = ENDPOINTS["fetch_position"]
        return await self._request(method, path)

    async def _fetch_open_orders_raw(self):
        method, path = ENDPOINTS["fetch_open_positions"]
        return await self._request(method, path)

    async def _cancel_order(self, rfq_id: str) -> bool:
        if not rfq_id:
            return False
        method, path = ENDPOINTS["cancel_order"]
        resp = await self._request(method, path, json_body={"rfq_id": rfq_id})
        # 취소 응답이 비어있을 수 있음 → status 200이면 True로 간주
        return True if resp is not None else True

    async def _get_collateral_raw(self):
        method, path = ENDPOINTS["get_collateral"]
        return await self._request(method, path)

    async def _supported_assets_raw(self):
        method, path = ENDPOINTS["supported_assets"]
        return await self._request(method, path)
    
    # ---------------------------
    # Public API
    # ---------------------------
    async def initialize_if_needed(self) -> None:
        """
        idempotent: 이미 초기화되어 있으면 아무 것도 하지 않음
        """
        if not self._initialized:
            await self.initialize()

    async def fetch_price(
        self,
        symbol: str,
        *,
        force_refresh: bool = True,
        min_refresh_ms: Optional[int] = None,
    ) -> Optional[float]:
        
        await self.initialize_if_needed()
        coin = symbol.upper()
        cached = self._rt_cache.get(coin)

        now_ms = int(time.monotonic() * 1000)
        thresh_ms = int(min_refresh_ms if min_refresh_ms is not None else self.options.get("min_price_refresh_ms", 250))

        # throttle: 마지막 갱신 후 thresh_ms 이내면 캐시 우선 반환
        last_ms = int((cached or {}).get("last_price_at_ms") or 0)
        if cached and last_ms and (now_ms - last_ms) < thresh_ms:
            return cached.get("mark_price")

        probe_qty = self.options.get("probe_qty", "0.0001")
        funding = int((cached or {}).get("funding_interval_s") or self.options.get("funding_interval_s", 3600))

        # 강제 갱신 또는 캐시 부재 → 시도
        if force_refresh or (not cached or cached.get("mark_price") is None):
            try:
                core = await self._fetch_indicative_quote(coin=coin, qty=probe_qty, funding_interval_s=funding)
                
                price = core.get("mark_price")
                # [ADDED] 성공 시 타임스탬프 갱신(만약 내부에서 갱신 못했을 경우 보강)
                self._rt_cache.setdefault(coin, {}).update({"mark_price": price, "last_price_at_ms": now_ms})
                return price
            except Exception:
                # 실패 시 캐시 fallback
                if cached and (cached.get("mark_price") is not None):
                    return cached.get("mark_price")
                return None
            
        # 강제 갱신이 아니고 캐시가 있으면 캐시 반환
        if cached and (cached.get("mark_price") is not None):
            return cached.get("mark_price")

        # 캐시도 없으면 최후 시도
        try:
            core = await self._fetch_indicative_quote(coin=coin, qty=probe_qty, funding_interval_s=funding)
            price = core.get("mark_price")
            self._rt_cache.setdefault(coin, {}).update({"mark_price": price, "last_price_at_ms": now_ms})
            return price
        except Exception:
            return None
    
    async def create_order(self, symbol, side, amount, price=None, order_type="market", *, is_reduce_only=False):
        """
        - market: indicative quote → quote_id 획득(캐시 갱신) → create_market_order(순차)
        - limit: 캐시된 instrument 사용. 없으면 해당 시점에 indicative 호출로 instrument를 캐시 → create_limit_order
        """
        await self.initialize_if_needed()
        coin = str(symbol).upper()
        side = (side or "buy").lower()

        # 주문 시 해당 coin을 기본 referer coin으로 설정
        self._last_viewed_coin = coin

        # limit: instrument 필요 → 캐시 우선
        if price is not None or order_type == "limit":
            cached = self._rt_cache.get(coin)
            instrument = (cached or {}).get("instrument")
            if not instrument:
                # seed 캐시가 없다면 probe로 보충
                funding = int(self.options.get("funding_interval_s", 3600))
                core = await self._fetch_indicative_quote(coin=coin, qty=str(amount), funding_interval_s=funding)
                instrument = core.get("instrument")
            res = await self._create_limit_order(
                coin=coin,
                side=side,
                qty=str(amount),
                price=str(price),
                instrument=instrument,
                #is_reduce_only=bool(self.options.get("reduce_only", False)),
                #is_auto_resize=bool(self.options.get("is_auto_resize", False)),
                #use_mark_price=bool(self.options.get("use_mark_price", False)),
            )
            return res.get('rfq_id')

        # market: 최신 quote_id 필요
        cached = self._rt_cache.get(coin)
        funding = int((cached or {}).get("funding_interval_s") or self.options.get("funding_interval_s", 3600))
        core = await self._fetch_indicative_quote(coin=coin, qty=str(amount), funding_interval_s=funding)
        quote_id = core.get("quote_id")
        if not quote_id:
            raise RuntimeError("quote_id를 얻지 못했습니다. indicative quote 실패.")
        
        res = await self._create_market_order(
            coin=coin,
            side=side,
            quote_id=quote_id,
            max_slippage=float(self.options.get("max_slippage", 0.01)),
            #is_reduce_only=bool(self.options.get("reduce_only", False)),
        )
        return res.get('rfq_id')
    
    async def get_position(self, symbol):
        await self.initialize_if_needed()
        coin = str(symbol).upper()
        data = await self._fetch_positions_all()
        return _extract_position_for_coin(data, coin=coin)
    
    async def get_collateral(self):
        await self.initialize_if_needed()
        data = await self._get_collateral_raw()
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return {"total_collateral": None, "available_collateral": None}
        return {
            "total_collateral": data.get("balance"),
            "available_collateral": data.get("max_withdrawable_amount"),
        }
    
    async def get_open_orders(self, symbol):
        return await self.fetch_open_orders(symbol)

    async def fetch_open_orders(self, symbol):
        await self.initialize_if_needed()
        coin = str(symbol).upper()
        raw = await self._fetch_open_orders_raw()
        return _extract_open_orders_core(raw, coin=coin)
    
    async def cancel_orders(self, symbol, open_orders = None):
        await self.initialize_if_needed()

        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []
        
        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]
        
        results = []
        for o in open_orders:
            rfq_id = o.get("rfq_id")
            ok = await self._cancel_order(rfq_id)
            results.append({"id": rfq_id, "status": "Success" if ok else "Failed"})
        return results

    async def get_mark_price(self, symbol):
        return await self.fetch_price(symbol)

    async def get_orderbook(self, symbol, *, qty=None) -> Optional[Dict[str, Any]]:
        """
        RFQ 방식이라 실제 orderbook이 없음.
        indicative quote의 bid/ask를 단일 레벨로 반환.

        Returns:
            {
                "bids": [[price, qty]],  # 단일 레벨이지만 list로 감싸서 일관성 유지
                "asks": [[price, qty]],
                "time": timestamp_ms,
                "msg": "RFQ 방식 안내 메시지"
            }
        """
        await self.initialize_if_needed()
        coin = str(symbol).upper()

        # get_orderbook 호출 시 해당 coin을 기본 referer coin으로 설정
        # (오더북을 보고 있다 = 해당 페이지를 보고 있다는 의미)
        self._last_viewed_coin = coin

        # 캐시 확인 (min_price_refresh_ms 이내면 캐시 사용)
        cached = self._rt_cache.get(coin)
        now_ms = int(time.monotonic() * 1000)
        thresh_ms = int(self.options.get("min_price_refresh_ms", 250))
        last_ms = int((cached or {}).get("last_price_at_ms") or 0)

        # bid/ask가 없으면 (초기 캐시는 mark_price만 있음) fetch 필요
        has_bid_ask = cached and cached.get("bid") is not None and cached.get("ask") is not None
        if not cached or not has_bid_ask or (now_ms - last_ms) >= thresh_ms:
            # 캐시 없거나 만료 → 새로 fetch
            if qty:
                qty = str(qty)

            qty_str = qty or self.options.get("probe_qty", "0.0001")

            # qty가 probe_qty와 다르면 업데이트
            if qty and qty_str != self.options.get("probe_qty"):
                self.options["probe_qty"] = qty_str

            funding = int((cached or {}).get("funding_interval_s") or self.options.get("funding_interval_s", 3600))
            await self._fetch_indicative_quote(coin=coin, qty=qty_str, funding_interval_s=funding)
            cached = self._rt_cache.get(coin)

        if not cached:
            return None

        bid = cached.get("bid")
        ask = cached.get("ask")
        qty = float(cached.get("qty") or self.options.get("probe_qty", "0.0001"))

        return {
            "bids": [[bid, qty]] if bid else [],
            "asks": [[ask, qty]] if ask else [],
            "time": int(time.time() * 1000),
            "msg": "RFQ 방식: 실제 orderbook이 아닌 indicative quote 기반 단일 레벨. 수량에 따라 가격이 달라질 수 있음.",
        }

    async def unsubscribe_orderbook(self, symbol) -> None:
        """RFQ 방식은 WS가 아니라 구독 취소할 것이 없음"""
        print(f"[Variational] unsubscribe_orderbook({symbol}): REST API 기반 RFQ 방식이라 구독 취소할 것이 없습니다.")

    async def supported_assets(self) -> List[str]:
        await self.initialize_if_needed()
        # initialize에서 이미 필터링/정렬된 리스트를 캐싱
        return list(self._asset_list)
    
    # 구현안해도됨
    async def close_position(self, symbol, position, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)

    async def close(self):
        """No persistent session to close"""
        pass