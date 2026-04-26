"""
Aster DEX Exchange Wrapper
===========================
Base L1 기반 Perp DEX. Binance 호환 REST API + EIP-712 인증.
V3 API (wallet signature, no API key needed).

인터페이스:
  get_mark_price(symbol) → float
  create_order(symbol, side, amount, ...) → dict
  get_position(symbol) → dict | None
  close_position(symbol, position) → dict
  get_collateral() → dict
  update_leverage(symbol, leverage, margin_mode) → None
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data

from mpdex.base import MultiPerpDex, MultiPerpDexMixin

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.asterdex.com"
WS_URL = "wss://fstream.asterdex.com"

# EIP-712 domain for Aster V3
EIP712_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,  # mainnet signing chain ID
    "verifyingContract": "<EVM_ADDRESS>",
}

EIP712_TYPES = {
    "Message": [{"name": "msg", "type": "string"}],
}


class AsterExchange(MultiPerpDexMixin, MultiPerpDex):

    def __init__(self, user_address: str, signer_private_key: str):
        """
        user_address: 메인 계정 지갑 주소
        signer_private_key: API 지갑 private key (web에서 authorize 필요)
        """
        super().__init__()
        self._user = user_address
        self._signer_key = signer_private_key
        self._signer_address = Account.from_key(signer_private_key).address
        self._session: Optional[aiohttp.ClientSession] = None

    async def init(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        logger.info(f"[aster] 초기화 완료 (user={self._user[:10]}... signer={self._signer_address[:10]}...)")
        return self

    def _build_signed_url(self, endpoint: str, params: dict) -> str:
        """EIP-712 서명 생성 → URL 반환"""
        params["user"] = self._user
        params["signer"] = self._signer_address
        params["nonce"] = str(int(time.time() * 1_000_000))

        # Build sorted message string (signature 제외)
        msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        # EIP-712 signing
        full_message = {"types": {"EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ], **EIP712_TYPES}, "primaryType": "Message", "domain": EIP712_DOMAIN,
            "message": {"msg": msg}}

        signed = Account.sign_typed_data(self._signer_key, full_message=full_message)
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig

        # signature는 param_str에 append (dict에 넣지 않음)
        return f"{BASE_URL}{endpoint}?{msg}&signature={sig}"

    async def _public_get(self, endpoint: str, params: dict = None) -> Any:
        url = f"{BASE_URL}{endpoint}"
        async with self._session.get(url, params=params) as r:
            if r.status != 200:
                text = await r.text()
                logger.warning(f"[aster] {endpoint} {r.status}: {text[:200]}")
                return None
            return await r.json()

    def _urllib_req(self, url: str, method: str = "GET") -> Any:
        import urllib.request
        data = b"" if method == "POST" else None
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            logger.warning(f"[aster] {method} {e.code}: {e.read().decode()[:200]}")
            return None
        except Exception as e:
            logger.warning(f"[aster] {method} error: {e}")
            return None

    async def _signed_get(self, endpoint: str, params: dict) -> Any:
        return self._urllib_req(self._build_signed_url(endpoint, params), "GET")

    async def _signed_post(self, endpoint: str, params: dict) -> Any:
        return self._urllib_req(self._build_signed_url(endpoint, params), "POST")

    async def _signed_delete(self, endpoint: str, params: dict) -> Any:
        return self._urllib_req(self._build_signed_url(endpoint, params), "DELETE")

    # ── Market Data (public) ──

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._public_get("/fapi/v3/premiumIndex", {"symbol": symbol})
        if data:
            return float(data.get("markPrice", 0))
        return 0.0

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        data = await self._public_get("/fapi/v3/premiumIndex", {"symbol": symbol})
        if data:
            return float(data.get("lastFundingRate", 0))
        return None

    # ── Trading ──

    async def create_order(self, symbol: str, side: str, amount: float,
                           price: float = None, order_type: str = "market", **kwargs):
        side_str = side.upper()
        if side_str in ("LONG", "BUY"):
            side_str = "BUY"
        elif side_str in ("SHORT", "SELL"):
            side_str = "SELL"

        params = {
            "symbol": symbol,
            "side": side_str,
            "type": "MARKET" if order_type.lower() == "market" else "LIMIT",
            "quantity": str(amount),
        }
        if price and order_type.lower() == "limit":
            params["price"] = str(price)
            params["timeInForce"] = kwargs.get("tif", "GTC")

        if kwargs.get("is_reduce_only"):
            params["reduceOnly"] = "true"

        result = await self._signed_post("/fapi/v3/order", params)
        if result:
            logger.info(f"[aster] 주문 체결: {side_str} {symbol} {amount}")
        return result

    async def get_position(self, symbol: str) -> Optional[Dict]:
        data = await self._signed_get("/fapi/v3/positionRisk", {"symbol": symbol})
        if not data:
            data = await self._signed_get("/fapi/v3/positionInformation", {"symbol": symbol})
        if not data:
            return None

        positions = data if isinstance(data, list) else [data]
        for pos in positions:
            amt = float(pos.get("positionAmt", 0))
            if abs(amt) > 0:
                return {
                    "symbol": symbol,
                    "side": "long" if amt > 0 else "short",
                    "size": abs(amt),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),
                    "liquidation_price": pos.get("liquidationPrice"),
                    "raw_data": pos,
                }
        return None

    async def close_position(self, symbol: str, position: Dict = None, **kwargs):
        if not position:
            position = await self.get_position(symbol)
        if not position:
            return None

        close_side = "SELL" if position["side"] == "long" else "BUY"
        return await self.create_order(
            symbol, close_side, position["size"],
            order_type="market", is_reduce_only=True,
        )

    async def get_collateral(self) -> Dict:
        data = await self._signed_get("/fapi/v3/balance", {})
        if not data:
            return {"total_collateral": 0, "available_collateral": 0}

        total = 0
        available = 0
        for asset in data:
            if asset.get("asset") == "USDT":
                total = float(asset.get("balance", 0))
                available = float(asset.get("availableBalance", 0))
                break
        return {"total_collateral": total, "available_collateral": available}

    async def update_leverage(self, symbol: str, leverage: int = None, margin_mode: str = None):
        if leverage:
            await self._signed_post("/fapi/v3/leverage", {
                "symbol": symbol, "leverage": str(leverage)
            })
        if margin_mode:
            await self._signed_post("/fapi/v3/marginType", {
                "symbol": symbol, "marginType": margin_mode.upper()
            })

    async def get_open_orders(self, symbol: str) -> List:
        data = await self._signed_get("/fapi/v3/openOrders", {"symbol": symbol})
        return data or []

    async def cancel_orders(self, symbol: str):
        await self._signed_delete("/fapi/v3/allOpenOrders", {"symbol": symbol})

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
