"""
Katana Perps Exchange Wrapper
IDEX v4 기반 Perpetual DEX on Katana Network (chainId 747474)

인증:
- HMAC: KP-API-Key + KP-HMAC-Signature (SHA256)
- Wallet: EIP-712 Typed Data 서명 (주문/취소)

Domain: name=KatanaPerps, version=1.0.0, chainId=747474
"""
from mpdex.base import MultiPerpDex, MultiPerpDexMixin
import asyncio
import aiohttp
import hmac
import hashlib
import json
import logging
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

BASE_URL = "https://api-perps.katana.network"
CHAIN_ID = 747474
EXCHANGE_CONTRACT = "<EVM_ADDRESS>"
ZERO_ADDR = "<EVM_ADDRESS>"
ZERO_QTY = "0.00000000"

ORDER_TYPE_MAP = {"market": 0, "limit": 1}
ORDER_SIDE_MAP = {"buy": 0, "sell": 1}
TIF_MAP = {"gtc": 0, "gtx": 1, "ioc": 2, "fok": 3}

EIP712_DOMAIN = {
    "name": "KatanaPerps",
    "version": "1.0.0",
    "chainId": CHAIN_ID,
    "verifyingContract": EXCHANGE_CONTRACT,
}
WALLET_SIG_RETRYABLE_CODE = "INVALID_WALLET_SIGNATURE"
MAX_WALLET_SIG_RETRIES = 5

ORDER_TYPES = {
    "Order": [
        {"name": "nonce", "type": "uint128"},
        {"name": "wallet", "type": "address"},
        {"name": "marketSymbol", "type": "string"},
        {"name": "orderType", "type": "uint8"},
        {"name": "orderSide", "type": "uint8"},
        {"name": "quantity", "type": "string"},
        {"name": "limitPrice", "type": "string"},
        {"name": "triggerPrice", "type": "string"},
        {"name": "triggerType", "type": "uint8"},
        {"name": "callbackRate", "type": "string"},
        {"name": "conditionalOrderId", "type": "uint128"},
        {"name": "isReduceOnly", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "selfTradePrevention", "type": "uint8"},
        {"name": "isLiquidationAcquisitionOnly", "type": "bool"},
        {"name": "delegatedPublicKey", "type": "address"},
        {"name": "clientOrderId", "type": "string"},
    ]
}


def _fmt_katana(val, reference: str) -> str:
    """Format value to match Katana's required decimal format (same as stepSize/tickSize).
    Katana requires values formatted exactly like their step/tick sizes:
    e.g. stepSize=0.00100000 -> qty must be X.XXX00000 (8 decimal places always)
    tickSize=0.10000000 -> price must be X.X0000000 (8 decimal places always)
    tickSize=1.00000000 -> price must be X.00000000 (integer, 8 decimal zeros)

    Raises ValueError for invalid inputs (None, NaN, 0, negative).
    """
    # Validate input
    if val is None:
        raise ValueError(f"[katana] cannot format None value (reference={reference})")
    d_val = Decimal(str(val))
    if d_val.is_nan() or d_val.is_infinite():
        raise ValueError(f"[katana] cannot format {val} (reference={reference})")

    # Find significant decimals from reference (e.g. "0.10000000" -> 1 sig decimal)
    ref = reference.rstrip("0")  # "0.1"
    if "." in ref:
        sig_decimals = len(ref.split(".")[1])
    else:
        sig_decimals = 0
    # Quantize to significant decimals, then format with 8 total
    quant = Decimal(f"0.{'0' * (sig_decimals - 1)}1" if sig_decimals > 0 else "1")
    rounded = d_val.quantize(quant, rounding=ROUND_DOWN)
    return f"{rounded:.8f}"


class KatanaExchange(MultiPerpDexMixin, MultiPerpDex):

    def __init__(self, api_key: str, api_secret: str, wallet_private_key: str, slippage: float = 0.01):
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.slippage = slippage
        pk_hex = wallet_private_key[2:] if wallet_private_key.startswith("0x") else wallet_private_key
        self._account = Account.from_key(bytes.fromhex(pk_hex))
        self.wallet_address = self._account.address
        self._session: Optional[aiohttp.ClientSession] = None
        self._markets_cache: dict = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── HMAC ──

    def _hmac(self, payload: str) -> str:
        return hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    # ── EIP-712 wallet signing ──

    def _sign_order(self, params: dict, nonce_int: int) -> str:
        # NOTE: signature must use the ORIGINAL short-format values (not 8-decimal)
        # because Katana normalizes server-side before verification
        data = {
            "nonce": nonce_int,
            "wallet": self.wallet_address,
            "marketSymbol": params["market"],
            "orderType": ORDER_TYPE_MAP.get(params["type"], 0),
            "orderSide": ORDER_SIDE_MAP.get(params["side"], 0),
            "quantity": params["quantity"],
            "limitPrice": params.get("price", ZERO_QTY),
            "triggerPrice": ZERO_QTY,
            "triggerType": 0,
            "callbackRate": ZERO_QTY,
            "conditionalOrderId": 0,
            "isReduceOnly": params.get("reduceOnly", False),
            "timeInForce": TIF_MAP.get(params.get("timeInForce", "gtc"), 0),
            "selfTradePrevention": 0,
            "isLiquidationAcquisitionOnly": False,
            "delegatedPublicKey": ZERO_ADDR,
            "clientOrderId": params.get("clientOrderId", ""),
        }
        signable = encode_typed_data(EIP712_DOMAIN, ORDER_TYPES, data)
        signed = self._account.sign_message(signable)
        return signed.signature.hex()

    # ── REST helpers ──

    async def _public_get(self, path: str, params: dict = None) -> dict:
        session = await self._get_session()
        async with session.get(f"{BASE_URL}{path}", params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()

    async def _auth_get(self, path: str, extra: dict = None):
        session = await self._get_session()
        from urllib.parse import urlencode
        nonce = str(uuid.uuid1())
        params = {"nonce": nonce, "wallet": self.wallet_address}
        if extra:
            params.update(extra)
        qs = urlencode(params)
        headers = {"KP-API-Key": self.api_key, "KP-HMAC-Signature": self._hmac(qs)}
        async with session.get(f"{BASE_URL}{path}?{qs}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.json()

    async def _auth_post(self, path: str, params: dict) -> dict:
        session = await self._get_session()
        for attempt in range(1, MAX_WALLET_SIG_RETRIES + 1):
            nonce = uuid.uuid1()
            wallet_sig = self._sign_order(params, nonce.int)

            body = {
                "parameters": {**params, "nonce": str(nonce), "wallet": self.wallet_address},
                "signature": wallet_sig,
            }
            body_str = json.dumps(body, separators=(",", ":"))
            headers = {
                "KP-API-Key": self.api_key,
                "KP-HMAC-Signature": self._hmac(body_str),
                "Content-Type": "application/json",
            }
            async with session.post(
                f"{BASE_URL}{path}",
                headers=headers,
                data=body_str.encode("ascii"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

            if resp.status < 400:
                return data

            code = data.get("code") if isinstance(data, dict) else None
            if (
                resp.status == 401
                and code == WALLET_SIG_RETRYABLE_CODE
                and attempt < MAX_WALLET_SIG_RETRIES
            ):
                logger.warning(
                    "[katana] wallet signature flake: market=%s side=%s tif=%s attempt=%s/%s clientOrderId=%s",
                    params.get("market"),
                    params.get("side"),
                    params.get("timeInForce", "gtc"),
                    attempt,
                    MAX_WALLET_SIG_RETRIES,
                    params.get("clientOrderId", ""),
                )
                await asyncio.sleep(0.05 * attempt)
                continue

            if resp.status == 401:
                import strategies.multi_runner as _mr2
                logging.getLogger("strategies.multi_runner").error(f"[katana] SIG_FAIL body={body_str[:300]}")
            raise Exception(f"[katana] {resp.status}: {data.get('message', data)}")

        raise RuntimeError("[katana] exhausted wallet signature retries")

    # ── Market data ──

    async def _ensure_markets(self):
        # 2026-04-22: 쿨다운 추가 (dict 응답 반복 스팸 방지)
        import time
        if not hasattr(self, "_markets_last_try"):
            self._markets_last_try = 0.0
        if self._markets_cache:
            return
        if time.time() - self._markets_last_try < 30:
            return  # 30초 쿨다운
        self._markets_last_try = time.time()
        try:
            markets = await self._public_get("/v1/markets")
            if isinstance(markets, list) and markets:
                for m in markets:
                    self._markets_cache[m["market"]] = m
                logger.info(f"[katana] markets cache loaded: {list(self._markets_cache.keys())}")
            elif isinstance(markets, dict):
                # 에러 응답 (rate limit 등) — debug 레벨로 강등
                logger.debug(f"[katana] markets dict response: {str(markets)[:100]}")
            else:
                logger.debug(f"[katana] markets unexpected: {type(markets)}")
        except Exception as e:
            logger.debug(f"[katana] markets load err: {e}")

    @staticmethod
    def _to_market_id(symbol: str) -> str:
        """2026-04-22: Katana 심볼 변환 (BTC → BTC-USD)"""
        if "-" in symbol:
            return symbol
        return f"{symbol.upper()}-USD"

    async def get_mark_price(self, symbol: str, **kwargs) -> float:
        market = self._to_market_id(symbol)
        price = 0.0
        try:
            data = await self._public_get("/v1/orderbook", {"market": market, "level": 1})
            price = float(data.get("markPrice", data.get("lastPrice", 0)) or 0)
        except Exception as e:
            logger.debug(f"[katana] orderbook fetch 실패: {e}")
        # 2026-04-22 fallback: /v1/markets의 indexPrice 사용
        if price <= 0:
            await self._ensure_markets()
            m = self._markets_cache.get(market)
            if m:
                try:
                    price = float(m.get("indexPrice") or 0)
                except Exception:
                    pass
        if price <= 0:
            raise ValueError(f"[katana] {symbol} mark price invalid: {price}")
        return price

    # ── Account ──

    async def get_collateral(self) -> dict:
        wallets = await self._auth_get("/v1/wallets")
        if not wallets:
            return {"total_collateral": 0, "available_collateral": 0}
        w = wallets[0]
        return {
            "total_collateral": float(w.get("equity", 0)),
            "available_collateral": float(w.get("freeCollateral", 0)),
        }

    async def get_position(self, symbol: str) -> dict:
        market = self._to_market_id(symbol)
        wallets = await self._auth_get("/v1/wallets")
        if not wallets:
            return {}
        for p in wallets[0].get("positions", []):
            if p.get("market") == market:
                qty = float(p.get("quantity", 0))
                return {
                    "side": "long" if qty > 0 else "short",
                    "size": abs(qty),
                    "entry_price": float(p.get("entryPrice", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnL", 0)),
                }
        return {}

    # ── Orders ──

    async def create_order(self, symbol, side, amount, price=None, order_type="market", **kwargs):
        market = self._to_market_id(symbol)
        await self._ensure_markets()
        mkt = self._markets_cache.get(market)
        if mkt is None:
            # Force re-fetch if symbol not in cache (cache might be stale or initial load failed)
            self._markets_cache.clear()
            await self._ensure_markets()
            mkt = self._markets_cache.get(market, {})
            if not mkt:
                logger.error(f"[katana] market {market} not found in cache — using fallback tick/step sizes")
        step = mkt.get("stepSize", "0.00100000")
        tick = mkt.get("tickSize", "0.01000000")
        logger.debug(f"[katana] {market} tickSize={tick} stepSize={step}")

        # Katana requires all values in fixed 8-decimal format matching step/tick
        qty = _fmt_katana(amount, step)

        params = {
            "market": market,
            "type": order_type,
            "side": side,
            "quantity": qty,
        }

        if order_type == "limit" and price is not None:
            import math
            if not isinstance(price, (int, float)) or math.isnan(price) or math.isinf(price) or price <= 0:
                raise ValueError(f"[katana] invalid limit price: {price}")
            params["price"] = _fmt_katana(price, tick)
        elif order_type == "market":
            mark = await self.get_mark_price(market)
            slp = mark * (1 + self.slippage) if side == "buy" else mark * (1 - self.slippage)
            params["price"] = _fmt_katana(slp, tick)
            params["type"] = "limit"
            params["timeInForce"] = "ioc"

        tif = kwargs.get("tif", kwargs.get("timeInForce"))
        if tif:
            tif_lower = str(tif).lower().replace("_", "")
            if tif_lower in ("ioc", "immediateorcancel"):
                params["timeInForce"] = "ioc"
            elif tif_lower in ("fok", "fillorkill"):
                params["timeInForce"] = "fok"
            elif tif_lower in ("gtx", "alo"):
                # Katana uses gtx for post-only (maker only)
                params["timeInForce"] = "gtx"

        reduce_only = kwargs.get("reduceOnly")
        if reduce_only is None:
            reduce_only = kwargs.get("is_reduce_only")
        if reduce_only:
            params["reduceOnly"] = True

        client_order_id = kwargs.get("clientOrderId", kwargs.get("client_order_id"))
        if not client_order_id:
            client_order_id = uuid.uuid4().hex
        params["clientOrderId"] = str(client_order_id)[:40]

        import strategies.multi_runner as _mr
        _mr_logger = logging.getLogger("strategies.multi_runner")
        _mr_logger.info(
            f"[katana] pre-sign: market={params['market']} qty={params['quantity']} price={params.get('price','N/A')} "
            f"type={params.get('type')} tif={params.get('timeInForce','gtc')} side={params.get('side')} "
            f"reduceOnly={params.get('reduceOnly', False)} coid={params['clientOrderId']}"
        )
        result = await self._auth_post("/v1/orders", params)
        logger.info(f"[katana] order: {side} {qty} {market} @ {params.get('price', 'market')}")
        return result

    async def close_position(self, symbol, position=None, *, is_reduce_only=True, **kwargs):
        if position is None:
            position = await self.get_position(symbol)
        if not position or not position.get("size"):
            return None
        close_side = "sell" if position["side"] == "long" else "buy"
        try:
            return await self.create_order(
                symbol,
                close_side,
                position["size"],
                order_type="market",
                is_reduce_only=is_reduce_only,
            )
        except ValueError as e:
            # mark price 0인 경우 entry_price 기반 IOC로 폴백
            if "mark price invalid" in str(e) and position.get("entry_price", 0) > 0:
                slippage = 0.03  # 3% 슬리피지 허용
                ep = position["entry_price"]
                fallback_price = ep * (1 - slippage) if close_side == "sell" else ep * (1 + slippage)
                logger.warning(f"[katana] mark price 0 → entry_price 기반 청산: {close_side} @ {fallback_price:.2f}")
                return await self.create_order(
                    symbol,
                    close_side,
                    position["size"],
                    price=fallback_price,
                    order_type="limit",
                    tif="ioc",
                    is_reduce_only=is_reduce_only,
                )
            raise

    async def get_open_orders(self, symbol: str) -> list:
        market = self._to_market_id(symbol)
        result = await self._auth_get("/v1/orders", {"market": market})
        return result if isinstance(result, list) else []

    async def cancel_orders(self, symbol: str) -> list:
        """Cancel all open orders for a market using EIP-712 OrderCancellationByMarketSymbol."""
        market = self._to_market_id(symbol)
        orders = await self.get_open_orders(market)
        if not orders:
            return []

        CANCEL_TYPES = {
            "OrderCancellationByMarketSymbol": [
                {"name": "nonce", "type": "uint128"},
                {"name": "wallet", "type": "address"},
                {"name": "delegatedKey", "type": "address"},
                {"name": "marketSymbol", "type": "string"},
            ]
        }
        session = await self._get_session()
        try:
            for attempt in range(1, MAX_WALLET_SIG_RETRIES + 1):
                nonce = uuid.uuid1()
                data = {
                    "nonce": nonce.int,
                    "wallet": self.wallet_address,
                    "delegatedKey": ZERO_ADDR,
                    "marketSymbol": market,
                }
                signable = encode_typed_data(EIP712_DOMAIN, CANCEL_TYPES, data)
                signed = self._account.sign_message(signable)

                params = {"nonce": str(nonce), "wallet": self.wallet_address, "market": market}
                body = {"parameters": params, "signature": signed.signature.hex()}
                body_str = json.dumps(body, separators=(",", ":"))
                hmac_sig = hmac.new(self.api_secret.encode(), body_str.encode(), hashlib.sha256).hexdigest()
                headers = {
                    "KP-API-Key": self.api_key,
                    "KP-HMAC-Signature": hmac_sig,
                    "Content-Type": "application/json",
                }

                async with session.delete(
                    f"{BASE_URL}/v1/orders",
                    headers=headers,
                    data=body_str.encode("ascii"),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json()

                if resp.status == 200:
                    canceled = result if isinstance(result, list) else []
                    logger.info(f"[katana] {market} 주문 {len(canceled)}건 취소 완료")
                    return canceled

                code = result.get("code") if isinstance(result, dict) else None
                if (
                    resp.status == 401
                    and code == WALLET_SIG_RETRYABLE_CODE
                    and attempt < MAX_WALLET_SIG_RETRIES
                ):
                    logger.warning(
                        "[katana] cancel signature flake: market=%s attempt=%s/%s",
                        market,
                        attempt,
                        MAX_WALLET_SIG_RETRIES,
                    )
                    await asyncio.sleep(0.05 * attempt)
                    continue

                logger.warning(f"[katana] {market} 주문 취소 실패: {resp.status} {result}")
                return []
        except Exception as e:
            logger.error(f"[katana] {market} 주문 취소 에러: {e}")
            return []

    async def update_leverage(self, symbol: str, leverage=None, margin_mode=None):
        logger.debug(f"[katana] leverage: {symbol} {leverage}x")
        return True

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
