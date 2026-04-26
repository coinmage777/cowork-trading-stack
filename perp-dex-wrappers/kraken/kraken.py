import asyncio
import base64
import hashlib
import hmac
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

import aiohttp

from mpdex.base import MultiPerpDex, MultiPerpDexMixin


# --- Symbol mapping: mpdex coin key -> Kraken Futures linear perp ---
SYMBOL_MAP: Dict[str, str] = {
    'BTC': 'PF_XBTUSD',
    'ETH': 'PF_ETHUSD',
    'SOL': 'PF_SOLUSD',
    'HYPE': 'PF_HYPEUSD',
    'ENA': 'PF_ENAUSD',
    'XRP': 'PF_XRPUSD',
    'DOGE': 'PF_DOGEUSD',
    'LINK': 'PF_LINKUSD',
    'AVAX': 'PF_AVAXUSD',
    'SUI': 'PF_SUIUSD',
}

# Inverse, for display / parsing
INVERSE_SYMBOL_MAP: Dict[str, str] = {v: k for k, v in SYMBOL_MAP.items()}


class KrakenFuturesExchange(MultiPerpDexMixin, MultiPerpDex):
    """
    Kraken Futures (kraken.com/futures) linear perpetuals wrapper.

    - Auth: APIKey + Nonce (millisec) + Authent header.
      Authent = Base64(HMAC_SHA512(Base64Decode(private_key),
                                   sha256(postData + nonce + endpoint_path)))
      where endpoint_path is the path AFTER "/derivatives" (i.e. "/api/v3/...").
    - Contract size: PF_XXXUSD linear contracts have a fixed $1 notional per contract,
      so the on-wire `size` parameter is integer USD notional (NOT coin units).
      We convert mpdex coin-unit `amount` to USD via mark price internally.
    - We intentionally do NOT implement websocket streaming in this first cut
      (no `KrakenFuturesWSClient`). All reads go via REST. Funding-rate-arb usage
      is low-frequency so REST is sufficient.
    """

    DEFAULT_TIMEOUT = 10  # seconds
    BASE_URL = "https://futures.kraken.com/derivatives"

    def __init__(self, api_key: str, private_key: str):
        super().__init__()
        self.has_spot = False
        self.API_KEY = api_key
        self.PRIVATE_KEY = private_key  # base64-encoded secret as issued by Kraken
        self.COLLATERAL_SYMBOL = 'USD'

        # All paths are relative to BASE_URL; signature uses the path part.
        # Kraken expects the path portion that begins with "/api/v3/..." for the
        # SHA-256 preimage of Authent.
        self._session: Optional[aiohttp.ClientSession] = None

        # WS flags — all False, REST only for now
        self.ws_supported = {
            "get_mark_price": False,
            "get_position": False,
            "get_open_orders": False,
            "get_collateral": False,
            "get_orderbook": False,
            "create_order": False,
            "cancel_orders": False,
            "update_leverage": False,
        }

        # Decoded secret bytes, cached
        try:
            self._secret_bytes = base64.b64decode(self.PRIVATE_KEY)
        except Exception as exc:
            raise ValueError(f"kraken: private_key must be base64-encoded: {exc}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def init(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
        )
        await self.update_avaiable_symbols()
        return self

    async def close(self, force_close: bool = True):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
            )
        return self._session

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_decimal(v) -> Decimal:
        if isinstance(v, Decimal):
            return v
        if isinstance(v, float):
            return Decimal(format(v, 'f'))
        return Decimal(str(v))

    def _resolve_symbol(self, symbol: str) -> str:
        """
        Accepts either an mpdex coin key ('BTC', 'HYPE') or a native kraken symbol
        ('PF_XBTUSD'). Returns the kraken native symbol.
        """
        if not symbol:
            raise ValueError("kraken: empty symbol")
        s = symbol.strip()
        if s.upper().startswith("PF_"):
            return s.upper()
        # strip common pair suffixes, e.g. 'BTC-USD' / 'BTC/USDC'
        base = s.upper().replace("/", "-").split("-")[0]
        mapped = SYMBOL_MAP.get(base)
        if mapped is None:
            # Last resort: try PF_<base>USD
            return f"PF_{base}USD"
        return mapped

    def _coin_key(self, kraken_symbol: str) -> str:
        return INVERSE_SYMBOL_MAP.get(kraken_symbol, kraken_symbol)

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------
    def _sign(self, endpoint_path: str, nonce: str, post_data: str) -> str:
        """
        Kraken Futures Authent:
            step1 = sha256(post_data + nonce + endpoint_path).digest()
            authent = base64(hmac_sha512(base64_decode(secret), step1))

        endpoint_path must be the path AFTER the host (e.g. "/api/v3/sendorder"),
        NOT including the "/derivatives" prefix (per Kraken docs).
        """
        if endpoint_path.startswith("/derivatives"):
            endpoint_path = endpoint_path[len("/derivatives"):]

        message = (post_data + nonce + endpoint_path).encode("utf-8")
        sha = hashlib.sha256(message).digest()
        mac = hmac.new(self._secret_bytes, sha, hashlib.sha512).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _auth_headers(self, endpoint_path: str, post_data: str = "") -> Dict[str, str]:
        nonce = str(int(time.time() * 1000))
        authent = self._sign(endpoint_path, nonce, post_data)
        return {
            "APIKey": self.API_KEY,
            "Nonce": nonce,
            "Authent": authent,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @staticmethod
    def _encode_params(params: Dict[str, Any]) -> str:
        """
        Kraken Futures signs the raw urlencoded form body. Keep insertion order
        stable; Kraken does not mandate sorted keys but signatures must match
        what is sent on the wire, so we use the same string both places.
        """
        parts: List[str] = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            parts.append(f"{k}={v}")
        return "&".join(parts)

    # ------------------------------------------------------------------
    # Low level HTTP
    # ------------------------------------------------------------------
    async def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.BASE_URL}{path}"
        async with session.get(url, params=params or {}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"kraken public GET {path} failed {resp.status}: {data}"
                )
            return data

    async def _private_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._ensure_session()
        # For GET endpoints Kraken signs an empty postData. Query string is
        # appended to the URL but not included in the signature preimage.
        post_data = ""
        headers = self._auth_headers(path, post_data)
        url = f"{self.BASE_URL}{path}"
        async with session.get(url, headers=headers, params=params or {}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"kraken private GET {path} failed {resp.status}: {data}"
                )
            return data

    async def _private_post(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        session = await self._ensure_session()
        body = self._encode_params(params or {})
        headers = self._auth_headers(path, body)
        url = f"{self.BASE_URL}{path}"
        async with session.post(url, headers=headers, data=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"kraken private POST {path} failed {resp.status}: {data}"
                )
            return data

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------
    async def update_avaiable_symbols(self):
        self.available_symbols['perp'] = []
        self.available_symbols['spot'] = []
        try:
            data = await self._public_get("/api/v3/instruments")
        except Exception:
            # Non-fatal; fall back to our static map so the wrapper still initialises
            for k in SYMBOL_MAP:
                self.available_symbols['perp'].append(f"{k}-USD")
            return
        instruments = data.get("instruments") or []
        for inst in instruments:
            sym = inst.get("symbol") or ""
            tradeable = inst.get("tradeable", True)
            if not sym.upper().startswith("PF_"):
                continue
            if not tradeable:
                continue
            coin = self._coin_key(sym.upper())
            composite = f"{coin}-USD" if coin != sym.upper() else sym
            self.available_symbols['perp'].append(composite)

    def get_perp_quote(self, symbol, *, is_basic_coll=False):
        return 'USD'

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    async def get_mark_price(self, symbol) -> float:
        kraken_sym = self._resolve_symbol(symbol)
        # /api/v3/tickers returns all tickers; /api/v3/tickers/<sym> is supported too
        try:
            data = await self._public_get(f"/api/v3/tickers/{kraken_sym}")
        except Exception:
            # Some deployments reject the single-symbol path; fall back to full list
            data = await self._public_get("/api/v3/tickers")

        ticker = None
        if isinstance(data, dict):
            if "ticker" in data and isinstance(data["ticker"], dict):
                ticker = data["ticker"]
            elif "tickers" in data and isinstance(data["tickers"], list):
                for t in data["tickers"]:
                    if (t.get("symbol") or "").upper() == kraken_sym:
                        ticker = t
                        break
        if ticker is None:
            raise RuntimeError(f"kraken: no ticker for {kraken_sym}")

        # Kraken exposes markPrice explicitly for PF_ instruments; fall back to
        # last / mid if markPrice is missing or zero.
        raw = (
            ticker.get("markPrice")
            or ticker.get("last")
            or ticker.get("mark")
            or ticker.get("bid")
        )
        try:
            price = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0

        if price <= 0:
            raise ValueError(
                f"kraken: invalid mark price for {kraken_sym} (got {raw!r})"
            )
        return price

    async def get_orderbook(self, symbol) -> Optional[Dict[str, Any]]:
        kraken_sym = self._resolve_symbol(symbol)
        data = await self._public_get("/api/v3/orderbook", {"symbol": kraken_sym})
        ob = data.get("orderBook") or {}
        return {
            "symbol": kraken_sym,
            "bids": ob.get("bids", []),
            "asks": ob.get("asks", []),
        }

    # ------------------------------------------------------------------
    # Account / position
    # ------------------------------------------------------------------
    async def get_collateral(self) -> float:
        """
        Returns total USD equity across Kraken Futures multi-collateral accounts.

        /api/v3/accounts response shape (abridged):
            {
              "result": "success",
              "accounts": {
                "flex": {
                  "type": "multiCollateralMarginAccount",
                  "portfolioValue": "1234.56",
                  "balanceValue": "1000.00",
                  "totalEquity": "1234.56",
                  "availableMargin": "800.00",
                  ...
                },
                "cash": { "type": "cashAccount", "balances": {"USD": "0"} },
                ...
              }
            }

        We prefer `flex.portfolioValue` (multi-collateral USD equity). If only the
        legacy per-asset margin accounts are present (e.g. `fi_xbtusd`), we sum
        `auxiliary.portfolioValue` across them.
        """
        data = await self._private_post("/api/v3/accounts", {})
        accounts = data.get("accounts") or {}

        # Preferred: multi-collateral flex account
        flex = accounts.get("flex")
        if isinstance(flex, dict):
            for key in ("portfolioValue", "totalEquity", "balanceValue"):
                val = flex.get(key)
                if val is not None:
                    try:
                        return round(float(val), 2)
                    except (TypeError, ValueError):
                        pass

        # Fallback: sum legacy per-product futures accounts (fi_xbtusd, etc.)
        total = 0.0
        found_any = False
        for name, acct in accounts.items():
            if not isinstance(acct, dict):
                continue
            aux = acct.get("auxiliary") or {}
            pv = aux.get("portfolioValue")
            if pv is None:
                pv = acct.get("balanceValue")
            if pv is None:
                continue
            try:
                total += float(pv)
                found_any = True
            except (TypeError, ValueError):
                continue
        if found_any:
            return round(total, 2)

        # Final fallback: explicit cash USD balance
        cash = accounts.get("cash") or {}
        balances = cash.get("balances") or {}
        try:
            return round(float(balances.get("USD", 0) or 0), 2)
        except (TypeError, ValueError):
            return 0.0

    def _parse_position(self, pos: Dict[str, Any]) -> Dict[str, Any]:
        side_raw = (pos.get("side") or "").lower()
        # Kraken openpositions `side` is "long"/"short" directly
        side = "long" if side_raw.startswith("l") else "short"
        try:
            size = abs(float(pos.get("size") or 0))
        except (TypeError, ValueError):
            size = 0.0
        try:
            entry = float(pos.get("price") or pos.get("fillPrice") or 0)
        except (TypeError, ValueError):
            entry = 0.0

        kraken_sym = (pos.get("symbol") or "").upper()
        return {
            "symbol": kraken_sym,
            "coin": self._coin_key(kraken_sym),
            "side": side,
            "size": size,
            "entry_price": entry,
            "unrealized_pnl": pos.get("unrealizedFunding") or pos.get("pnl") or 0,
            "liquidation_price": pos.get("liquidationThreshold"),
            "raw_data": pos,
        }

    async def get_position(self, symbol) -> Dict[str, Any]:
        kraken_sym = self._resolve_symbol(symbol)
        data = await self._private_post("/api/v3/openpositions", {})
        positions = data.get("openPositions") or []
        for pos in positions:
            if (pos.get("symbol") or "").upper() == kraken_sym:
                parsed = self._parse_position(pos)
                if parsed["size"] <= 0:
                    return {}
                return parsed
        return {}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _parse_orders(self, orders) -> List[Dict[str, Any]]:
        if not orders:
            return []
        if isinstance(orders, dict):
            orders = [orders]
        out: List[Dict[str, Any]] = []
        for o in orders:
            out.append({
                "id": o.get("order_id") or o.get("orderId") or o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "size": o.get("unfilledSize") or o.get("size") or o.get("filledSize"),
                "price": o.get("limitPrice") or o.get("price"),
                "order_type": o.get("orderType") or o.get("type"),
            })
        return out

    async def get_open_orders(self, symbol) -> List[Dict[str, Any]]:
        kraken_sym = self._resolve_symbol(symbol)
        data = await self._private_post("/api/v3/openorders", {})
        orders = data.get("openOrders") or []
        filtered = [
            o for o in orders
            if (o.get("symbol") or "").upper() == kraken_sym
        ]
        return self._parse_orders(filtered)

    async def create_order(
        self,
        symbol,
        side,
        amount,
        price: Optional[float] = None,
        order_type: str = 'market',
        *,
        is_reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Args:
            symbol: mpdex coin key ('BTC') or native ('PF_XBTUSD').
            side:   'buy' or 'sell'.
            amount: size in COIN UNITS (e.g. 0.01 for 0.01 BTC). We convert to
                    integer USD contracts internally using current mark price,
                    since Kraken PF_ contracts are fixed $1 notional.
            price:  optional limit price in USD. If supplied, order_type is forced to 'lmt'.
        """
        if amount is None or float(amount) <= 0:
            raise ValueError(f"kraken: invalid order amount {amount!r}")

        kraken_sym = self._resolve_symbol(symbol)
        side_norm = str(side).lower().strip()
        if side_norm not in ("buy", "sell"):
            raise ValueError(f"kraken: invalid side {side!r}")

        # Force limit order type when a price is provided (matches backpack behaviour)
        if price is not None:
            order_type = 'limit'
        ot = 'lmt' if str(order_type).lower().startswith('l') else 'mkt'

        mark = await self.get_mark_price(kraken_sym)  # raises on 0/negative
        notional_usd = float(amount) * mark
        size_contracts = int(Decimal(format(notional_usd, 'f')).to_integral_value(rounding=ROUND_DOWN))
        if size_contracts <= 0:
            raise ValueError(
                f"kraken: size rounds to 0 contracts "
                f"(amount={amount}, mark={mark}, notional=${notional_usd:.4f})"
            )

        params: Dict[str, Any] = {
            "orderType": ot,
            "symbol": kraken_sym,
            "side": side_norm,
            "size": size_contracts,
            "reduceOnly": bool(is_reduce_only),
            # Deterministic but unique client-side id for deduplication
            "cliOrdId": uuid.uuid4().hex[:32],
        }
        if ot == 'lmt':
            if price is None or float(price) <= 0:
                raise ValueError("kraken: limit order requires positive price")
            params["limitPrice"] = self._format_price(price)

        data = await self._private_post("/api/v3/sendorder", params)

        # sendorder response shape: { "result": "success", "sendStatus": {...} }
        send_status = data.get("sendStatus") or {}
        status = send_status.get("status") or data.get("result")
        order_info: Dict[str, Any] = {
            "id": send_status.get("order_id"),
            "symbol": kraken_sym,
            "side": side_norm,
            "size": size_contracts,  # USD contracts actually sent
            "amount_coin": float(amount),
            "price": params.get("limitPrice"),
            "order_type": ot,
            "reduce_only": bool(is_reduce_only),
            "status": status,
            "raw_data": data,
        }
        if status and "placed" not in str(status).lower() and status != "success":
            order_info["error"] = status
        return order_info

    @staticmethod
    def _format_price(price) -> str:
        d = Decimal(format(float(price), 'f'))
        s = format(d, 'f')
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s or "0"

    async def close_position(self, symbol, position=None, *, is_reduce_only=True) -> Any:
        """
        Closes a position via market order with reduceOnly=True.

        `position` is the dict returned by `get_position` (with `size` in COIN UNITS,
        matching mpdex convention). If omitted we fetch it ourselves.
        """
        if position is None or not position:
            position = await self.get_position(symbol)
        if not position:
            return None

        size_coin = float(position.get("size") or 0)
        if size_coin <= 0:
            return None
        side = 'sell' if str(position.get("side", "")).lower() in ('long', 'buy') else 'buy'
        return await self.create_order(
            symbol,
            side,
            size_coin,
            price=None,
            order_type='market',
            is_reduce_only=is_reduce_only,
        )

    async def cancel_orders(self, symbol, open_orders=None) -> Dict[str, Any]:
        if open_orders is not None and not isinstance(open_orders, list):
            open_orders = [open_orders]

        if open_orders:
            results: List[Any] = []
            for o in open_orders:
                oid = o.get("id") if isinstance(o, dict) else o
                if not oid:
                    continue
                data = await self._private_post(
                    "/api/v3/cancelorder", {"order_id": oid}
                )
                results.append(data)
            return {"cancelled": results}

        kraken_sym = self._resolve_symbol(symbol)
        data = await self._private_post(
            "/api/v3/cancelallorders", {"symbol": kraken_sym}
        )
        return data

    # ------------------------------------------------------------------
    # Leverage (not configurable per-symbol on Kraken multi-collateral;
    # leverage is set account-wide in the UI. Report as not_implemented.)
    # ------------------------------------------------------------------
    async def update_leverage(self, symbol, leverage=None, margin_mode=None):
        kraken_sym = self._resolve_symbol(symbol)
        if leverage is None:
            return {
                "symbol": kraken_sym,
                "leverage": None,
                "margin_mode": margin_mode,
                "status": "not_implemented",
            }
        # POST /api/v3/leveragepreferences  params: symbol, maxLeverage
        try:
            data = await self._private_post(
                "/api/v3/leveragepreferences",
                {"symbol": kraken_sym, "maxLeverage": int(leverage)},
            )
            status = "ok" if (data.get("result") == "success") else "error"
            return {
                "symbol": kraken_sym,
                "leverage": int(leverage),
                "margin_mode": margin_mode,
                "status": status,
                "raw": data,
            }
        except Exception as exc:
            return {
                "symbol": kraken_sym,
                "leverage": int(leverage),
                "margin_mode": margin_mode,
                "status": "error",
                "error": str(exc),
            }

    async def get_leverage_info(self, symbol):
        kraken_sym = self._resolve_symbol(symbol)
        try:
            data = await self._private_get("/api/v3/leveragepreferences")
            levs = data.get("leveragePreferences") or []
            for item in levs:
                if (item.get("symbol") or "").upper() == kraken_sym:
                    return {
                        "symbol": kraken_sym,
                        "leverage": item.get("maxLeverage"),
                        "margin_mode": "cross",
                        "status": "ok",
                        "max_leverage": item.get("maxLeverage"),
                        "available_margin_modes": ["cross"],
                    }
        except Exception:
            pass
        return {
            "symbol": kraken_sym,
            "leverage": None,
            "margin_mode": "cross",
            "status": "not_implemented",
            "max_leverage": None,
            "available_margin_modes": ["cross"],
        }
