"""
Decibel Exchange Wrapper for multi-perp-dex
Decibel (Aptos 기반 온체인 무기한 선물 DEX)
REST API + On-chain Transaction (aptos-sdk)
"""
from mpdex.base import MultiPerpDex, MultiPerpDexMixin
from typing import Optional, Dict, Any, List
import logging
import aiohttp
import asyncio

logger = logging.getLogger(__name__)

# Mainnet 설정
MAINNET_API_BASE = "https://api.mainnet.aptoslabs.com/decibel"
MAINNET_NODE_URL = "https://api.mainnet.aptoslabs.com/v1"
MAINNET_PACKAGE = "<PRIVATE_KEY>"

# Testnet 설정
TESTNET_API_BASE = "https://api.testnet.aptoslabs.com/decibel"
TESTNET_NODE_URL = "https://api.testnet.aptoslabs.com/v1"
TESTNET_PACKAGE = "<PRIVATE_KEY>"


class DecibelExchange(MultiPerpDexMixin, MultiPerpDex):
    """
    Decibel Perpetual DEX Wrapper

    - REST API: 가격, 포지션, 잔고 조회 (Geomi API 키 필요)
    - On-chain: 주문, 취소 (Aptos 프라이빗 키로 트랜잭션 서명)
    """

    def __init__(
        self,
        private_key: str,
        api_key: str = None,
        use_mainnet: bool = True,
        subaccount_address: str = None,
        slippage: float = 0.01,
    ):
        super().__init__()
        self.private_key = private_key
        self.api_key = api_key
        self.use_mainnet = use_mainnet
        self.slippage = slippage

        # 네트워크별 설정
        if use_mainnet:
            self.api_base = MAINNET_API_BASE
            self.node_url = MAINNET_NODE_URL
            self.package = MAINNET_PACKAGE
        else:
            self.api_base = TESTNET_API_BASE
            self.node_url = TESTNET_NODE_URL
            self.package = TESTNET_PACKAGE

        # Aptos 계정 (init에서 초기화)
        self.account = None
        self.rest_client = None
        self.account_address = None
        self.subaccount_address = subaccount_address

        # 마켓 메타데이터 캐시 {market_name: {addr, px_decimals, sz_decimals, ...}}
        self._markets: Dict[str, Dict] = {}
        # market_addr → market_name 역매핑
        self._addr_to_name: Dict[str, str] = {}

        self._session: Optional[aiohttp.ClientSession] = None

    async def init(self):
        """Aptos 계정 초기화 + 마켓 메타데이터 로드"""
        from aptos_sdk.account import Account
        from aptos_sdk.ed25519 import PrivateKey
        from aptos_sdk.async_client import RestClient

        # 프라이빗 키에서 계정 생성 (AIP-80 포맷 지원)
        pk_str = self.private_key
        if pk_str.startswith("ed25519-priv-"):
            pk_hex = pk_str.replace("ed25519-priv-0x", "").replace("ed25519-priv-", "")
        else:
            pk_hex = pk_str.replace("0x", "")
        private_key = PrivateKey.from_hex(pk_hex)
        self.account = Account.load_key(private_key.hex())
        self.account_address = str(self.account.address())
        self.rest_client = RestClient(self.node_url)

        logger.info(f"[decibel] Aptos 계정: {self.account_address}")

        # HTTP 세션
        self._session = aiohttp.ClientSession()

        # 마켓 메타데이터 로드
        await self._load_markets()

        # 서브계정 조회
        await self._load_subaccount()

        return self

    def _get_headers(self) -> dict:
        """REST API 헤더"""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _api_get(self, endpoint: str, params: dict = None) -> Any:
        """REST API GET 요청 (자동 세션 복구 + 1회 재시도)"""
        url = f"{self.api_base}/api/v1/{endpoint}"
        last_err = None
        for attempt in range(2):
            # 세션 무효화 감지 → 즉시 재생성 (aiohttp 장기 세션 None/closed 대비)
            if self._session is None or getattr(self._session, "closed", True):
                try:
                    if self._session is not None and not self._session.closed:
                        await self._session.close()
                except Exception:
                    pass
                try:
                    self._session = aiohttp.ClientSession()
                    logger.warning(f"[decibel] aiohttp 세션 재생성 (endpoint={endpoint}, attempt={attempt})")
                except Exception as e:
                    logger.error(f"[decibel] 세션 재생성 실패: {e}")
                    return None
            try:
                async with self._session.get(url, headers=self._get_headers(), params=params) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"[decibel] API {resp.status}: {endpoint} → {text[:200]}")
                        return None
                    return await resp.json()
            except AttributeError as e:
                # 'NoneType' object has no attribute 'get' 등 세션 상태 이상 → 세션 강제 재생성 후 재시도
                last_err = e
                logger.warning(f"[decibel] 세션 상태 이상 ({endpoint}, try {attempt}): {e} — 세션 재생성")
                try:
                    if self._session is not None:
                        await self._session.close()
                except Exception:
                    pass
                self._session = None
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
            except Exception as e:
                last_err = e
                logger.error(f"[decibel] API 에러: {endpoint} → {e}")
                return None
        logger.error(f"[decibel] API 재시도 후에도 실패: {endpoint} → {last_err}")
        return None

    async def _load_markets(self):
        """마켓 목록 로드 + 메타데이터 캐시"""
        data = await self._api_get("markets")
        if not data:
            logger.warning("[decibel] 마켓 목록 로드 실패")
            return

        for m in data:
            name = m.get("market_name", "").upper()
            addr = m.get("market_addr", "")
            self._markets[name] = {
                "addr": addr,
                "px_decimals": m.get("px_decimals", 9),
                "sz_decimals": m.get("sz_decimals", 9),
                "tick_size": m.get("tick_size", 1),
                "lot_size": m.get("lot_size", 1),
                "min_size": m.get("min_size", 1),
                "max_leverage": m.get("max_leverage", 20),
                "mode": m.get("mode", "Open"),
            }
            self._addr_to_name[addr] = name

        # BTC/USD → BTC 형태로 심볼 목록 저장
        self.available_symbols = {"perp": [name.replace("/USD", "") for name in self._markets.keys()]}
        logger.info(f"[decibel] 마켓 {len(self._markets)}개 로드: {list(self._markets.keys())}")

    async def _load_subaccount(self):
        """서브계정 주소 조회"""
        data = await self._api_get("subaccounts", {"owner": self.account_address})
        if data and len(data) > 0:
            # 응답이 문자열 배열이거나 객체 배열일 수 있음
            first = data[0]
            if isinstance(first, str):
                self.subaccount_address = first
            elif isinstance(first, dict):
                self.subaccount_address = first.get("address") or first.get("subaccount_address") or first.get("account")
            logger.info(f"[decibel] 서브계정: {self.subaccount_address}")
        else:
            # config에서 직접 지정된 subaccount_address 사용
            logger.warning("[decibel] 서브계정 API 조회 실패 — config 또는 수동 설정 필요")

    def _get_market(self, symbol: str) -> Dict:
        """심볼 → 마켓 메타데이터. BTC → BTC/USD 매핑"""
        sym = symbol.upper()
        # 이미 정확한 마켓 이름이면 그대로
        if sym in self._markets:
            return self._markets[sym]
        # BTC → BTC/USD 변환
        with_usd = f"{sym}/USD"
        if with_usd in self._markets:
            return self._markets[with_usd]
        # -PERP 등 제거 후 재시도
        cleaned = sym.replace("-PERP", "").replace("_PERP", "").replace("/USD", "").replace("-USD", "")
        with_usd2 = f"{cleaned}/USD"
        if with_usd2 in self._markets:
            return self._markets[with_usd2]
        raise ValueError(f"[decibel] 지원하지 않는 심볼: {symbol} (가능: {list(self._markets.keys())})")

    def _get_market_addr(self, symbol: str) -> str:
        """심볼 → 마켓 주소"""
        return self._get_market(symbol)["addr"]

    def _price_to_chain(self, price: float, market: Dict) -> int:
        """가격 → 온체인 단위 변환 (tick_size 정렬)"""
        px_dec = market["px_decimals"]
        tick = market["tick_size"]
        raw = int(price * (10 ** px_dec))
        return (raw // tick) * tick

    def _size_to_chain(self, size: float, market: Dict) -> int:
        """사이즈 → 온체인 단위 변환 (lot_size 정렬, min_size 보장)"""
        sz_dec = market["sz_decimals"]
        lot = market["lot_size"]
        min_sz = market["min_size"]
        raw = int(size * (10 ** sz_dec))
        aligned = (raw // lot) * lot
        return max(aligned, min_sz) if aligned > 0 else 0

    def _chain_to_price(self, chain_val: int, market: Dict) -> float:
        """온체인 단위 → 가격"""
        return chain_val / (10 ** market["px_decimals"])

    def _chain_to_size(self, chain_val: int, market: Dict) -> float:
        """온체인 단위 → 사이즈"""
        return chain_val / (10 ** market["sz_decimals"])

    # ==================== READ (REST API) ====================

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """마크 가격 조회"""
        try:
            market = self._get_market(symbol)
            data = await self._api_get("prices", {"market": market["addr"]})
            if data and len(data) > 0:
                return float(data[0].get("mark_px", 0))
            return None
        except Exception as e:
            logger.error(f"[decibel] get_mark_price 에러: {e}")
            return None

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """포지션 조회"""
        try:
            if not self.subaccount_address:
                return None

            market = self._get_market(symbol)
            data = await self._api_get("account_positions", {
                "account": self.subaccount_address,
                "market_address": market["addr"],
            })
            if not data:
                return None

            for p in data:
                if p.get("is_deleted", False):
                    continue
                size = float(p.get("size", 0))
                if size == 0:
                    continue

                return {
                    "symbol": symbol,
                    "side": "long" if size > 0 else "short",
                    "size": abs(size),
                    "entry_price": float(p.get("entry_price", 0)),
                    "unrealized_pnl": float(p.get("unrealized_funding", 0)),
                    "liquidation_price": float(p.get("estimated_liquidation_price", 0)) if p.get("estimated_liquidation_price") else None,
                    "leverage_type": "isolated" if p.get("is_isolated") else "cross",
                    "leverage_value": int(p.get("user_leverage", 0)),
                    "raw_data": p,
                }
            return None
        except Exception as e:
            logger.error(f"[decibel] get_position 에러: {e}")
            return None

    async def get_collateral(self):
        """잔고 조회"""
        try:
            if not self.subaccount_address:
                return None

            data = await self._api_get("account_overviews", {
                "account": self.subaccount_address,
            })
            if not data:
                return None

            # 응답이 dict인 경우 (단일 계정)
            overview = data if isinstance(data, dict) else (data[0] if (data and data[0]) else {})
            if not overview or not isinstance(overview, dict):
                return None

            total = float(overview.get("perp_equity_balance", 0))
            available = float(overview.get("usdc_cross_withdrawable_balance", 0))

            return {
                "total_collateral": total,
                "available_collateral": available,
                "unrealized_pnl": float(overview.get("unrealized_pnl", 0)),
                "margin_ratio": float(overview.get("cross_margin_ratio", 0)),
            }
        except Exception as e:
            logger.error(f"[decibel] get_collateral 에러: {e}")
            return None

    async def get_open_orders(self, symbol: str) -> List[Dict]:
        """오픈 오더 조회"""
        try:
            if not self.subaccount_address:
                return []

            data = await self._api_get("open_orders", {
                "account": self.subaccount_address,
                "limit": 100,
                "offset": 0,
            })
            if not data:
                return []

            items = data.get("items", []) if isinstance(data, dict) else data
            market = self._get_market(symbol)
            market_addr = market["addr"]

            orders = []
            for o in items:
                if o.get("market") != market_addr:
                    continue
                orders.append({
                    "id": o.get("order_id", ""),
                    "symbol": symbol,
                    "side": "buy" if o.get("is_buy") else "sell",
                    "size": float(o.get("remaining_size", 0) or o.get("orig_size", 0)),
                    "price": float(o.get("price", 0)) if o.get("price") else None,
                    "order_type": o.get("order_type", ""),
                    "raw_data": o,
                })
            return orders
        except Exception as e:
            logger.error(f"[decibel] get_open_orders 에러: {e}")
            return []

    # ==================== WRITE (On-chain Transaction) ====================

    async def _submit_tx(self, function: str, args: list) -> Optional[str]:
        """Aptos 온체인 트랜잭션 제출 (TransactionArgument 리스트 사용)"""
        try:
            from aptos_sdk.transactions import TransactionPayload, EntryFunction

            payload = EntryFunction.natural(
                f"{self.package}::dex_accounts_entry",
                function,
                [],  # type_arguments
                args,  # List[TransactionArgument]
            )

            signed_tx = await self.rest_client.create_bcs_signed_transaction(
                self.account,
                TransactionPayload(payload),
            )
            tx_hash = await self.rest_client.submit_bcs_transaction(signed_tx)
            await self.rest_client.wait_for_transaction(tx_hash)

            logger.info(f"[decibel] TX 성공: {function} → {tx_hash}")
            return tx_hash
        except Exception as e:
            logger.error(f"[decibel] TX 실패: {function} → {e}")
            raise

    async def _ensure_subaccount(self):
        """서브계정이 없으면 생성"""
        if self.subaccount_address:
            return

        logger.info("[decibel] 서브계정 생성 중...")
        await self._submit_tx("create_new_subaccount", [])
        await self._load_subaccount()

        if not self.subaccount_address:
            raise RuntimeError("[decibel] 서브계정 생성 실패")

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        """
        주문 생성

        - market 주문: price=None → IOC (time_in_force=2)로 슬리피지 가격 사용
        - limit 주문: price 지정 → GTC (time_in_force=0)
        """
        from aptos_sdk.bcs import Serializer

        await self._ensure_subaccount()

        market = self._get_market(symbol)
        market_addr = self._get_market_addr(symbol)

        is_buy = side.lower() in ("buy", "long")

        # 사이즈 변환
        chain_size = self._size_to_chain(amount, market)
        if chain_size <= 0:
            logger.warning(f"[decibel] 주문 사이즈 0 — 최소 단위 미달: {amount}")
            return None

        # 가격 & time_in_force 결정
        if price is None or order_type == 'market':
            # 시장가: 현재가에서 슬리피지 적용
            mark = await self.get_mark_price(symbol)
            if not mark:
                raise RuntimeError(f"[decibel] 마크 가격 조회 실패: {symbol}")
            order_price = mark * (1 + self.slippage) if is_buy else mark * (1 - self.slippage)
            chain_price = self._price_to_chain(order_price, market)
            time_in_force = 2  # IOC
        else:
            chain_price = self._price_to_chain(price, market)
            time_in_force = 0  # GTC

        is_reduce_only = kwargs.get("is_reduce_only", False)

        # place_order_to_subaccount 인자 직렬화
        # BCS 직렬화를 위한 인자 구성
        args = self._serialize_place_order_args(
            subaccount_addr=self.subaccount_address,
            market_addr=market_addr,
            price=chain_price,
            size=chain_size,
            is_buy=is_buy,
            time_in_force=time_in_force,
            is_reduce_only=is_reduce_only,
        )

        tx_hash = await self._submit_tx("place_order_to_subaccount", args)
        return {
            "tx_hash": tx_hash,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "order_type": order_type,
        }

    @staticmethod
    def _option_str(ser, val):
        """Option<String> 직렬화"""
        if val is None:
            ser.bool(False)
        else:
            ser.bool(True)
            ser.str(val)

    @staticmethod
    def _option_u64(ser, val):
        """Option<u64> 직렬화"""
        if val is None:
            ser.bool(False)
        else:
            ser.bool(True)
            ser.u64(val)

    @staticmethod
    def _option_address(ser, val):
        """Option<address> 직렬화"""
        if val is None:
            ser.bool(False)
        else:
            ser.bool(True)
            ser.struct(val)

    def _serialize_place_order_args(
        self, subaccount_addr, market_addr, price, size,
        is_buy, time_in_force, is_reduce_only,
    ) -> list:
        """place_order_to_subaccount 인자를 TransactionArgument로 직렬화"""
        from aptos_sdk.account_address import AccountAddress
        from aptos_sdk.bcs import Serializer
        from aptos_sdk.transactions import TransactionArgument

        sub_addr = AccountAddress.from_str(subaccount_addr)
        mkt_addr = AccountAddress.from_str(market_addr)

        return [
            TransactionArgument(sub_addr, Serializer.struct),       # subaccount: Object<Subaccount>
            TransactionArgument(mkt_addr, Serializer.struct),       # market: Object<PerpMarket>
            TransactionArgument(price, Serializer.u64),             # price: u64
            TransactionArgument(size, Serializer.u64),              # size: u64
            TransactionArgument(is_buy, Serializer.bool),           # is_buy: bool
            TransactionArgument(time_in_force, Serializer.u8),      # time_in_force: u8
            TransactionArgument(is_reduce_only, Serializer.bool),   # is_reduce_only: bool
            TransactionArgument(None, self._option_str),            # client_order_id: Option<String>
            TransactionArgument(None, self._option_u64),            # stop_price: Option<u64>
            TransactionArgument(None, self._option_u64),            # tp_trigger_price: Option<u64>
            TransactionArgument(None, self._option_u64),            # tp_limit_price: Option<u64>
            TransactionArgument(None, self._option_u64),            # sl_trigger_price: Option<u64>
            TransactionArgument(None, self._option_u64),            # sl_limit_price: Option<u64>
            TransactionArgument(None, self._option_address),        # builder_address: Option<address>
            TransactionArgument(None, self._option_u64),            # builder_fees: Option<u64>
        ]

    async def cancel_orders(self, symbol, open_orders=None):
        """주문 취소"""
        if open_orders is None:
            open_orders = await self.get_open_orders(symbol)

        if not open_orders:
            return None

        market = self._get_market(symbol)
        market_addr = self._get_market_addr(symbol)
        results = []

        for order in open_orders:
            try:
                from aptos_sdk.account_address import AccountAddress
                from aptos_sdk.bcs import Serializer
                from aptos_sdk.transactions import TransactionArgument

                order_id = int(order["id"]) if isinstance(order["id"], str) else order["id"]
                args = [
                    TransactionArgument(AccountAddress.from_str(self.subaccount_address), Serializer.struct),
                    TransactionArgument(order_id, Serializer.u128),
                    TransactionArgument(AccountAddress.from_str(market_addr), Serializer.struct),
                ]
                tx_hash = await self._submit_tx("cancel_order_to_subaccount", args)
                results.append({"order_id": order["id"], "tx_hash": tx_hash, "status": "cancelled"})
            except Exception as e:
                logger.error(f"[decibel] 주문 취소 실패 {order['id']}: {e}")
                results.append({"order_id": order["id"], "error": str(e)})

        return results

    async def close_position(self, symbol, position=None, *, is_reduce_only=True):
        """포지션 청산"""
        if position is None:
            position = await self.get_position(symbol)
        if not position or float(position.get("size", 0)) == 0:
            return None

        close_side = "sell" if position["side"] == "long" else "buy"
        return await self.create_order(
            symbol, close_side, float(position["size"]),
            order_type='market', is_reduce_only=is_reduce_only,
        )

    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        """Decibel은 cross-margin — 레버리지는 포지션 크기로 자동 결정"""
        return {
            "symbol": symbol,
            "leverage": leverage,
            "margin_mode": "cross",
            "status": "ok",
            "note": "Decibel uses cross-margin; leverage determined by position size",
        }

    async def get_leverage_info(self, symbol):
        """레버리지 정보"""
        market = self._get_market(symbol)
        return {
            "symbol": symbol,
            "leverage": None,
            "margin_mode": "cross",
            "status": "ok",
            "max_leverage": market.get("max_leverage", 20),
            "available_margin_modes": ["cross"],
        }

    async def close(self, force_close: bool = True):
        """연결 정리"""
        if self._session and not self._session.closed:
            await self._session.close()
        if self.rest_client:
            await self.rest_client.close()
        self._session = None
        self.rest_client = None
        logger.info("[decibel] 연결 종료")
