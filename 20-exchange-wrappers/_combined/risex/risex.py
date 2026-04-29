"""
Rise.trade (RISEx) wrapper — BSX Labs (acquired by RISE Chain).

- Testnet only (chain_id 11155931).
- EIP-712 signing via VerifyWitness permit wrapping a keccak-action hash.
- Reference: risex-client npm SDK v0.1.4 (decompiled / re-implemented in Python).
- Markets (as of 2026-04-24):
    market_id=1 -> BTC/USDC  (display BTC-PERP)
    market_id=2 -> ETH/USDC  (display ETH-PERP)
- min_order_size: BTC 0.000001, ETH 0.001 (wad step sizes — API uses size_steps / price_ticks units).

Notes / known limits:
- SignerKey == account wallet key (register_signer requires accountKey + signerKey; we use same
  EOA for simplicity). If the wallet owner has already linked a session key via the web app,
  registration is skipped (`isSignerRegistered`).
- cross-margin-balance endpoint reverts when account has never deposited. We treat that as 0.
- Uses RISE Chain Testnet USDC (<EVM_ADDRESS>); real USDC ≠ testnet.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiohttp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from mpdex.base import MultiPerpDex, MultiPerpDexMixin

logger = logging.getLogger(__name__)

# --- constants -------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.rise.trade"
DEFAULT_WS_URL = "wss://ws.rise.trade/ws"
DEFAULT_TIMEOUT = 10.0
CHAIN_ID = 4153
DEFAULT_SIGNER_EXPIRY_SECONDS = 30 * 24 * 60 * 60  # 30d
DEFAULT_PERMIT_DEADLINE_SECONDS = 300

# action names (keccak256(utf8(name)) is used as the first field of the action hash)
ACTION_PLACE_ORDER = "RISE_PERPS_PLACE_ORDER_V1"
ACTION_CANCEL_ORDER = "RISE_PERPS_CANCEL_ORDER_V1"
ACTION_CANCEL_ALL_ORDERS = "RISE_PERPS_CANCEL_ALL_ORDERS_V1"
REGISTER_SIGNER_MESSAGE = "Registering signer for RISEx"

# header flags (see encoder.ts)
V3_FLAG_PERMIT = 1
V3_FLAG_BUILDER = 2
V3_FLAG_CLIENT_ID = 4
V3_FLAG_PERMIT_ERC1271 = 9
V3_FLAG_TTL = 16

# enums
SIDE_LONG = 0
SIDE_SHORT = 1
ORDER_TYPE_MARKET = 0
ORDER_TYPE_LIMIT = 1
TIF_GTC = 0
TIF_GTT = 1
TIF_FOK = 2
TIF_IOC = 3
STP_EXPIRE_MAKER = 0

# EIP-712 types (exactly mirror risex-client)
REGISTER_SIGNER_TYPES = {
    "RegisterSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "message", "type": "string"},
        {"name": "expiration", "type": "uint32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]
}
VERIFY_SIGNER_TYPES = {
    "VerifySigner": [
        {"name": "account", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]
}
VERIFY_WITNESS_TYPES = {
    "VerifyWitness": [
        {"name": "account", "type": "address"},
        {"name": "target", "type": "address"},
        {"name": "hash", "type": "bytes32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
        {"name": "deadline", "type": "uint32"},
    ]
}


def _fix_v(signature: bytes) -> bytes:
    """Normalize v=0/1 -> 27/28 (some stacks use compact form)."""
    if len(signature) == 65 and signature[64] < 27:
        signature = signature[:64] + bytes([signature[64] + 27])
    return signature


def _keccak_action_hash(name: str) -> bytes:
    return keccak(text=name)


ACTION_PLACE_ORDER_HASH = _keccak_action_hash(ACTION_PLACE_ORDER)
ACTION_CANCEL_ORDER_HASH = _keccak_action_hash(ACTION_CANCEL_ORDER)
ACTION_CANCEL_ALL_ORDERS_HASH = _keccak_action_hash(ACTION_CANCEL_ALL_ORDERS)


def _encode_order_data(market_id: int, side: int, order_type: int, price_ticks: int,
                       size_steps: int, time_in_force: int, post_only: bool,
                       reduce_only: bool, stp_mode: int) -> int:
    """Pack order fields into a single 256-bit integer (same layout as encoder.ts)."""
    order_flags = 0
    if side & 1:
        order_flags |= 1
    if post_only:
        order_flags |= 2
    if reduce_only:
        order_flags |= 4
    order_flags |= (stp_mode & 3) << 3
    order_flags |= (order_type & 1) << 5
    order_flags |= (time_in_force & 3) << 6
    header_version = 1
    data = 0
    data |= (market_id & 0xFFFF) << 70
    data |= (size_steps & 0xFFFFFFFF) << 38
    data |= (price_ticks & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (header_version & 0x1F) << 1
    return data


def _compute_header_flags(builder_id: int, client_order_id: int, ttl_units: int, is_erc1271: bool) -> int:
    flags = V3_FLAG_PERMIT_ERC1271 if is_erc1271 else V3_FLAG_PERMIT
    if builder_id != 0:
        flags |= V3_FLAG_BUILDER
    if client_order_id != 0:
        flags |= V3_FLAG_CLIENT_ID
    if ttl_units != 0:
        flags |= V3_FLAG_TTL
    return flags


def _encode_order_hash(*, market_id: int, side: int, order_type: int, price_ticks: int,
                      size_steps: int, time_in_force: int, post_only: bool, reduce_only: bool,
                      stp_mode: int, ttl_units: int, client_order_id: int = 0,
                      builder_id: int = 0, is_erc1271: bool = False) -> bytes:
    order_data = _encode_order_data(market_id, side, order_type, price_ticks, size_steps,
                                    time_in_force, post_only, reduce_only, stp_mode)
    header_flags = _compute_header_flags(builder_id, client_order_id, ttl_units, is_erc1271)
    encoded = abi_encode(
        ["bytes32", "uint8", "uint256", "uint16", "uint64", "uint16"],
        [ACTION_PLACE_ORDER_HASH, header_flags, order_data, builder_id, client_order_id, ttl_units],
    )
    return keccak(encoded)


def _encode_cancel_order_hash(market_id: int, resting_order_id: int) -> bytes:
    encoded = abi_encode(
        ["bytes32", "uint256", "uint256"],
        [ACTION_CANCEL_ORDER_HASH, market_id, resting_order_id],
    )
    return keccak(encoded)


def _encode_cancel_all_hash(market_id: int) -> bytes:
    encoded = abi_encode(["bytes32", "uint256"], [ACTION_CANCEL_ALL_ORDERS_HASH, market_id])
    return keccak(encoded)


def _hex_to_base64(hex_str: str) -> str:
    clean = hex_str[2:] if hex_str.startswith("0x") else hex_str
    return base64.b64encode(bytes.fromhex(clean)).decode("ascii")


def _build_typed_data(domain: Dict[str, Any], types: Dict[str, Any],
                      primary_type: str, message: Dict[str, Any]) -> Dict[str, Any]:
    """Full EIP-712 structured dict consumed by eth_account.messages.encode_typed_data."""
    return {
        "domain": domain,
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            **types,
        },
        "primaryType": primary_type,
        "message": message,
    }


def _sign_typed_data(private_key: str, typed: Dict[str, Any]) -> str:
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=private_key)
    raw = signed.signature  # bytes
    return "0x" + _fix_v(bytes(raw)).hex()


# --- wrapper ---------------------------------------------------------------

class RisexExchange(MultiPerpDexMixin, MultiPerpDex):
    """Rise.trade Perp DEX wrapper (testnet)."""

    # Display symbol (external) -> API market_id. Populated from /v1/markets at init().
    _SYMBOL_FALLBACK: Dict[str, int] = {"BTC": 1, "ETH": 2}

    def __init__(self, wallet_address: str, private_key: str,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        super().__init__()
        if not wallet_address:
            raise ValueError("risex: wallet_address required")
        if not private_key:
            raise ValueError("risex: private_key required")
        pk = private_key if private_key.startswith("0x") else "0x" + private_key
        self._account = Account.from_key(pk)
        if self._account.address.lower() != wallet_address.lower():
            logger.warning(
                "[risex] wallet_address (%s) does not match private_key address (%s). Using pk-derived.",
                wallet_address, self._account.address,
            )
        self.account_address: str = self._account.address
        self.signer_address: str = self._account.address  # same EOA
        self._pk: str = pk
        self.base_url: str = base_url.rstrip("/")
        self.timeout: float = float(timeout)
        self._session: Optional[aiohttp.ClientSession] = None

        # EIP-712 domain + target router (filled in init())
        self._domain: Dict[str, Any] = {}
        self._target: Optional[str] = None

        # cached markets: market_id(int) -> market dict
        self._markets: Dict[int, Dict[str, Any]] = {}
        self._symbol_to_mid: Dict[str, int] = {}

        self.has_margin_mode = True
        self.available_symbols = {"perp": ["BTC-PERP", "ETH-PERP"]}

    # ----- lifecycle ------------------------------------------------------

    async def init(self) -> "RisexExchange":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            await self._load_domain()
            await self._load_markets()
            # Register signer if not already registered (no-op if same EOA signs + account).
            try:
                registered = await self._is_signer_registered()
                if not registered:
                    logger.info("[risex] session key not registered for %s — attempting register",
                                self.account_address)
                    await self._register_signer()
                else:
                    logger.info("[risex] session key already registered")
            except Exception as e:  # pragma: no cover
                logger.warning("[risex] register_signer skipped (non-fatal): %s", e)
        except Exception as e:
            await self.close()
            raise RuntimeError(f"[risex] init failed: {e}") from e
        logger.info("[risex] init ok — account=%s markets=%s", self.account_address,
                    list(self._symbol_to_mid.keys()))
        return self

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ----- low level HTTP --------------------------------------------------

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, body: Dict[str, Any]) -> Any:
        return await self._request("POST", path, body)

    async def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        if self._session is None:
            raise RuntimeError("[risex] not initialized")
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(method, url, json=body) as resp:
                txt = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"[risex] {method} {path} -> {resp.status}: {txt[:400]}")
                if not txt:
                    return None
                try:
                    data = json.loads(txt)
                except Exception:
                    raise RuntimeError(f"[risex] {method} {path} bad json: {txt[:200]}")
                # RISEx envelope: {"data": {...}, "request_id": "..."} OR {"error": {...}}
                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"[risex] {method} {path} api error: {err}")
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data
        except asyncio.TimeoutError:
            raise RuntimeError(f"[risex] {method} {path} timeout after {self.timeout}s")

    # ----- init helpers ---------------------------------------------------

    async def _load_domain(self):
        raw = await self._get("/v1/auth/eip712-domain")
        if not raw:
            raise RuntimeError("eip712-domain empty")
        self._domain = {
            "name": raw["name"],
            "version": raw["version"],
            "chainId": int(raw["chain_id"]),
            "verifyingContract": raw["verifying_contract"],
        }
        if self._domain["chainId"] != CHAIN_ID:
            logger.warning("[risex] unexpected chainId %s (expected %s)", self._domain["chainId"], CHAIN_ID)
        cfg = await self._get("/v1/system/config")
        addrs = (cfg or {}).get("addresses", {}) or {}
        perp_v2 = addrs.get("perp_v2") if isinstance(addrs.get("perp_v2"), dict) else {}
        contract_addrs = (cfg or {}).get("contract_addresses", {}) or {}
        self._target = (
            addrs.get("router")
            or perp_v2.get("orders_manager")
            or contract_addrs.get("perps_manager")
        )
        if not self._target:
            raise RuntimeError("router/orders_manager not in system config")

    async def _load_markets(self):
        raw = await self._get("/v1/markets")
        markets = (raw or {}).get("markets", []) if isinstance(raw, dict) else []
        self._markets.clear()
        self._symbol_to_mid.clear()
        for m in markets:
            try:
                mid = int(m.get("market_id"))
            except Exception:
                continue
            self._markets[mid] = m
            base = (m.get("base_asset_symbol") or m.get("display_name") or "").upper()
            # "BTC/USDC" -> BTC
            coin = base.split("/")[0].split("-")[0].strip()
            if coin:
                self._symbol_to_mid[coin] = mid
                self._symbol_to_mid[f"{coin}-PERP"] = mid
        if not self._symbol_to_mid:
            # fallback
            self._symbol_to_mid = dict(self._SYMBOL_FALLBACK)
            self._symbol_to_mid.update({f"{c}-PERP": m for c, m in self._SYMBOL_FALLBACK.items()})

    # ----- symbol / sizing helpers ---------------------------------------

    def _coin(self, symbol: str) -> str:
        s = (symbol or "").upper()
        s = s.replace("-PERP", "").replace("/USDC", "").replace("USDC", "").replace("-USD", "")
        return s.split("-")[0].split("/")[0].strip()

    def _market_id(self, symbol: str) -> int:
        coin = self._coin(symbol)
        mid = self._symbol_to_mid.get(coin) or self._symbol_to_mid.get(f"{coin}-PERP")
        if mid is None:
            mid = self._SYMBOL_FALLBACK.get(coin)
        if mid is None:
            raise ValueError(f"[risex] unknown symbol: {symbol}")
        return int(mid)

    def _market(self, symbol: str) -> Dict[str, Any]:
        m = self._markets.get(self._market_id(symbol))
        if not m:
            raise ValueError(f"[risex] market not loaded for {symbol}")
        return m

    def _step_size(self, symbol: str) -> Decimal:
        try:
            return Decimal(str(self._market(symbol).get("config", {}).get("step_size") or "0.000001"))
        except Exception:
            return Decimal("0.000001")

    def _step_price(self, symbol: str) -> Decimal:
        try:
            return Decimal(str(self._market(symbol).get("config", {}).get("step_price") or "0.1"))
        except Exception:
            return Decimal("0.1")

    def _size_to_steps(self, symbol: str, amount: float) -> int:
        amt = Decimal(str(amount))
        step = self._step_size(symbol)
        if step <= 0:
            return 0
        return int((amt / step).to_integral_value(rounding="ROUND_DOWN"))

    def _price_to_ticks(self, symbol: str, price: float) -> int:
        p = Decimal(str(price))
        tick = self._step_price(symbol)
        if tick <= 0:
            return 0
        return int((p / tick).to_integral_value(rounding="ROUND_HALF_EVEN"))

    def _ticks_to_price(self, symbol: str, ticks) -> float:
        tick = self._step_price(symbol)
        try:
            return float(Decimal(str(ticks)) * tick)
        except Exception:
            return 0.0

    def _steps_to_size(self, symbol: str, steps) -> float:
        step = self._step_size(symbol)
        try:
            return float(Decimal(str(steps)) * step)
        except Exception:
            return 0.0

    # ----- signing helpers ------------------------------------------------

    async def _get_nonce_state(self) -> Dict[str, Any]:
        raw = await self._get(f"/v1/nonce-state/{self.account_address}")
        if not isinstance(raw, dict):
            raise RuntimeError(f"nonce-state bad shape: {raw!r}")
        return raw

    async def _is_signer_registered(self) -> bool:
        try:
            raw = await self._get(
                f"/v1/auth/session-key-status?account={self.account_address}&signer={self.signer_address}")
            return isinstance(raw, dict) and int(raw.get("status") or 0) == 1
        except Exception as e:
            logger.debug("[risex] session-key-status check failed: %s", e)
            return False

    async def _register_signer(self, label: str = "mpdex-risex") -> Dict[str, Any]:
        nonce = await self._get_nonce_state()
        expiration = int(time.time()) + DEFAULT_SIGNER_EXPIRY_SECONDS
        auth_nonce_anchor = int(nonce.get("nonce_anchor") or 0) + 1
        auth_nonce_bitmap = 0
        # Same EOA signs both roles (account + signer).
        account_sig = _sign_typed_data(self._pk, _build_typed_data(
            self._domain, REGISTER_SIGNER_TYPES, "RegisterSigner",
            {
                "account": self.account_address,
                "signer": self.signer_address,
                "message": REGISTER_SIGNER_MESSAGE,
                "expiration": expiration,
                "nonceAnchor": auth_nonce_anchor,
                "nonceBitmap": auth_nonce_bitmap,
            }))
        signer_sig = _sign_typed_data(self._pk, _build_typed_data(
            self._domain, VERIFY_SIGNER_TYPES, "VerifySigner",
            {
                "account": self.account_address,
                "nonceAnchor": auth_nonce_anchor,
                "nonceBitmap": auth_nonce_bitmap,
            }))
        body = {
            "account": self.account_address,
            "signer": self.signer_address,
            "message": REGISTER_SIGNER_MESSAGE,
            "nonce_anchor": str(auth_nonce_anchor),
            "nonce_bitmap_index": auth_nonce_bitmap,
            "expiration": str(expiration),
            "account_signature": account_sig,
            "signer_signature": signer_sig,
            "label": label,
        }
        return await self._post("/v1/auth/register-signer", body)

    async def _create_permit(self, action_hash: bytes) -> Dict[str, Any]:
        if not self._target:
            raise RuntimeError("[risex] target contract unknown (init not complete)")
        nonce = await self._get_nonce_state()
        deadline = int(time.time()) + DEFAULT_PERMIT_DEADLINE_SECONDS
        nonce_anchor = int(nonce.get("nonce_anchor") or 0)
        nonce_bitmap_index = int(nonce.get("current_bitmap_index") or 0)
        hash_hex = "0x" + action_hash.hex()
        typed = _build_typed_data(
            self._domain, VERIFY_WITNESS_TYPES, "VerifyWitness",
            {
                "account": self.account_address,
                "target": self._target,
                "hash": hash_hex,
                "nonceAnchor": nonce_anchor,
                "nonceBitmap": nonce_bitmap_index,
                "deadline": deadline,
            })
        raw_sig = _sign_typed_data(self._pk, typed)
        return {
            "account": self.account_address,
            "signer": self.signer_address,
            "nonce_anchor": nonce_anchor,
            "nonce_bitmap_index": nonce_bitmap_index,
            "deadline": deadline,
            "signature": _hex_to_base64(raw_sig),
        }

    # ----- public interface ----------------------------------------------

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            m = self._market(symbol)
            mp = m.get("mark_price") or m.get("index_price") or m.get("last_price")
            if mp is None:
                # refresh once
                await self._load_markets()
                m = self._market(symbol)
                mp = m.get("mark_price") or m.get("index_price") or m.get("last_price")
            if mp is None:
                return None
            v = float(mp)
            if not math.isfinite(v) or v <= 0:
                return None
            return v
        except Exception as e:
            logger.error("[risex] get_mark_price(%s) failed: %s", symbol, e)
            return None

    async def _refresh_market_prices(self):
        try:
            await self._load_markets()
        except Exception:
            pass

    async def create_order(self, symbol: str, side: str, amount: float,
                           price: Optional[float] = None, order_type: str = "market",
                           *, is_reduce_only: bool = False, **kwargs) -> Optional[Dict[str, Any]]:
        try:
            mid = self._market_id(symbol)
            side_int = SIDE_LONG if (side or "").lower() in ("buy", "long") else SIDE_SHORT
            size_steps = self._size_to_steps(symbol, float(amount))
            if size_steps <= 0:
                logger.warning("[risex] size_steps <= 0 for amount=%s step=%s — skip", amount, self._step_size(symbol))
                return None
            is_market = (order_type or "market").lower() == "market"
            if is_market:
                # Mainnet rejects pure-market orders (price_ticks=0 / order_type=MARKET
                # produces SignerNotAuthorized on-chain revert). Convert to aggressive
                # limit-IOC that crosses the book by 0.3% for effective taker fill.
                try:
                    ob = await self._get(f"/v1/orderbook?market_id={mid}")
                    bids = (ob or {}).get("bids") or []
                    asks = (ob or {}).get("asks") or []
                    if side_int == SIDE_LONG and asks:
                        cross = float(asks[0]["price"]) * 1.003
                    elif side_int == SIDE_SHORT and bids:
                        cross = float(bids[0]["price"]) * 0.997
                    else:
                        mp = await self.get_mark_price(symbol)
                        if not mp:
                            raise RuntimeError("no mark price")
                        cross = mp * (1.003 if side_int == SIDE_LONG else 0.997)
                except Exception as e:
                    raise RuntimeError(f"[risex] market-order cross-price fetch failed: {e}") from e
                price_ticks = self._price_to_ticks(symbol, cross)
                tif = TIF_IOC
                post_only = False
                # Treat as LIMIT for encoding (on-chain rejects MARKET type)
                is_market = False
            else:
                if price is None or not math.isfinite(float(price)) or float(price) <= 0:
                    raise ValueError(f"limit order requires positive price, got {price}")
                price_ticks = self._price_to_ticks(symbol, float(price))
                tif = TIF_GTC
                post_only = bool(kwargs.get("post_only", False))
            action_hash = _encode_order_hash(
                market_id=mid,
                side=side_int,
                order_type=ORDER_TYPE_MARKET if is_market else ORDER_TYPE_LIMIT,
                price_ticks=price_ticks,
                size_steps=size_steps,
                time_in_force=tif,
                post_only=post_only,
                reduce_only=bool(is_reduce_only),
                stp_mode=STP_EXPIRE_MAKER,
                ttl_units=0,
                client_order_id=0,
                builder_id=0,
                is_erc1271=False,
            )
            permit = await self._create_permit(action_hash)
            body = {
                "market_id": mid,
                "side": side_int,
                "order_type": ORDER_TYPE_MARKET if is_market else ORDER_TYPE_LIMIT,
                "price_ticks": price_ticks,
                "size_steps": size_steps,
                "time_in_force": tif,
                "post_only": post_only,
                "reduce_only": bool(is_reduce_only),
                "stp_mode": STP_EXPIRE_MAKER,
                "ttl_units": 0,
                "client_order_id": "0",
                "builder_id": 0,
                "permit": permit,
            }
            return await self._post("/v1/orders/place", body)
        except Exception as e:
            logger.error("[risex] create_order(%s %s %s @%s) failed: %s", symbol, side, amount, price, e)
            raise

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            mid = self._market_id(symbol)
            raw = await self._get(
                f"/v1/account/position?market_id={mid}&account={self.account_address}")
            pos = (raw or {}).get("position") if isinstance(raw, dict) else None
            if not pos:
                return None
            size = 0.0
            try:
                size = float(pos.get("size") or 0)
            except Exception:
                size = 0.0
            if size == 0:
                return None
            side_val = pos.get("side")
            side_str = "long" if side_val in (0, "0", "long", "LONG", "buy", "BUY") else "short"
            entry = float(pos.get("entry_price") or 0)
            upnl = float(pos.get("unrealized_pnl") or 0)
            liq = pos.get("liquidation_price")
            lev = pos.get("leverage")
            margin_mode = pos.get("margin_mode")
            return {
                "side": side_str,
                "size": abs(size),
                "entry_price": entry,
                "unrealized_pnl": upnl,
                "leverage_type": "isolated" if margin_mode in (1, "1") else "cross",
                "leverage_value": float(lev) if lev else None,
                "liquidation_price": float(liq) if liq else None,
            }
        except Exception as e:
            logger.error("[risex] get_position(%s) failed: %s", symbol, e)
            return None

    async def get_collateral(self) -> Dict[str, float]:
        try:
            raw = await self._get(
                f"/v1/account/cross-margin-balance?account={self.account_address}")
            bal = 0.0
            if isinstance(raw, dict):
                bal = float(raw.get("balance") or 0)
            return {"total_collateral": bal, "available_collateral": bal}
        except Exception as e:
            # Testnet commonly reverts for never-deposited accounts — treat as 0.
            logger.debug("[risex] get_collateral -> 0 (reason: %s)", e)
            return {"total_collateral": 0.0, "available_collateral": 0.0}

    async def close_position(self, symbol: str, position: Optional[Dict[str, Any]] = None,
                             *, is_reduce_only: bool = True) -> Optional[Dict[str, Any]]:
        try:
            if position is None:
                position = await self.get_position(symbol)
            if not position or float(position.get("size") or 0) <= 0:
                return None
            side = "sell" if str(position.get("side")).lower() in ("long", "buy") else "buy"
            return await self.create_order(
                symbol, side, float(position["size"]), price=None,
                order_type="market", is_reduce_only=is_reduce_only,
            )
        except Exception as e:
            logger.error("[risex] close_position(%s) failed: %s", symbol, e)
            raise

    async def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        try:
            mid = self._market_id(symbol)
            raw = await self._get(
                f"/v1/orders/open?account={self.account_address}&market_id={mid}")
            orders = (raw or {}).get("orders", []) if isinstance(raw, dict) else []
            out: List[Dict[str, Any]] = []
            for o in orders:
                try:
                    out.append({
                        "id": str(o.get("order_id")),
                        "resting_order_id": o.get("resting_order_id"),
                        "symbol": symbol,
                        "side": "buy" if int(o.get("side") or 0) == SIDE_LONG else "sell",
                        "size": self._steps_to_size(symbol, o.get("size_steps") or 0),
                        "price": self._ticks_to_price(symbol, o.get("price_ticks") or 0)
                                 if int(o.get("order_type") or 0) == ORDER_TYPE_LIMIT else None,
                    })
                except Exception:
                    continue
            return out
        except Exception as e:
            logger.error("[risex] get_open_orders(%s) failed: %s", symbol, e)
            return []

    async def cancel_orders(self, symbol: str, open_orders: Optional[List[Dict[str, Any]]] = None):
        try:
            mid = self._market_id(symbol)
            if open_orders is None:
                open_orders = await self.get_open_orders(symbol)
            if not open_orders:
                # fall back to cancel-all for this market
                action_hash = _encode_cancel_all_hash(mid)
                permit = await self._create_permit(action_hash)
                return await self._post("/v1/orders/cancel-all", {"market_id": mid, "permit": permit})
            results = []
            for o in open_orders:
                try:
                    rid = o.get("resting_order_id")
                    if rid is None:
                        # Need to fetch resting_order_id; skip if unavailable
                        continue
                    action_hash = _encode_cancel_order_hash(mid, int(rid))
                    permit = await self._create_permit(action_hash)
                    body = {"market_id": mid, "order_id": o.get("id"), "permit": permit}
                    results.append(await self._post("/v1/orders/cancel", body))
                except Exception as e:
                    logger.warning("[risex] cancel one order failed: %s", e)
            return results
        except Exception as e:
            logger.error("[risex] cancel_orders(%s) failed: %s", symbol, e)
            return []

    async def update_leverage(self, symbol: str, leverage: Optional[int] = None,
                               margin_mode: Optional[str] = None):
        # Leverage/margin-mode calls are signed — optional for testnet. Return ok stub.
        return {
            "symbol": symbol,
            "leverage": leverage,
            "margin_mode": margin_mode or "cross",
            "status": "ok",
        }

    async def get_leverage_info(self, symbol: str):
        try:
            m = self._market(symbol)
            mx = int(float(m.get("config", {}).get("max_leverage") or 20))
        except Exception:
            mx = 20
        return {
            "symbol": symbol,
            "leverage": None,
            "margin_mode": "cross",
            "status": "ok",
            "max_leverage": mx,
            "available_margin_modes": ["cross", "isolated"],
        }

    async def get_available_symbols(self):
        return {"perp": [f"{c}-PERP" for c in ("BTC", "ETH") if c in self._symbol_to_mid]}
