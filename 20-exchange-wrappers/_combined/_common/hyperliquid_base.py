"""
Hyperliquid 계열 거래소 공통 베이스.
- 메타 캐시, WS 풀, 가격/포지션/담보 조회, 주문 구조 등 공유
- 서명/payload 생성만 서브클래스에서 오버라이드
"""
from multi_perp_dex import MultiPerpDex, MultiPerpDexMixin
from .common_hyperliquid import (
    parse_hip3_symbol,
    round_to_tick,
    format_price,
    format_size,
    init_shared_hl_cache,
    extract_order_id,
    extract_cancel_status,
    STABLES,
    STABLES_DISPLAY,
)
from typing import Dict, Optional, List, Tuple, Any
import aiohttp
from aiohttp import TCPConnector
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

# 전역 상수
HL_BASE_URL = "https://api.hyperliquid.xyz"
HL_BASE_WS = "wss://api.hyperliquid.xyz/ws"


# 2026-04-22: 공유 mark_price 캐시 (HL 계열 여러 wrapper 반복 호출 → rate limit 완화)
_HL_MARK_PRICE_CACHE: dict = {}
_HL_MARK_PRICE_CACHE_TTL = 2.0


class HyperliquidBase(MultiPerpDexMixin, MultiPerpDex):
    """
    Hyperliquid 계열 공통 베이스.
    서브클래스는 _make_signed_payload()를 오버라이드하여 서명 방식을 정의.
    """
    def __init__(
        self,
        wallet_address: Optional[str] = None,
        vault_address: Optional[str] = None,
        builder_code: Optional[str] = None,
        builder_fee_pair: Optional[dict] = None,
        *,
        FrontendMarket: bool = False,
        proxy: Optional[str] = None,
    ):
        super().__init__()
        self.has_spot = True
        self.has_margin_mode = True  # isolated / cross 지원 여부

        self.wallet_address = wallet_address
        self.vault_address = vault_address
        self.builder_code = self._resolve_builder_code(builder_code)
        self.builder_fee_pair = builder_fee_pair
        self.cloid_prefix: Optional[str] = None  # e.g. "0x4d455243" for Miracle
        self.proxy = proxy  # e.g. "http://proxy.example.com:8080" or "socks5://..."

        # Builder rotation: 여러 builder code를 라운드로빈으로 사용
        # 각 항목: {"name": "miracle", "builder_code": "0x...", "fee_pair": {"base": "3 35"}, "cloid_prefix": "0x..."}
        self.builder_rotation: List[dict] = []
        self._rotation_index: int = 0

        self.http_base = HL_BASE_URL
        self.ws_base = HL_BASE_WS

        # 메타 캐시(공유 참조)
        self.dex_list: List[str] = ["hl"]
        self.spot_index_to_name: Dict[int, str] = {}
        self.spot_name_to_index: Dict[str, int] = {}
        self.spot_asset_index_to_pair: Dict[int, str] = {}
        self.spot_asset_pair_to_index: Dict[str, int] = {}
        self.spot_asset_index_to_bq: Dict[int, Tuple[str, str]] = {}
        self.spot_token_sz_decimals: Dict[str, int] = {}
        self.perp_metas_raw: List[dict] = []
        self.perp_asset_map: Dict[str, Tuple[int, int, int, bool, int]] = {}

        self._http: Optional[aiohttp.ClientSession] = None

        # WS
        self.ws_client = None
        self._ws_pool_key = None
        # 2026-04-25: WS 재연결 백오프 상태 (영구 끊김 방지)
        self._ws_reconnect_attempts: int = 0
        self._ws_last_reconnect_ts: float = 0.0
        self.FrontendMarket = FrontendMarket
        # WS support flags
        self.ws_supported = {
            "get_mark_price": True,
            "get_position": True,
            "get_open_orders": True,
            "get_collateral": True,
            "get_orderbook": True,
            "create_order": True,  # Hyperliquid supports WS trading
            "cancel_orders": True,  # Hyperliquid supports WS cancel
            "update_leverage": True,
        }

    # -------------------- 추상/오버라이드 대상 --------------------
    async def _make_signed_payload(self, action: dict) -> dict:
        """
        서브클래스에서 오버라이드: action → 서명된 payload 반환.
        기본 구현은 NotImplementedError.
        """
        raise NotImplementedError("Subclass must implement _make_signed_payload")

    # -------------------- 공통 유틸 --------------------
    def _resolve_builder_code(self, code: Optional[str]) -> Optional[str]:
        if not code:
            return None
        if code.startswith("0x"):
            return code
        # 정규화: 소문자 + 구분자 제거
        key = code.lower().replace(".", "").replace("_", "").replace("-", "")
        
        aliases = {
            # lit 변형
            "lit": "<EVM_ADDRESS>",
            "littrade": "<EVM_ADDRESS>",
            # based 변형
            "based": "<EVM_ADDRESS>",
            "basedone": "<EVM_ADDRESS>",
            "basedapp": "<EVM_ADDRESS>",
            # 나머지
            "dexari": "<EVM_ADDRESS>",
            "liquid": "<EVM_ADDRESS>",
            "supercexy": "<EVM_ADDRESS>",
            "bullpen": "<EVM_ADDRESS>",
            "mass": "<EVM_ADDRESS>",
            "dreamcash": "<EVM_ADDRESS>",
        }
        
        return aliases.get(key, code)  # 매칭 없으면 원본 반환

    def _parse_fee_pair(self, raw) -> Tuple[int, int]:
        if raw is None:
            return (0, 0)
        if isinstance(raw, (tuple, list)):
            try:
                a = int(float(raw[0]))
                b = int(float(raw[1])) if len(raw) > 1 else a
                return (a, b)
            except Exception:
                return (0, 0)
        if isinstance(raw, int):
            return (raw, raw)
        s = str(raw).replace(",", " ").replace("/", " ").replace("|", " ").strip()
        toks = [t for t in s.split() if t]
        try:
            a = int(float(toks[0]))
            b = int(float(toks[1])) if len(toks) > 1 else a
            return (a, b)
        except Exception:
            return (0, 0)

    def _pick_builder_fee_int(self, dex: Optional[str], order_type: str, is_spot: bool = False) -> Optional[int]:
        """
        빌더 fee 선택: dex별 키 → "dex" 공통 키 → "base" 키 순으로 폴백.
        - dex가 주어지면: m[dex] → m["dex"] → m["base"]
        - dex가 None이면: m["base"]
        """
        try:
            idx = 0 if str(order_type).lower() == "limit" else 1
            m = self.builder_fee_pair or {}
            
            # spot
            if is_spot:
                if "spot" in m:
                    a, b = self._parse_fee_pair(m["spot"])
                    return (a, b)[idx]

                if "base" in m:
                    a, b = self._parse_fee_pair(m["base"])
                    return (a, b)[idx]
                
                return None
            
            # perp
            # 1) 개별 DEX(hip3) 키
            if dex and dex in m:
                a, b = self._parse_fee_pair(m[dex])
                return (a, b)[idx]
            
            # 2) 공통 DEX 키 (dex가 주어졌을 때만)
            if dex and "dex" in m:
                a, b = self._parse_fee_pair(m["dex"])
                return (a, b)[idx]
            
            # 3) 메인/기본 키 (최종 폴백)
            if "base" in m:
                a, b = self._parse_fee_pair(m["base"])
                return (a, b)[idx]
            
            return None
        except Exception:
            return None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                connector=TCPConnector(force_close=True, enable_cleanup_closed=True)
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
            if self._ws_pool_key:
                # Pool에서 가져온 경우 release
                from mpdex.exchanges.hyperliquid_ws_client import WS_POOL
                try:
                    await WS_POOL.release(
                        address=self._ws_pool_key,
                        client=self.ws_client,
                        force_close=force_close
                    )
                except Exception:
                    pass
            else:
                # Proxy 직접 연결한 경우 close
                try:
                    await self.ws_client.close()
                except Exception:
                    pass
            self._ws_pool_key = None
            self.ws_client = None

    # -------------------- 초기화 --------------------
    async def init(self):
        s = self._session()
        cache = await init_shared_hl_cache(session=s)
        self.dex_list = cache["dex_list"]
        self.spot_index_to_name = cache["spot_index_to_name"]
        self.spot_name_to_index = cache["spot_name_to_index"]
        self.spot_asset_index_to_pair = cache["spot_asset_index_to_pair"]
        self.spot_asset_pair_to_index = cache["spot_asset_pair_to_index"]
        self.spot_asset_index_to_bq = cache["spot_asset_index_to_bq"]
        self.spot_token_sz_decimals = cache["spot_token_sz_decimals"]
        self.perp_metas_raw = cache["perp_metas_raw"]
        self.perp_asset_map = cache["perp_asset_map"]

        from mpdex.exchanges.hyperliquid_ws_client import WS_POOL
        try:
            await WS_POOL.prime_shared_meta(
                dex_order=self.dex_list,
                idx2name=self.spot_index_to_name,
                name2idx=self.spot_name_to_index,
                pair_by_index=self.spot_asset_index_to_pair,
                bq_by_index=self.spot_asset_index_to_bq,
            )
        except Exception:
            pass

        try:
            await self._create_ws_client()
        except Exception as ws_err:
            self._ws_init_failed = True
            import logging
            logging.getLogger(__name__).warning(
                f"[HyperliquidBase] WS 연결 실패 — REST 모드로 동작: {ws_err}"
            )

        self.update_available_symbols()

        return self

    def update_available_symbols(self):
        # perp initialization
        self.available_symbols['perp'] = {}
        for dex in self.dex_list:
            self.available_symbols['perp'][dex] = []
        self.available_symbols['spot'] = []
        
        for k in self.perp_asset_map:
            quote = self.get_perp_quote(k)
            quote_display = quote
            for onc, disp in zip(STABLES, STABLES_DISPLAY):
                if onc == quote:
                    quote_display = disp
                
            if ':' in k:
                dex = k.split(':')[0]
                coin = k.split(':')[1]
            else:
                dex = 'hl'
                coin = k
            composite_symbol = f"{coin}-{quote_display}"
            self.available_symbols['perp'][dex].append(composite_symbol)
            #self.available_symbols['perp'][dex].append(coin)
            #print(k,v,dex,quote,quote_display,composite_symbol)
        #return

        for k in self.spot_asset_pair_to_index:
            self.available_symbols['spot'].append(k)

    async def _make_transfer_payload(self, action: dict) -> dict:
        """
        usdClassTransfer 전용 서명 payload 생성.
        - sign_user_signed_action 사용 (sign_l1_action 아님)
        - 서브클래스에서 오버라이드 가능
        """
        raise NotImplementedError("Subclass must implement _make_transfer_payload for usdClassTransfer")

    async def transfer_to_spot(self, amount, *, prefer_ws: bool = True, timeout: float = 5.0):
        """
        Perp 지갑 → Spot 지갑으로 USDC 전송.
        - toPerp: false
        """
        coll_coin = "USDC"
        amount = float(amount)

        # 잔고 확인
        res = await self.get_collateral()
        available = res.get("available_collateral") or 0
        if amount > available:
            return {"status": "error", "message": f"insufficient perp balance: available={available} {coll_coin}, requested={amount}"}
        
        str_amount = str(amount)
        if self.vault_address:
            str_amount += f" subaccount:{self.vault_address}"
        #print(str_amount, self.vault_address)

        # 액션 생성 (signatureChainId, hyperliquidChain은 서명 함수에서 삽입됨)
        nonce = int(time.time() * 1000)
        action = {
            "type": "usdClassTransfer",
            "amount": str_amount,
            "toPerp": False,  # perp → spot
            "nonce": nonce,
            # signatureChainId, hyperliquidChain은 sign_user_signed_action에서 삽입됨
        }

        try:
            # [CHANGED] transfer 전용 서명 사용
            payload = await self._make_transfer_payload(action)
            resp = await self._send_action(payload, prefer_ws=prefer_ws, timeout=timeout)
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def transfer_to_perp(self, amount, *, prefer_ws: bool = True, timeout: float = 5.0):
        """
        Spot 지갑 → Perp 지갑으로 USDC 전송.
        - toPerp: true
        """
        coll_coin = "USDC"
        amount = float(amount)
        
        # 잔고 확인
        res = await self.get_spot_balance(coll_coin)
        available = (res.get(coll_coin) or res.get(coll_coin.upper()) or {}).get("available", 0)
        if amount > available:
            return {"status": "error", "message": f"insufficient spot balance: available={available} {coll_coin}, requested={amount}"}

        str_amount = str(amount)
        if self.vault_address:
            str_amount += f" subaccount:{self.vault_address}"
        #print(str_amount)
        # 액션 생성
        nonce = int(time.time() * 1000)
        action = {
            "type": "usdClassTransfer",
            "amount": str_amount,
            "toPerp": True,  # spot → perp
            "nonce": nonce,
            # signatureChainId, hyperliquidChain은 sign_user_signed_action에서 삽입됨
        }

        try:
            # transfer 전용 서명 사용
            payload = await self._make_transfer_payload(action)
            resp = await self._send_action(payload, prefer_ws=prefer_ws, timeout=timeout)
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}
        
    async def _ensure_ws_client_alive(self) -> bool:
        """
        2026-04-25: WS 클라이언트가 살아있는지 확인하고, 끊겨 있으면 백오프 후 재연결.
        Returns True if alive after this call, False if still down (REST fallback 권장).
        백오프: [10, 30, 60, 120, 300] 초 (max 5분)
        """
        # 이미 살아있으면 즉시 OK
        if self.ws_client is not None:
            connected = getattr(self.ws_client, 'connected', None)
            if connected is None or connected:
                return True

        now = time.time()
        backoff_seq = [10.0, 30.0, 60.0, 120.0, 300.0]
        idx = min(self._ws_reconnect_attempts, len(backoff_seq) - 1)
        backoff = backoff_seq[idx]
        if now - self._ws_last_reconnect_ts < backoff:
            return False
        self._ws_last_reconnect_ts = now

        # 영구 실패 플래그 리셋 (init 시점 일시적 실패였을 수 있음)
        if getattr(self, '_ws_init_failed', False):
            logger.info("[hl_ws] _ws_init_failed=True 이지만 재연결 시도 (영구 실패 추정 해제)")
            self._ws_init_failed = False

        # 끊긴 클라이언트가 있으면 정리
        if self.ws_client is not None:
            try:
                await self.ws_client.close()
            except Exception:
                pass
            self.ws_client = None
            self._ws_pool_key = None

        try:
            await self._create_ws_client()
        except Exception as e:
            logger.warning(f"[hl_ws] 재연결 실패 (attempt={self._ws_reconnect_attempts+1}): {e}")
            self._ws_reconnect_attempts += 1
            return False

        if self.ws_client is not None and getattr(self.ws_client, 'connected', True):
            attempts = self._ws_reconnect_attempts
            self._ws_reconnect_attempts = 0
            logger.info(f"[hl_ws] 재연결 성공 (attempts after fail={attempts})")
            return True

        self._ws_reconnect_attempts += 1
        return False

    async def _create_ws_client(self):
        if self.ws_client is not None:
            return
        if getattr(self, '_ws_init_failed', False):
            return  # 이미 실패함 — REST 모드로 동작

        address = self.vault_address or self.wallet_address

        # Proxy 사용 시 pool 안 쓰고 직접 생성 (proxy별 독립 연결)
        if self.proxy:
            from mpdex.exchanges.hyperliquid_ws_client import HLWSClientRaw
            client = HLWSClientRaw(dex=None, address=address, proxy=self.proxy)
            client.set_spot_meta(
                self.spot_index_to_name,
                self.spot_name_to_index,
                self.spot_asset_index_to_pair,
                self.spot_asset_index_to_bq,
            )
            client.set_perp_original_names(self.perp_asset_map)
            await client.connect()
            await client.subscribe()  # allMids 등 기본 구독
            await client.ensure_user_streams(address)  # 유저 스트림 구독
            self.ws_client = client
            self._ws_pool_key = None  # pool 안 씀
        else:
            # 일반: pool 사용
            from mpdex.exchanges.hyperliquid_ws_client import WS_POOL
            client = await WS_POOL.acquire(
                address=address,
                dex=None,
                dex_order=self.dex_list,
                idx2name=self.spot_index_to_name,
                name2idx=self.spot_name_to_index,
                pair_by_index=self.spot_asset_index_to_pair,
                bq_by_index=self.spot_asset_index_to_bq,
            )
            client.set_perp_original_names(self.perp_asset_map)
            self.ws_client = client
            self._ws_pool_key = (address or "").lower()

        for dex in self.dex_list:
            if dex != "hl":
                await client.ensure_allmids_for(dex)

    # -------------------- 자산 해석 --------------------
    async def _resolve_perp_asset_and_szdec(self, dex: Optional[str], coin_key: str):
        """
        캐시에서 Perp asset_id와 szDecimals, maxLeverage, onlyIsolated 반환
        - dex=None(메인):     key = coin_key.upper()
        - dex='xyz'(HIP-3):   key = coin_key(원문 'xyz:COIN')
        """
        key = coin_key if dex else coin_key.upper()
        return self.perp_asset_map.get(key, (None, 0, 1, False, 0, None))

    async def _resolve_asset_id_for_symbol(self, symbol: str, *, is_spot: bool) -> int:
        raw = str(symbol).strip()
        if is_spot or "/" in raw:
            pair = raw.upper()
            idx = self.spot_asset_pair_to_index.get(pair)
            if idx is None:
                raise RuntimeError(f"unknown spot pair: {pair}")
            return 10000 + int(idx)
        dex, coin_key = parse_hip3_symbol(raw)
        asset_id, *_ = await self._resolve_perp_asset_and_szdec(dex, coin_key)
        if asset_id is None:
            raise RuntimeError(f"asset not found: {raw}")
        return int(asset_id)

    def _spot_base_sz_decimals(self, pair: str) -> int:
        """
        pair: 'BASE/QUOTE'
        return: BASE 토큰의 szDecimals (없으면 0)
        """
        idx = self.spot_asset_pair_to_index.get(pair.upper())
        if idx is None:
            return 0
        bq = self.spot_asset_index_to_bq.get(idx)
        if not bq:
            return 0
        return self.spot_token_sz_decimals.get(bq[0].upper(), 0)

    def _spot_price_tick_decimals(self, pair: str) -> int:
        return max(0, 6 - self._spot_base_sz_decimals(pair))

    def _spot_pair_candidates(self, raw: str) -> List[str]:
        """
        'BASE/QUOTE'면 그대로 1개, 아니면 STABLES 우선순위로 BASE/QUOTE 후보를 만든다.
        """
        if "/" in raw:
            return [raw.upper()]
        return [f"{raw.upper()}/{q}" for q in STABLES]

    def get_perp_quote(self, symbol: str, *, is_basic_coll=False) -> str:
        if is_basic_coll:
            return 'USDC'
        
        dex, coin_key = parse_hip3_symbol(str(symbol).strip())
        _, _, _, _, quote_id, _ = self.perp_asset_map.get(coin_key, (None, 0, 1, False, 0, None))
        return self.spot_index_to_name.get(quote_id, "USDC")

    # -------------------- 가격/포지션/담보 (공통) --------------------
    def _parse_position_core(self, pos: dict) -> dict:
        """
        clearinghouseState.assetPositions[*].position 또는 WS 정규화 포맷을
        표준 스키마로 변환합니다.
        반환 스키마:
        {"entry_price": float|None, "unrealized_pnl": float|None, "side": "long"|"short"|"flat", "size": float}
        추가: lverage 정보, leverage_type, leverage_value, liquidation_price

        {
            "assetPositions": [
                {
                "position": {
                    "coin": "ETH",
                    "cumFunding": {
                    "allTime": "514.085417",
                    "sinceChange": "0.0",
                    "sinceOpen": "0.0"
                    },
                    "entryPx": "2986.3",
                    "leverage": {
                    "rawUsd": "-95.059824",
                    "type": "isolated",
                    "value": 20
                    },
                    "liquidationPx": "2866.26936529",
                    "marginUsed": "4.967826",
                    "maxLeverage": 50,
                    "positionValue": "100.02765",
                    "returnOnEquity": "-0.0026789",
                    "szi": "0.0335",
                    "unrealizedPnl": "-0.0134"
                },
                "type": "oneWay"
                }
            ],
            "crossMaintenanceMarginUsed": "0.0",
            "crossMarginSummary": {
                "accountValue": "13104.514502",
                "totalMarginUsed": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "13104.514502"
            },
            "marginSummary": {
                "accountValue": "13109.482328",
                "totalMarginUsed": "4.967826",
                "totalNtlPos": "100.02765",
                "totalRawUsd": "13009.454678"
            },
            "time": 1708622398623,
            "withdrawable": "13104.514502"
            }
        """
        def fnum(x, default=None):
            try:
                return float(x)
            except Exception:
                return default
            
        if "entry_px" in pos or "size" in pos:
            size = fnum(pos.get("size"), 0.0) or 0.0
            side = pos.get("side") or ("long" if size > 0 else "short" if size < 0 else "flat")

            return {
                "symbol": pos.get("coin"),
                "side": side,
                "size": abs(size),
                "entry_price": fnum(pos.get("entry_px")),
                "unrealized_pnl": fnum(pos.get("upnl"), 0.0),
                "liquidation_price": fnum(pos.get("liq_px"), None),
                "raw_data": pos,
            }

        size_signed = fnum(pos.get("szi"), 0.0) or 0.0
        side = "long" if size_signed > 0 else "short" if size_signed < 0 else "flat"
        return {
            "symbol": pos.get("coin"),
            "side": side,
            "size": abs(size_signed),
            "entry_price": fnum(pos.get("entryPx")),
            "unrealized_pnl": fnum(pos.get("unrealizedPnl"), 0.0),
            "liquidation_price": fnum(pos.get("liquidationPx"), None),
            "raw_data": pos,
        }

    async def get_position(self, symbol: str):
        """
        주어진 perp 심볼에 대한 단일 포지션 요약을 반환합니다.
        반환 스키마:
          {"entry_price": float|None, "unrealized_pnl": float|None, "side": "long"|"short"|"flat", "size": float}
        """
        
        try:
            pos = await self.get_position_ws(symbol)
            if pos:
                return pos
        except Exception as e:
            logger.warning(f"hyperliquid: get_position falling back to rest api / symbol {symbol} / error in ws {e}")

        return await self.get_position_rest(symbol)
    
    async def get_position_raw_ws(self, symbol: str, timeout: float = 2.0):
        address = (self.vault_address or self.wallet_address or "").lower()
        if not address:
            return None
        
        if not await self._ensure_ws_client_alive():
            raise TimeoutError("WS unavailable (positions_raw)")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ws_client.get_positions_raw_for_user(address):
                break
            await asyncio.sleep(0.05)
        pos_by_dex = self.ws_client.get_positions_raw_for_user(address)
        return pos_by_dex
        #return None

    async def get_position_ws(self, symbol: str, timeout: float = 2.0):
        """
        webData3(WS 캐시)에서 조회. 스냅샷 미도착 시 timeout까지 짧게 대기합니다.
        dex를 지정하지 않으면 self.dex_list 순서대로 검색합니다.
        """
        address = (self.vault_address or self.wallet_address or "").lower()
        if not address:
            return None
        
        if not await self._ensure_ws_client_alive():
            raise TimeoutError("WS unavailable (positions_norm)")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ws_client.get_positions_norm_for_user(address):
                break
            await asyncio.sleep(0.05)
        pos_by_dex = self.ws_client.get_positions_norm_for_user(address)
        if pos_by_dex is None:
            raise TimeoutError(f"WS positions not ready for {address}")
        sym = symbol.upper().strip()
        for pm in pos_by_dex.values():
            pos = pm.get(sym)
            if pos:
                parsed = self._parse_position_core(pos)
                if parsed["size"] and parsed["side"] != "flat":
                    return parsed
        return None

    async def get_position_rest(self, symbol: str):
        """
        REST clearinghouseState를 dex별로 조회하여 포지션을 찾습니다.
        dex를 지정하지 않으면 self.dex_list 순서대로 검색합니다.
        {
            "assetPositions": [
                {
                "position": {
                    "coin": "ETH",
                    "cumFunding": {
                    "allTime": "514.085417",
                    "sinceChange": "0.0",
                    "sinceOpen": "0.0"
                    },
                    "entryPx": "2986.3",
                    "leverage": {
                    "rawUsd": "-95.059824",
                    "type": "isolated",
                    "value": 20
                    },
                    "liquidationPx": "2866.26936529",
                    "marginUsed": "4.967826",
                    "maxLeverage": 50,
                    "positionValue": "100.02765",
                    "returnOnEquity": "-0.0026789",
                    "szi": "0.0335",
                    "unrealizedPnl": "-0.0134"
                },
                "type": "oneWay"
                }
            ],
            "crossMaintenanceMarginUsed": "0.0",
            "crossMarginSummary": {
                "accountValue": "13104.514502",
                "totalMarginUsed": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "13104.514502"
            },
            "marginSummary": {
                "accountValue": "13109.482328",
                "totalMarginUsed": "4.967826",
                "totalNtlPos": "100.02765",
                "totalRawUsd": "13009.454678"
            },
            "time": 1708622398623,
            "withdrawable": "13104.514502"
            }
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return None
        s = self._session()
        sym = symbol.strip().upper()
        for d in self.dex_list:
            dex_param = "" if d == "hl" else d
            payload = {"type": "clearinghouseState", "user": address, "dex": dex_param}
            try:
                async with s.post(f"{self.http_base}/info", json=payload, headers={"Content-Type": "application/json"}) as r:
                    data = await r.json()
            except Exception:
                continue
            for ap in (data or {}).get("assetPositions", []):
                pos = (ap or {}).get("position", {})
                if str(pos.get("coin", "")).upper() == sym:
                    parsed = self._parse_position_core(pos)
                    if parsed["size"] and parsed["side"] != "flat":
                        return parsed
        return None
    
    async def get_spot_balance(self, coin: str = None) -> dict:
        if "/" in coin: # symbol 대비
            coin = coin.split("/")[0]

        try:
            return await self.get_spot_balance_ws(coin)
        except Exception as e:
            logger.warning(f"hyperliquid: get_spot_balance falling back - error in ws {e}")
        logger.info('rest api not supported for get_spot_balance')
        
    async def get_spot_balance_ws(self, coin: str = None, timeout: float = 2.0) -> dict:
        default_json = {"total": 0.0, "available": 0.0, "locked": 0.0, "entry_ntl":0.0}
        address = (self.vault_address or self.wallet_address or "").lower()
        if not address:
            return default_json
        
        if not await self._ensure_ws_client_alive():
            raise TimeoutError("WS unavailable (spot_balance)")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ws_client.get_margin_by_dex_for_user(address):
                break
            await asyncio.sleep(0.05)
        balances = self.ws_client.get_balances_by_user(address) or {}

        spot_balances = balances.get("spot_balance",{})
        if coin:
            spot_balances = spot_balances.get(coin, default_json)
            spot_balances = {coin.upper(): spot_balances}

        return spot_balances

    async def get_collateral(self):
        try:
            return await self.get_collateral_ws()
        except Exception as e:
            logger.warning(f"hyperliquid: get_collateral falling back to rest api / error in ws {e}")
        return await self.get_collateral_rest()

    async def get_collateral_ws(self, timeout: float = 2.0):
        """
        WS(webData3/spotState) 기반 담보 조회.
        - 주소가 설정되어 있어야 하며, 첫 스냅샷이 도착할 때까지 최대 timeout 초 대기.
        """
        address = (self.vault_address or self.wallet_address or "").lower()
        if not address:
            return {"available_collateral": None, "total_collateral": None, "spot": {d: None for d in STABLES_DISPLAY}}
        
        if not await self._ensure_ws_client_alive():
            raise TimeoutError("WS unavailable (collateral)")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ws_client.get_margin_by_dex_for_user(address):
                break
            await asyncio.sleep(0.05)

        margin = self.ws_client.get_margin_by_dex_for_user(address)
        if margin is None:
            raise TimeoutError(f"WS margin not ready for {address}")
        av = sum((m or {}).get("accountValue", 0.0) for m in margin.values())
        wd = sum((m or {}).get("withdrawable", 0.0) for m in margin.values())
        balances = self.ws_client.get_balances_by_user(address) or {}
        spot = {disp: float(balances.get(onc, 0.0)) for onc, disp in zip(STABLES, STABLES_DISPLAY)}
        return {"available_collateral": wd or None, "total_collateral": av or None, "spot": spot}

    async def get_collateral_rest(self):
        """
        REST 기반 담보 조회:
        - Perp: clearinghouseState를 dex별로 병렬 호출 후 합산
        - Spot: spotClearinghouseState에서 STABLES 추출
        """
        address = self.vault_address or self.wallet_address
        if not address:
            return {
                "available_collateral": None,
                "total_collateral": None,
                "spot": {d: None for d in STABLES_DISPLAY},
            }

        s = self._session()
        url = f"{self.http_base}/info"
        headers = {"Content-Type": "application/json"}

        # ---------------- Perp: clearinghouseState 병렬 집계 ----------------
        def _dex_param(name: str) -> str:
            k = (name or "").strip().lower()
            return "" if (k == "" or k == "hl") else k

        dex_order = list(dict.fromkeys(self.dex_list or ["hl"]))

        async def _fetch_ch(dex_name: str) -> tuple[float, float]:
            payload = {"type": "clearinghouseState", "user": address, "dex": _dex_param(dex_name)}
            try:
                async with s.post(url, json=payload, headers=headers) as r:
                    data = await r.json()
            except Exception:
                return (0.0, 0.0)
            try:
                ms = (data or {}).get("marginSummary") or {}
                av = float(ms.get("accountValue") or 0.0)
            except Exception:
                av = 0.0
            try:
                wd = float((data or {}).get("withdrawable") or 0.0)
            except Exception:
                wd = 0.0
            return (av, wd)

        # 병렬 호출
        perp_results = await asyncio.gather(*[_fetch_ch(d) for d in dex_order], return_exceptions=False)
        av_sum = sum(av for av, _ in perp_results)
        wd_sum = sum(wd for _, wd in perp_results)

        total_collateral = av_sum if av_sum != 0.0 else None
        available_collateral = wd_sum if wd_sum != 0.0 else None

        # ---------------- Spot: spotClearinghouseState ----------------
        spot_map = {d: 0.0 for d in STABLES_DISPLAY}
        try:
            payload_spot = {"type": "spotClearinghouseState", "user": address}
            async with s.post(url, json=payload_spot, headers=headers) as r:
                spot_resp = await r.json()
            balances_list = (spot_resp or {}).get("balances") or []
            balances = {}
            for b in balances_list:
                if not isinstance(b, dict):
                    continue
                name = str(b.get("coin") or b.get("tokenName") or b.get("token") or "").upper()
                try:
                    total = float(b.get("total") or 0.0)
                except Exception:
                    continue
                if name:
                    balances[name] = total

            for onchain, disp in zip(STABLES, STABLES_DISPLAY):
                spot_map[disp] = float(balances.get(onchain, 0.0))
        except Exception:
            pass

        return {
            "available_collateral": available_collateral,
            "total_collateral": total_collateral,
            "spot": spot_map,
        }

    async def get_mark_price(self, symbol: str, *, is_spot: bool = False):
        import time as _t
        if "/" in symbol:
            is_spot = True
        cache_key = (str(symbol).upper(), is_spot)
        cached = _HL_MARK_PRICE_CACHE.get(cache_key)
        if cached and cached[1] > _t.time():
            return cached[0]
        try:
            price = await self.get_mark_price_ws(symbol, is_spot=is_spot)
            _HL_MARK_PRICE_CACHE[cache_key] = (price, _t.time() + _HL_MARK_PRICE_CACHE_TTL)
            return price
        except Exception as e:
            logger.debug(f"hyperliquid: mark_price WS fail / {symbol} / {e}")
        price = await self.get_mark_price_rest(symbol, is_spot=is_spot)
        _HL_MARK_PRICE_CACHE[cache_key] = (price, _t.time() + _HL_MARK_PRICE_CACHE_TTL)
        return price

    async def get_mark_price_ws(self, symbol: str, *, is_spot: bool = False, timeout: float = 3.0):
        """
        WS 캐시 기반 마크 프라이스 조회.
        - is_spot=True 이면 'BASE/QUOTE' 페어 가격을 조회
        - is_spot=False 이면 perp(예: 'BTC') 가격을 조회
        - 첫 틱이 아직 도착하지 않은 경우 wait_price_ready가 있으면 timeout까지 대기
        - 값을 얻지 못하면 예외를 던져 상위(get_mark_price)에서 REST 폴백하게 한다.
        """
        if not self.ws_client:
            await self._create_ws_client()

        raw = str(symbol).strip()
        #if "/" in raw:
        #    is_spot = True

        if is_spot:
            for pair in self._spot_pair_candidates(raw.upper()):
                # spot_pair로 명시
                if hasattr(self.ws_client, "wait_price_ready"):
                    try:
                        ready = await asyncio.wait_for(
                            self.ws_client.wait_price_ready(pair, timeout=timeout, kind="spot_pair"),
                            timeout=timeout
                        )
                        if not ready:
                            continue
                    except Exception:
                        continue
                
                px = self.ws_client.get_spot_pair_px(pair)
                if px is not None:
                    return float(px)

            # 모든 후보 실패
            raise TimeoutError(f"WS spot price not ready. tried={self._spot_pair_candidates(raw.upper())}")

        # Perp 경로
        key = raw.upper()
        # perp로 명시
        try:
            await asyncio.wait_for(
                self.ws_client.wait_price_ready(key, timeout=timeout, kind="perp"),
                timeout=timeout
            )
        except Exception:
            pass

        px = self.ws_client.get_price(key)
        if px is None:
            raise TimeoutError(f"WS perp price not ready for {key}")
        return float(px)

    async def get_mark_price_rest(self, symbol: str, *, is_spot: bool = False):
        dex = symbol.split(":")[0].lower() if ":" in symbol else None
        s = self._session()
        payload = {"type": "spotMetaAndAssetCtxs"} if is_spot else {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}
        try:
            async with s.post(f"{self.http_base}/info", json=payload, headers={"Content-Type": "application/json"}) as r:
                resp = await r.json()
        except Exception:
            return None
        if not isinstance(resp, list) or len(resp) < 2:
            return None
        universe, meta = resp[0].get("universe", []), resp[1]
        if is_spot:
            for pair in self._spot_pair_candidates(symbol.upper()):
                idx = self.spot_asset_pair_to_index.get(pair)
                if idx is not None and idx < len(meta):
                    px = meta[idx].get("markPx")
                    if px is not None:
                        return float(px)
            return None
        for i, v in enumerate(universe):
            if v.get("name", "").upper() == symbol.upper():
                return float(meta[i].get("markPx"))
        return None

    # -------------------- 주문/취소 (공통 골격) --------------------
    async def _send_action(
        self,
        payload: dict,
        *,
        prefer_ws: bool,
        timeout: float,
        max_retries: int = 3,
        base_delay: float = 0.5,
    ):
        """WS post 우선 → HTTP 폴백으로 payload 전송. 실패 시 exponential backoff 재시도."""
        last_error = None

        for attempt in range(max_retries):
            # WS 시도 (WS 초기화 실패 시 즉시 REST 폴백)
            if prefer_ws and not getattr(self, '_ws_init_failed', False):
                try:
                    if not self.ws_client:
                        await self._create_ws_client()
                    if self.ws_client:
                        resp = await self.ws_client.post_action(payload, timeout=timeout)
                        if str(resp.get("type", "")) == "error":
                            raise RuntimeError(str(resp.get("payload")))
                        return resp.get("payload", {})
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.debug(f"[HL] WS action failed (attempt {attempt + 1}/{max_retries}): {e}, retry in {delay:.2f}s")
                        await asyncio.sleep(delay)
                        continue
                    # 마지막 시도면 REST로 폴백
                    logger.warning(f"[HL] WS failed, falling back to REST: {e}")
                    await asyncio.sleep(0.1)

            # REST 시도
            try:
                s = self._session()
                async with s.post(
                    f"{self.http_base}/exchange",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    proxy=self.proxy,
                ) as r:
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.debug(f"[HL] REST action failed (attempt {attempt + 1}/{max_retries}): {e}, retry in {delay:.2f}s")
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_error or RuntimeError("_send_action failed after retries")

    async def update_leverage(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None, *, prefer_ws: bool = True, timeout: float = 5.0):
        """
        Update leverage and/or margin mode for a symbol.

        Args:
            leverage: If None, current leverage is preserved.
            margin_mode: If None, current margin_mode is preserved.

        Note: At least one of leverage or margin_mode should be provided.
        """
        if leverage is None and margin_mode is None:
            return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

        dex, coin_key = parse_hip3_symbol(symbol.strip())
        asset_id, _, _, only_isolated, *_ = await self._resolve_perp_asset_and_szdec(dex, coin_key)
        if asset_id is None:
            return {"status": "error", "message": "asset not found"}

        # If leverage or margin_mode is None, get current settings
        current_leverage = leverage
        current_margin_mode = margin_mode
        if leverage is None or margin_mode is None:
            try:
                current_info = await self.get_leverage_info(symbol, prefer_ws=prefer_ws, timeout=timeout)
                if current_info.get("status") == "ok":
                    if leverage is None:
                        current_leverage = current_info.get("leverage", 1)
                    if margin_mode is None:
                        current_margin_mode = current_info.get("margin_mode", "cross")
                else:
                    # Fallback defaults
                    if leverage is None:
                        current_leverage = 1
                    if margin_mode is None:
                        current_margin_mode = "cross"
            except Exception:
                # Fallback defaults on error
                if leverage is None:
                    current_leverage = 1
                if margin_mode is None:
                    current_margin_mode = "cross"

        # Determine actual margin mode (handle only_isolated)
        requested_mode = current_margin_mode.lower()
        forced_msg = None
        if only_isolated and requested_mode == "cross":
            actual_mode = "isolated"
            forced_msg = "symbol only supports isolated, forced from cross to isolated"
        else:
            actual_mode = requested_mode

        is_cross = actual_mode == "cross"
        action = {"type": "updateLeverage", "asset": int(asset_id), "isCross": is_cross, "leverage": int(current_leverage)}
        payload = await self._make_signed_payload(action)
        resp = await self._send_action(payload, prefer_ws=prefer_ws, timeout=timeout)

        result = {"status": "ok", "leverage": current_leverage, "margin_mode": actual_mode, "result": resp}
        if forced_msg:
            result["message"] = forced_msg
        return result

    async def get_leverage_info(self, symbol: str, *, prefer_ws: bool = True, timeout: float = 5.0) -> Dict[str, Any]:
        """
        Get current leverage settings for a symbol via activeAssetData.

        Args:
            symbol: Trading pair symbol (e.g., "BTC", "ETH:PERP-USDC")
            prefer_ws: If True, try WS first then fallback to REST
            timeout: Timeout for WS subscription

        Returns:
            {
                "symbol": str,
                "leverage": int,
                "margin_mode": "cross" or "isolated",
                "status": "ok",
                "max_leverage": int,
                "mark_price": str,
                "max_trade_sizes": [str, str],
                "available_to_trade": [str, str],
            }
        """
        dex, coin = parse_hip3_symbol(symbol.strip())

        # Get max_leverage and only_isolated from asset info
        _, _, max_lev, only_isolated, *_ = await self._resolve_perp_asset_and_szdec(dex, coin)

        if prefer_ws:
            try:
                return await self.get_leverage_info_ws(coin, max_leverage=max_lev, only_isolated=only_isolated, timeout=timeout)
            except Exception as e:
                logger.warning(f"[HL] get_leverage_info WS failed, falling back to REST: {e}")

        return await self.get_leverage_info_rest(coin, max_leverage=max_lev, only_isolated=only_isolated)

    async def get_leverage_info_ws(self, coin: str, max_leverage: int = 1, only_isolated: bool = False, timeout: float = 5.0) -> Dict[str, Any]:
        """Get leverage info via WebSocket (subscribe → receive → unsubscribe)"""
        if not self.ws_client:
            await self._create_ws_client()

        address = self.vault_address or self.wallet_address
        user_lower = address.lower()
        coin_upper = coin.upper()  # WS client uses uppercase for keys
        subscription = {"type": "activeAssetData", "user": address, "coin": coin}

        # Setup event for waiting (use uppercase for key to match WS client)
        key = (user_lower, coin_upper)
        self.ws_client._active_asset_events[key] = asyncio.Event()

        try:
            # Subscribe
            await self.ws_client.send_subscribe(subscription)

            # Wait for data with timeout
            try:
                await asyncio.wait_for(self.ws_client._active_asset_events[key].wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"WS activeAssetData not ready for {coin}")

            # Get cached data
            data = self.ws_client.get_active_asset_data(coin, user=address)
            if not data:
                raise ValueError(f"No data received for {coin}")

            return self._parse_leverage_info(coin, data, max_leverage=max_leverage, only_isolated=only_isolated)

        finally:
            # Unsubscribe and cleanup
            try:
                await self.ws_client.send_unsubscribe(subscription)
            except Exception:
                pass
            self.ws_client._active_asset_events.pop(key, None)
            self.ws_client._active_asset_data.pop((user_lower, coin_upper), None)

    async def get_leverage_info_rest(self, coin: str, max_leverage: int = 1, only_isolated: bool = False) -> Dict[str, Any]:
        """Get leverage info via REST API"""
        address = self.vault_address or self.wallet_address
        s = self._session()
        payload = {
            "type": "activeAssetData",
            "user": address,
            "coin": coin,
        }

        async with s.post(f"{self.http_base}/info", json=payload, headers={"Content-Type": "application/json"}) as r:
            if r.status != 200:
                text = await r.text()
                return {"status": "error", "message": f"REST failed: {r.status} {text}"}
            data = await r.json()

        return self._parse_leverage_info(coin, data, max_leverage=max_leverage, only_isolated=only_isolated)

    def _parse_leverage_info(self, coin: str, data: Dict[str, Any], max_leverage: int = 1, only_isolated: bool = False) -> Dict[str, Any]:
        """Parse activeAssetData response to standard format"""
        leverage_info = data.get("leverage", {})
        available_modes = ["isolated"] if only_isolated else ["cross", "isolated"]
        return {
            "symbol": coin,
            "leverage": leverage_info.get("value"),
            "margin_mode": leverage_info.get("type"),
            "status": "ok",
            "max_leverage": max_leverage,
            "available_margin_modes": available_modes,
            "mark_price": data.get("markPx"),
            "max_trade_sizes": data.get("maxTradeSzs"),
            "available_to_trade": data.get("availableToTrade"),
        }

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        order_type: str = "market",
        *,
        is_reduce_only: bool = False,
        is_spot: bool = False,
        tif: Optional[str] = None,
        client_id: Optional[str] = None,
        slippage: float = 0.05,
        prefer_ws: bool = True,
        timeout: float = 5.0,
    ):
        if "/" in symbol:
            is_spot = True

        is_buy = side.lower() == "buy"
        raw = symbol.strip()
        slip = float(slippage or 0.0)

        if is_spot or "/" in raw:
            dex = None
            pair = raw.upper()
            asset_id = 10000 + self.spot_asset_pair_to_index.get(pair, 0)
            tick_dec = self._spot_price_tick_decimals(pair)
            size_dec = self._spot_base_sz_decimals(pair)
            mark_sym = pair
        else:
            dex, coin_key = parse_hip3_symbol(raw)
            asset_id, sz_dec, *_ = await self._resolve_perp_asset_and_szdec(dex, coin_key)
            if asset_id is None:
                raise RuntimeError(f"hyperliquid create_order: asset_id unresolved for symbol={raw} (dex={dex} coin={coin_key}) — perp_asset_map stale/missing")
            if sz_dec is None:
                sz_dec = 0
            tick_dec = max(0, 6 - int(sz_dec))
            size_dec = sz_dec
            mark_sym = coin_key

        if price is None:
            ord_type, tif_final = "market", "FrontendMarket" if self.FrontendMarket else (tif or "Gtc")
            base_px = await self.get_mark_price(mark_sym, is_spot=is_spot or "/" in raw)
            if base_px is None:
                price_str = "0"
            else:
                eff = base_px * (1.0 + slip) if is_buy else base_px * (1.0 - slip)
                price_str = format_price(float(round_to_tick(eff, tick_dec, up=is_buy)), tick_dec) or "0"
        else:
            ord_type, tif_final = "limit", tif or "Gtc"
            price_str = format_price(float(round_to_tick(price, tick_dec, up=is_buy)), tick_dec) or "0"

        size_str = format_size(amount, int(size_dec))
        order_obj = {"a": int(asset_id), "b": is_buy, "p": price_str, "s": size_str, "r": is_reduce_only, "t": {"limit": {"tif": tif_final}}}
        # cloid 우선순위 (2026-04-19 수정):
        #   1) client_id 직접 지정
        #   2) rotation 활성 시: rotation entry의 cloid_prefix (없으면 no cloid — wrapper-level로 fallback 안 함)
        #   3) rotation 비활성 시: wrapper self.cloid_prefix
        # 이전 버그: rotation 활성일 때도 wrapper의 cloid_prefix (Miracle) 먼저 박혀서
        # DreamCash/based/bullpen/supercexy 주문도 Miracle cloid로 태깅됨 → 해당 프론트 attribution 손실.
        import uuid as _uuid_mod
        effective_cloid = client_id
        action = {"type": "order", "orders": [order_obj], "grouping": "na"}
        # Builder rotation: 리스트가 있으면 라운드로빈
        if self.builder_rotation:
            rot = self.builder_rotation[self._rotation_index % len(self.builder_rotation)]
            self._rotation_index += 1
            b_code = self._resolve_builder_code(rot.get("builder_code", ""))
            b_fee_pair = rot.get("fee_pair", self.builder_fee_pair)
            b_cloid = rot.get("cloid_prefix")
            if b_code:
                # fee 계산 (rotation 항목의 fee_pair 사용)
                old_fee_pair = self.builder_fee_pair
                self.builder_fee_pair = b_fee_pair
                fee = self._pick_builder_fee_int(dex, ord_type, is_spot=is_spot)
                self.builder_fee_pair = old_fee_pair
                action["builder"] = {"b": b_code.lower(), **({"f": int(fee)} if fee is not None else {})}
            # rotation 항목의 cloid만 사용 (wrapper cloid_prefix로 fallback 안 함)
            if not effective_cloid and b_cloid:
                effective_cloid = f"{b_cloid}{_uuid_mod.uuid4().hex[:24]}"
        elif self.builder_code:
            fee = self._pick_builder_fee_int(dex, ord_type, is_spot=is_spot)
            action["builder"] = {"b": self.builder_code.lower(), **({"f": int(fee)} if fee is not None else {})}
            # rotation 없을 때만 wrapper cloid_prefix 사용
            if not effective_cloid and self.cloid_prefix:
                effective_cloid = f"{self.cloid_prefix}{_uuid_mod.uuid4().hex[:24]}"
        else:
            # builder 없이 (일반) — wrapper cloid_prefix만 사용
            if not effective_cloid and self.cloid_prefix:
                effective_cloid = f"{self.cloid_prefix}{_uuid_mod.uuid4().hex[:24]}"
        if effective_cloid:
            order_obj["c"] = effective_cloid

        payload = await self._make_signed_payload(action)
        resp = await self._send_action(payload, prefer_ws=prefer_ws, timeout=timeout)
        try:
            return extract_order_id(resp)
        except Exception as e:
            return str(e)

    async def cancel_orders(self, symbol: str, open_orders=None, *, is_spot: bool = False, prefer_ws: bool = True, timeout: float = 5.0):
        
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return []
        
        asset_cache, cancels, results = {}, [], []
        for od in open_orders:
            oid, sym = od.get("order_id"), od.get("symbol") or symbol
            if oid is None:
                results.append({"order_id": oid, "symbol": sym, "ok": False, "error": "missing order_id"})
                continue
            try:
                if sym not in asset_cache:
                    asset_cache[sym] = await self._resolve_asset_id_for_symbol(sym, is_spot=is_spot or "/" in sym)
                cancels.append({"a": asset_cache[sym], "o": int(oid)})
                results.append({"order_id": int(oid), "symbol": sym, "ok": None, "error": None})
            except Exception as e:
                results.append({"order_id": oid, "symbol": sym, "ok": False, "error": str(e)})
        if not [r for r in results if r["ok"] is None]:
            return results
        action = {"type": "cancel", "cancels": cancels}
        payload = await self._make_signed_payload(action)
        try:
            resp = await self._send_action(payload, prefer_ws=prefer_ws, timeout=timeout)
            extract_cancel_status(resp)
            for r in results:
                if r["ok"] is None:
                    r["ok"] = True
        except Exception as e:
            for r in results:
                if r["ok"] is None:
                    r["ok"] = False
                    r["error"] = str(e)
        return results

    async def close_position(self, symbol, position, *, is_reduce_only=True):
        return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)

    # -------------------- open_orders (공통 골격) --------------------
    def _normalize_open_order_rest(self, o: dict):
        coin = str(o.get("coin", ""))
        if coin.startswith("@"):
            pair = self.spot_asset_index_to_pair.get(int(coin[1:]))
            symbol = pair.upper() if pair else None
        else:
            symbol = coin.upper()
        if not symbol:
            return None
        return {"order_id": o.get("oid"), "symbol": symbol, "side": "short" if o.get("side") == "A" else "long", "price": float(o.get("limitPx") or 0), "size": float(o.get("sz") or 0)}

    async def get_open_orders(self, symbol: str):
        try:
            return await self.get_open_orders_ws(symbol)
        except Exception as e:
            logger.warning(f"hyperliquid get_open_orders: falling back to rest api error {e}")
        return await self.get_open_orders_rest(symbol)

    async def get_open_orders_ws(self, symbol: str, timeout: float = 2.0):
        address = (self.vault_address or self.wallet_address or "").lower()
        if not address:
            return None
        if not await self._ensure_ws_client_alive():
            raise TimeoutError("WS unavailable (open_orders)")
        await self.ws_client.ensure_user_streams(address)
        await self.ws_client.wait_open_orders_ready(timeout=timeout, address=address)
        orders = self.ws_client.get_open_orders_for_user(address) or []
        sym = symbol.upper().strip()
        return [o for o in orders if (o.get("symbol") or "").upper() == sym] or None

    async def get_open_orders_rest(self, symbol: str, dex: str = "ALL_DEXS"):
        address = self.vault_address or self.wallet_address
        if not address:
            return None
        s = self._session()
        try:
            async with s.post(f"{self.http_base}/info", json={"type": "openOrders", "user": address, "dex": dex}, headers={"Content-Type": "application/json"}) as r:
                resp = await r.json()
        except Exception:
            return None
        raw = resp.get("orders") if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        normalized = [self._normalize_open_order_rest(o) for o in raw if isinstance(o, dict)]
        sym = symbol.upper().strip()
        return [o for o in normalized if o and o["symbol"] == sym] or None

    # -------------------- [ADDED] Orderbook 기능 --------------------
    async def subscribe_orderbook(self, symbol: str) -> None:
        """
        특정 심볼의 오더북(l2Book) 구독 시작.
        - WS 모드가 아니어도 WS 클라이언트를 생성하여 구독.
        - Spot은 'BASE/QUOTE' 형식, Perp는 'BTC' 형식.
        """
        if not self.ws_client:
            await self._create_ws_client()
        await self.ws_client.subscribe_orderbook(symbol)

    async def unsubscribe_orderbook(self, symbol: str) -> bool:
        """
        오더북 구독 해제.
        """
        if not self.ws_client:
            return True
        return await self.ws_client.unsubscribe_orderbook(symbol)

    async def get_orderbook(self, symbol: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """
        오더북 조회.
        - 아직 구독 중이 아니면 자동으로 구독 후 첫 스냅샷까지 대기.
        - 반환 형식:
          {
            "bids": [[price, size, n], ...],  # 가격 내림차순
            "asks": [[price, size, n], ...],  # 가격 오름차순
            "time": int
          }
        """
        if not self.ws_client:
            await self._create_ws_client()

        # 구독 확인 및 시작
        coin = self.ws_client._resolve_coin_for_orderbook(symbol)
        #print(coin)
        if self.ws_client._orderbook_sub_counts.get(coin, 0) <= 0:
            await self.ws_client.subscribe_orderbook(symbol)

        # 첫 스냅샷 대기
        ready = await self.ws_client.wait_orderbook_ready(symbol, timeout=timeout)
        if not ready:
            return None

        return self.ws_client.get_orderbook(symbol)